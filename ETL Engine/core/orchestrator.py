"""
Orchestrator — builds and executes the migration execution plan.

Responsibilities:
  1. Parse migration config (pipelines + objects)
  2. Auto-detect object dependencies via Describe API (field_rules)
  3. Topological sort within each pipeline (Kahn's algorithm)
  4. Detect cyclic dependencies → flag for two-pass handling
  5. Build final ordered execution plan
  6. Execute pipelines sequentially; objects within a level in parallel (future)
  7. Respect run state — skip completed, retry failed

Tier 0 objects are hardcoded to always load first (User, Profile, etc.)
"""

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any
from core.connector import SFConnector
from core.field_rules import (
    build_field_profile,
    build_soql,
    normalize_query_filter,
    ObjectFieldProfile,
    owner_id_uses_user_external_id,
    resolve_single_lookup_reference,
)

logger = logging.getLogger(__name__)

# Objects that must always be in the first pipeline tier — referenced by everything
TIER_0_OBJECTS = {"UserRole", "Profile", "User"}
TIER_1_OBJECTS = {"RecordType", "BusinessHours", "Pricebook2", "Product2"}

# These objects should NOT be migrated (system objects or handled separately)
SKIP_OBJECTS = {"Group", "GroupMember", "PermissionSet", "PermissionSetAssignment"}


@dataclass
class ObjectPlan:
    object_name: str
    soql: str
    external_id_field: str
    operation: str                          # upsert / insert / update
    transform_rules: list[dict]
    cyclic_fields: list[str]                # fields to null in Pass 1
    depends_on: list[str]                   # objects that must load before this
    scope_rules: list[dict] = field(default_factory=list)
    query_filter: str = ""
    match_by: str | None = None             # e.g. 'Username' for User
    extension_config: dict = field(default_factory=dict)   # per-object extension settings
    lookup_mappings: dict[str, dict] = field(default_factory=dict)
    record_type_mappings: dict[str, str] = field(default_factory=dict)
    source_key_field: str = "Id"


@dataclass
class ExtractionPlan:
    object_name: str
    node_id: str
    soql: str
    level: int = 0
    scope_rules: list[dict] = field(default_factory=list)
    overwrite: bool = True


@dataclass
class PipelinePlan:
    pipeline_id: str
    name: str
    order: int
    seed_cfg: dict
    nodes_cfg: list[dict]
    object_configs: list[dict]
    extract_levels: list[list[ExtractionPlan]]
    object_levels: list[list[ObjectPlan]]   # outer = level, inner = parallel objects
    cyclic_pairs: list[tuple[str, str]]     # (object, cyclic_field) needing Pass 2
    cycle_messages: list[str] = field(default_factory=list)


@dataclass
class ExecutionPlan:
    migration_name: str
    pipelines: list[PipelinePlan]           # ordered by pipeline.order


class Orchestrator:
    def __init__(self, source_connector: SFConnector):
        self.source_connector = source_connector
        self._profile_cache: dict[str, ObjectFieldProfile] = {}

    # ------------------------------------------------------------------ #
    #  Build execution plan from migration config dict                   #
    # ------------------------------------------------------------------ #

    def build_plan(self, config: dict) -> ExecutionPlan:
        """
        Parse migration config and produce a fully-ordered ExecutionPlan.
        config: parsed YAML dict from migration config file.
        """
        migration_name = config.get("migration", {}).get("name", "Unnamed Migration")
        pipelines_cfg = config.get("pipelines", [])

        pipeline_plans = []

        for pcfg in sorted(pipelines_cfg, key=lambda p: p.get("order", 99)):
            pipeline_id = pcfg["id"]
            object_configs = pcfg.get("objects", [])
            seed_cfg = pcfg.get("seed", {})
            nodes_cfg = pcfg.get("nodes", [])
            logger.info(f"[Orchestrator] Building plan for pipeline: {pipeline_id}")

            # Build field profiles for all objects in this pipeline
            profiles = self._build_profiles(object_configs)

            # Build dependency graph and detect cycles
            graph, reverse_graph = self._build_dependency_graph(object_configs, profiles)
            cycles = self._detect_cycles(graph)

            # Topological sort → ordered levels
            object_levels_names, cyclic_pairs, cycle_messages = self._topological_sort(
                object_configs, profiles, graph, cycles, seed_cfg.get("object")
            )

            # Build ObjectPlan for each object
            object_levels = [
                [self._build_object_plan(name, object_configs, profiles, cyclic_pairs, seed_cfg, nodes_cfg)
                 for name in level]
                for level in object_levels_names
            ]
            extract_levels = self._build_extract_levels(object_configs, profiles, seed_cfg, nodes_cfg)

            pipeline_plans.append(PipelinePlan(
                pipeline_id=pipeline_id,
                name=pcfg.get("name", pipeline_id),
                order=pcfg.get("order", 99),
                seed_cfg=seed_cfg,
                nodes_cfg=nodes_cfg,
                object_configs=object_configs,
                extract_levels=extract_levels,
                object_levels=object_levels,
                cyclic_pairs=cyclic_pairs,
                cycle_messages=cycle_messages,
            ))

        return ExecutionPlan(migration_name=migration_name, pipelines=pipeline_plans)

    # ------------------------------------------------------------------ #
    #  Field profile building (with cache)                               #
    # ------------------------------------------------------------------ #

    def _build_profiles(self, object_configs: list[dict]) -> dict[str, ObjectFieldProfile]:
        profiles = {}
        for ocfg in object_configs:
            name = ocfg["name"]
            if name not in self._profile_cache:
                logger.info(f"[Orchestrator] Describing {name}...")
                self._profile_cache[name] = build_field_profile(self.source_connector, name)
            profiles[name] = self._profile_cache[name]
        return profiles

    def _get_profile(self, object_name: str) -> ObjectFieldProfile:
        if object_name not in self._profile_cache:
            logger.info(f"[Orchestrator] Describing {object_name}...")
            self._profile_cache[object_name] = build_field_profile(self.source_connector, object_name)
        return self._profile_cache[object_name]

    def _build_soql_for_object(
        self,
        object_name: str,
        object_config: dict,
        profile: ObjectFieldProfile | None,
        object_configs: list[dict],
        query_filter: str = "",
    ) -> str:
        if not profile:
            soql = f"SELECT Id FROM {object_name}"
            normalized_filter = normalize_query_filter(query_filter)
            if normalized_filter:
                soql += f" {normalized_filter}"
            return soql

        selected_fields = list(object_config.get("fields") or profile.include_fields)
        fields_clause: list[str] = ["Id"] + selected_fields
        object_cfg_by_name = {cfg["name"]: cfg for cfg in object_configs}

        for field_name in selected_fields:
            refs = list(profile.lookup_fields.get(field_name, []))
            field_meta = profile.field_meta.get(field_name)
            explicit_lookup = (object_config.get("lookup_mappings") or {}).get(field_name) or {}
            if owner_id_uses_user_external_id(field_name, refs, explicit_lookup):
                refs = ["User"]
            else:
                single_ref = resolve_single_lookup_reference(field_name, refs)
                if single_ref:
                    refs = [single_ref]
            if len(refs) != 1 or not field_meta or not field_meta.relationship_name:
                continue

            ref_obj = refs[0]
            resolution = str(explicit_lookup.get("lookup_resolution") or "").strip().lower()
            if resolution in {"preserve", "target_id", "idmapper"}:
                continue

            explicit_ref = str(explicit_lookup.get("referenced_object") or "").strip()
            explicit_ext = str(
                explicit_lookup.get("referenced_source_key")
                or explicit_lookup.get("external_id")
                or ""
            ).strip()
            ref_ext_id = None
            if explicit_ext and (not explicit_ref or explicit_ref == ref_obj):
                ref_ext_id = explicit_ext
            if not ref_ext_id:
                ref_ext_id = object_cfg_by_name.get(ref_obj, {}).get("external_id")
            if not ref_ext_id:
                ref_profile = self._get_profile(ref_obj)
                ref_ext_id = next(iter(ref_profile.external_id_fields), None)
            if ref_ext_id:
                fields_clause.append(f"{field_meta.relationship_name}.{ref_ext_id}")

        deduped_fields = list(dict.fromkeys(fields_clause))
        soql = f"SELECT {', '.join(deduped_fields)} FROM {object_name}"
        normalized_filter = normalize_query_filter(query_filter)
        if normalized_filter:
            soql += f" {normalized_filter}"
        return soql

    # ------------------------------------------------------------------ #
    #  Dependency graph construction                                      #
    # ------------------------------------------------------------------ #

    def _build_dependency_graph(
        self,
        object_configs: list[dict],
        profiles: dict[str, ObjectFieldProfile],
    ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """
        Build directed graph: object → set of objects it depends on.
        Only considers objects that are in the current migration set.
        Also respects explicit depends_on overrides in config.
        Extraction scope rules are intentionally excluded here because they
        control retrieval order/scope, not load-time lookup dependencies.
        Returns (graph, reverse_graph).
        """
        object_names = {ocfg["name"] for ocfg in object_configs}
        graph: dict[str, set[str]] = defaultdict(set)
        reverse_graph: dict[str, set[str]] = defaultdict(set)

        for ocfg in object_configs:
            name = ocfg["name"]
            graph.setdefault(name, set())

            # Auto-detect only from lookup fields selected for this migration object.
            profile = profiles.get(name)
            if profile:
                selected_fields = set(ocfg.get("fields", []))

                def is_selected(field_name: str) -> bool:
                    return not selected_fields or field_name in selected_fields

                for field_name, refs in profile.lookup_fields.items():
                    if not is_selected(field_name):
                        continue
                    if len(refs) != 1:
                        continue
                    ref_obj = refs[0]
                    if ref_obj in object_names:
                        graph[name].add(ref_obj)
                        reverse_graph[ref_obj].add(name)

            # Explicit overrides from config
            for dep in ocfg.get("depends_on", []):
                if dep in object_names:
                    graph[name].add(dep)
                    reverse_graph[dep].add(name)

        return dict(graph), dict(reverse_graph)

    # ------------------------------------------------------------------ #
    #  Cycle detection (DFS)                                             #
    # ------------------------------------------------------------------ #

    def _detect_cycles(self, graph: dict[str, set[str]]) -> list[list[str]]:
        """
        Find all cycles in the dependency graph using DFS.
        Returns list of cycles, each cycle is a list of object names.
        """
        visited = set()
        rec_stack = set()
        cycles = []

        def dfs(node, path):
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in graph.get(node, set()):
                if neighbor not in visited:
                    dfs(neighbor, path)
                elif neighbor in rec_stack:
                    # Found a cycle — extract it
                    cycle_start = path.index(neighbor)
                    cycles.append(path[cycle_start:])

            path.pop()
            rec_stack.discard(node)

        for node in list(graph.keys()):
            if node not in visited:
                dfs(node, [])

        return cycles

    # ------------------------------------------------------------------ #
    #  Topological sort — Kahn's algorithm                               #
    # ------------------------------------------------------------------ #

    def _topological_sort(
        self,
        object_configs: list[dict],
        profiles: dict[str, ObjectFieldProfile],
        graph: dict[str, set[str]],
        cycles: list[list[str]],
        seed_object: str | None = None,
    ) -> tuple[list[list[str]], list[tuple[str, str]], list[str]]:
        """
        Perform Kahn's topological sort.
        Cycles are broken by removing the weakest (nullable) edge.
        Returns (ordered_levels, cyclic_pairs).
        cyclic_pairs = [(object_name, field_name), ...] that need Pass 2 resolution.
        """
        object_names = [ocfg["name"] for ocfg in object_configs]
        object_config_map = {cfg["name"]: cfg for cfg in object_configs}

        # Break cycles — record which edges were removed for Pass 2
        cyclic_pairs: list[tuple[str, str]] = []
        cycle_messages: list[str] = []
        seen_cycle_messages: set[str] = set()
        seen_cyclic_pairs: set[tuple[str, str]] = set()
        working_graph = {k: set(v) for k, v in graph.items()}

        remaining_cycles = cycles or self._detect_cycles(working_graph)
        while remaining_cycles:
            cycle = remaining_cycles[0]
            cycle_label = self._format_cycle_path(cycle, profiles, object_config_map)
            if cycle_label not in seen_cycle_messages:
                cycle_messages.append(cycle_label)
                seen_cycle_messages.add(cycle_label)

            # Remove an edge to break cycle — prefer self-references first
            broken = False
            for obj in cycle:
                if obj in working_graph and obj in working_graph.get(obj, set()):
                    working_graph[obj].discard(obj)
                    pair = (obj, "_self_reference_")
                    if pair not in seen_cyclic_pairs:
                        cyclic_pairs.append(pair)
                        seen_cyclic_pairs.add(pair)
                    logger.warning(
                        f"[Orchestrator] Cycle broken: {obj} → {obj} | "
                        f"Will use two-pass resolution"
                    )
                    broken = True
                    break

            if not broken and len(cycle) >= 2:
                candidate = self._choose_cycle_edge_to_break(
                    cycle,
                    working_graph,
                    profiles,
                    object_config_map,
                )
                if candidate:
                    object_name, referenced_object, field_name = candidate
                    working_graph[object_name].discard(referenced_object)
                    pair = (object_name, field_name)
                    if pair not in seen_cyclic_pairs:
                        cyclic_pairs.append(pair)
                        seen_cyclic_pairs.add(pair)
                    logger.warning(
                        f"[Orchestrator] Cycle broken: {object_name}.{field_name} → {referenced_object} | "
                        f"Will defer '{field_name}' to Pass 2"
                    )
                    broken = True

            if not broken:
                raise ValueError(
                    f"Could not break detected cycle: {cycle_label}. "
                    "No removable in-pipeline edge was found."
                )

            remaining_cycles = self._detect_cycles(working_graph)

        # Kahn's algorithm: in_degree[node] = number of prerequisite nodes
        in_degree = defaultdict(int)
        reverse: dict[str, set[str]] = defaultdict(set)
        for obj in object_names:
            in_degree[obj] += 0
        for obj in object_names:
            for dep in working_graph.get(obj, set()):
                in_degree[obj] += 1    # obj depends on dep, so obj needs dep first
                reverse[dep].add(obj)  # when dep is done, obj's in-degree decreases

        levels: list[list[str]] = []
        processed = set()

        def sort_ready(nodes: list[str]) -> list[str]:
            tier0 = sorted([o for o in nodes if o in TIER_0_OBJECTS])
            tier1 = sorted([o for o in nodes if o in TIER_1_OBJECTS and o not in TIER_0_OBJECTS])
            rest = sorted([o for o in nodes if o not in TIER_0_OBJECTS and o not in TIER_1_OBJECTS])
            return tier0 + tier1 + rest

        kahn_queue = deque(sort_ready([
            obj for obj in object_names
            if all(dep not in object_names for dep in working_graph.get(obj, set()))
        ]))

        while kahn_queue:
            current_level = list(kahn_queue)
            levels.append(current_level)
            kahn_queue = deque()
            for obj in current_level:
                processed.add(obj)
                for dependent in reverse.get(obj, set()):
                    if dependent in processed:
                        continue
                    if all(
                        dep in processed or dep not in object_names
                        for dep in working_graph.get(dependent, set())
                    ):
                        kahn_queue.append(dependent)
            if kahn_queue:
                kahn_queue = deque(sort_ready(list(dict.fromkeys(kahn_queue))))

        final_levels = [lvl for lvl in levels if lvl]
        unscheduled = [obj for obj in object_names if obj not in processed]
        if unscheduled:
            unresolved_details = []
            for obj in sort_ready(unscheduled):
                remaining_deps = sort_ready([
                    dep for dep in working_graph.get(obj, set())
                    if dep in object_names and dep not in processed
                ])
                if remaining_deps:
                    unresolved_details.append(f"{obj} (still depends on: {', '.join(remaining_deps)})")
                else:
                    unresolved_details.append(obj)

            detail_text = "; ".join(unresolved_details)
            raise ValueError(
                "Pipeline planning left some configured objects unscheduled after cycle resolution: "
                f"{detail_text}. Resolve the remaining cycle(s) or extend cycle breaking so every configured "
                "object reaches an execution level."
            )

        return final_levels, cyclic_pairs, cycle_messages

    # ------------------------------------------------------------------ #
    #  Build ObjectPlan from config + profile                            #
    # ------------------------------------------------------------------ #

    def _build_object_plan(
        self,
        object_name: str,
        object_configs: list[dict],
        profiles: dict[str, ObjectFieldProfile],
        cyclic_pairs: list[tuple[str, str]],
        seed_cfg: dict | None = None,
        nodes_cfg: list[dict] | None = None,
    ) -> ObjectPlan:
        # Find config for this object
        ocfg = next((o for o in object_configs if o["name"] == object_name), {})
        profile = profiles.get(object_name)
        seed_cfg = seed_cfg or {}
        nodes_cfg = nodes_cfg or []
        scope_rules_by_object = self._build_scope_rules(nodes_cfg, seed_cfg.get("object"))
        effective_query_filter = ocfg.get("query_filter", "")
        if not effective_query_filter and object_name == seed_cfg.get("object"):
            effective_query_filter = seed_cfg.get("query_filter", "")

        # Build SOQL
        soql = ocfg.get("soql")
        if not soql and profile:
            soql = self._build_soql_for_object(
                object_name,
                ocfg,
                profile,
                object_configs,
                effective_query_filter,
            )
        elif not soql:
            soql = f"SELECT Id FROM {object_name}"

        # Cyclic fields for this object
        obj_cyclic_fields = self._resolve_cyclic_fields(
            object_name,
            profile,
            ocfg,
            cyclic_pairs,
        )

        operation = (ocfg.get("operation") or "upsert").strip().lower()
        external_id_field = (ocfg.get("external_id") or "").strip()
        if operation == "upsert" and not external_id_field:
            raise ValueError(
                f"[{object_name}] No target external ID field is configured for upsert. "
                "Choose a real target external ID field before running this pipeline."
            )
        if not external_id_field:
            external_id_field = "Id"

        # Auto-inject: copy source Id → external_id_field so upsert key is populated
        # e.g. Migration_Key__c = source record Id (cross-org identity key)
        configured_rules = list(ocfg.get("transforms", []))
        if external_id_field != "Id":
            already_mapped = any(
                r.get("type") == "field_rename" and r.get("target") == external_id_field
                for r in configured_rules
            )
            if not already_mapped:
                configured_rules = [
                    {"type": "field_rename", "source": "Id", "target": external_id_field}
                ] + configured_rules

        extension_config = ocfg.get("extensions") or {}
        if not isinstance(extension_config, dict):
            extension_config = {}

        return ObjectPlan(
            object_name=object_name,
            soql=soql,
            external_id_field=external_id_field,
            operation=operation,
            transform_rules=configured_rules,
            cyclic_fields=obj_cyclic_fields,
            depends_on=list(ocfg.get("depends_on", [])),
            scope_rules=scope_rules_by_object.get(object_name, []),
            query_filter=effective_query_filter,
            match_by=ocfg.get("match_by"),
            extension_config=dict(extension_config),
            lookup_mappings={
                str(field_name): dict(mapping)
                for field_name, mapping in (ocfg.get("lookup_mappings") or {}).items()
                if isinstance(mapping, dict) and str(field_name) != "RecordTypeId"
            },
            record_type_mappings={
                str(source): str(target)
                for source, target in (ocfg.get("record_type_mappings") or {}).items()
                if str(source or "").strip() and str(target or "").strip()
            },
            source_key_field=str(
                (ocfg.get("identity") or {}).get("source_field")
                or ocfg.get("source_key_field")
                or "Id"
            ).strip() or "Id",
        )

    def _resolve_cyclic_fields(
        self,
        object_name: str,
        profile: ObjectFieldProfile | None,
        object_config: dict,
        cyclic_pairs: list[tuple[str, str]],
    ) -> list[str]:
        """
        Resolve deferred cyclic fields for this object.
        `cyclic_pairs` stores the exact field selected for Pass 2 when possible.
        """
        if not profile:
            return []

        selected_fields = set(object_config.get("fields", []))
        cyclic_fields: set[str] = set()

        def is_selected(field_name: str) -> bool:
            return not selected_fields or field_name in selected_fields

        # Explicit deferred external-ID lookups are two-pass fields even when
        # they are not graph cycles (for example EmailMessage.ActivityId).
        for field_name, mapping in (object_config.get("lookup_mappings") or {}).items():
            if not isinstance(mapping, dict) or not is_selected(str(field_name)):
                continue
            resolution = str(mapping.get("lookup_resolution") or "").strip().lower()
            field_meta = profile.field_meta.get(str(field_name))
            references = list(profile.lookup_fields.get(str(field_name), []))
            can_update_after_insert = bool(getattr(field_meta, "updateable", False))
            if resolution == "deferred_external_id" and can_update_after_insert:
                cyclic_fields.add(str(field_name))

        has_self_reference_cycle = any(
            obj == object_name and reference == "_self_reference_"
            for obj, reference in cyclic_pairs
        )
        if has_self_reference_cycle:
            for field_name, refs in profile.lookup_fields.items():
                if object_name in refs and is_selected(field_name):
                    cyclic_fields.add(field_name)

        for obj, reference in cyclic_pairs:
            if obj != object_name or reference == "_self_reference_":
                continue
            if reference in profile.lookup_fields and is_selected(reference):
                cyclic_fields.add(reference)
                continue
            for field_name, refs in profile.lookup_fields.items():
                if len(refs) == 1 and refs[0] == reference and is_selected(field_name):
                    cyclic_fields.add(field_name)

        return sorted(cyclic_fields)

    def _choose_cycle_edge_to_break(
        self,
        cycle: list[str],
        working_graph: dict[str, set[str]],
        profiles: dict[str, ObjectFieldProfile],
        object_config_map: dict[str, dict],
    ) -> tuple[str, str, str] | None:
        candidates: list[tuple[int, str, str, str]] = []

        def cycle_edges() -> list[tuple[str, str]]:
            if not cycle:
                return []
            edges: list[tuple[str, str]] = []
            for idx, obj in enumerate(cycle):
                dep = cycle[(idx + 1) % len(cycle)]
                if dep in working_graph.get(obj, set()):
                    edges.append((obj, dep))
            return edges

        for object_name, referenced_object in cycle_edges():
            profile = profiles.get(object_name)
            object_config = object_config_map.get(object_name, {})
            if not profile:
                continue
            selected_fields = set(object_config.get("fields", []))

            for field_name, refs in profile.lookup_fields.items():
                if len(refs) != 1 or refs[0] != referenced_object:
                    continue
                if selected_fields and field_name not in selected_fields:
                    continue
                score = self._score_cyclic_field(profile, field_name)
                candidates.append((score, object_name, referenced_object, field_name))

        if not candidates:
            return None

        candidates.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
        _, object_name, referenced_object, field_name = candidates[0]
        return object_name, referenced_object, field_name

    def _score_cyclic_field(self, profile: ObjectFieldProfile, field_name: str) -> int:
        meta = profile.field_meta.get(field_name)
        if not meta:
            return 0

        score = 0
        normalized = field_name.lower()

        # Prefer deferring fields that are not core parent anchors.
        if field_name == "AccountId":
            score -= 100
        if normalized.endswith("ownerid"):
            score -= 80
        if normalized.endswith("parentid"):
            score -= 30

        # Prefer deferring optional/custom lookups over foundational standard ones.
        if meta.nillable:
            score += 40
        if field_name.endswith("__c"):
            score += 35
        if field_name.endswith("__pc") or field_name.endswith("__pr"):
            score += 20
        if field_name.startswith("Primary_") or field_name.startswith("Preferred_"):
            score += 15

        # Standard Salesforce "anchor" lookups are usually better kept in Pass 1.
        if field_name in {"AccountId", "ContactId", "OpportunityId", "OrderId"}:
            score -= 40
        if field_name == "ReportsToId":
            score += 10

        # Slight preference toward fields that are not required on insert.
        if not meta.is_required:
            score += 10

        return score

    def _build_extract_levels(
        self,
        object_configs: list[dict],
        profiles: dict[str, ObjectFieldProfile],
        seed_cfg: dict | None = None,
        nodes_cfg: list[dict] | None = None,
    ) -> list[list[ExtractionPlan]]:
        seed_cfg = seed_cfg or {}
        nodes_cfg = nodes_cfg or []
        if not nodes_cfg:
            levels: list[list[ExtractionPlan]] = []
            seen_objects: set[str] = set()
            for ocfg in object_configs:
                object_name = ocfg["name"]
                profile = profiles.get(object_name)
                effective_query_filter = ocfg.get("query_filter", "")
                if not effective_query_filter and object_name == seed_cfg.get("object"):
                    effective_query_filter = seed_cfg.get("query_filter", "")
                soql = self._build_soql_for_object(
                    object_name,
                    ocfg,
                    profile,
                    object_configs,
                    effective_query_filter,
                )
                levels.append([ExtractionPlan(
                    object_name=object_name,
                    node_id=object_name,
                    soql=soql,
                    level=len(levels),
                    scope_rules=[],
                    overwrite=object_name not in seen_objects,
                )])
                seen_objects.add(object_name)
            return levels

        object_cfg_by_name = {ocfg["name"]: ocfg for ocfg in object_configs}
        node_by_id = {node["id"]: node for node in nodes_cfg}
        seen_objects: set[str] = set()
        levels: list[list[ExtractionPlan]] = []

        for depth in sorted({node.get("depth", 0) for node in nodes_cfg}):
            level: list[ExtractionPlan] = []
            for node in [n for n in nodes_cfg if n.get("depth", 0) == depth]:
                object_name = node.get("object")
                if object_name not in object_cfg_by_name:
                    continue

                ocfg = object_cfg_by_name[object_name]
                profile = profiles.get(object_name)
                effective_query_filter = ocfg.get("query_filter", "")
                if (
                    node.get("direction") == "seed"
                    and not effective_query_filter
                    and object_name == seed_cfg.get("object")
                ):
                    effective_query_filter = seed_cfg.get("query_filter", "")

                soql = self._build_soql_for_object(
                    object_name,
                    ocfg,
                    profile,
                    object_configs,
                    effective_query_filter,
                )

                scope_rules: list[dict] = []
                parent_id = node.get("parent_id")
                if node.get("direction") != "seed" and parent_id and parent_id in node_by_id:
                    parent = node_by_id[parent_id]
                    scope_rules = [{
                        "mode": node.get("direction"),
                        "source_object": parent.get("object"),
                        "via_field": node.get("via_field"),
                    }]

                level.append(ExtractionPlan(
                    object_name=object_name,
                    node_id=node["id"],
                    soql=soql,
                    level=depth,
                    scope_rules=scope_rules,
                    overwrite=object_name not in seen_objects,
                ))
                seen_objects.add(object_name)

            if level:
                levels.append(level)

        return levels

    def _build_scope_rules(self, nodes: list[dict], seed_object: str | None) -> dict[str, list[dict]]:
        if not nodes:
            return {}

        node_by_id = {node["id"]: node for node in nodes}
        rules_by_object: dict[str, list[dict]] = defaultdict(list)

        for node in nodes:
            if node.get("direction") == "seed" or not node.get("parent_id"):
                continue

            object_name = node.get("object")
            if seed_object and object_name == seed_object:
                continue

            parent = node_by_id.get(node["parent_id"])
            if not parent:
                continue

            rule = {
                "mode": node.get("direction"),
                "source_object": parent.get("object"),
                "via_field": node.get("via_field"),
            }
            if rule not in rules_by_object[object_name]:
                rules_by_object[object_name].append(rule)

        return dict(rules_by_object)

    # ------------------------------------------------------------------ #
    #  Plan display                                                       #
    # ------------------------------------------------------------------ #

    def display_plan(self, plan: ExecutionPlan):
        from rich.console import Console
        from rich.table import Table
        console = Console()
        console.print(f"\n[bold cyan]Migration: {plan.migration_name}[/bold cyan]\n")

        for pipeline in plan.pipelines:
            console.print(f"[bold yellow]Pipeline [{pipeline.order}]: {pipeline.name}[/bold yellow]")
            for level_idx, level in enumerate(pipeline.extract_levels):
                objects_str = ", ".join(o.object_name for o in level)
                console.print(f"  Level {level_idx}: [green]{objects_str}[/green]")
            if pipeline.cycle_messages:
                for message in pipeline.cycle_messages:
                    console.print(f"  [red]⚠ Cycle: {message} → Two-pass resolution[/red]")
            elif pipeline.cyclic_pairs:
                for obj, fld in pipeline.cyclic_pairs:
                    label = self._format_cyclic_pair_label(obj, fld)
                    console.print(f"  [red]⚠ Cycle: {label} → Two-pass resolution[/red]")
            console.print()

    def _format_cyclic_pair_label(self, object_name: str, reference: str) -> str:
        if reference == "_self_reference_":
            return f"{object_name} (self-reference)"

        profile = self._profile_cache.get(object_name)
        if profile:
            if reference in profile.lookup_fields:
                refs = profile.lookup_fields.get(reference, [])
                if len(refs) == 1:
                    return f"{object_name}.{reference} -> {refs[0]}"
                return f"{object_name}.{reference}"
            for field_name, refs in profile.lookup_fields.items():
                if len(refs) == 1 and refs[0] == reference:
                    return f"{object_name}.{field_name} -> {reference}"

        return f"{object_name} -> {reference}"

    def _format_cycle_path(
        self,
        cycle: list[str],
        profiles: dict[str, ObjectFieldProfile],
        object_config_map: dict[str, dict],
    ) -> str:
        if not cycle:
            return "(unknown cycle)"
        if len(cycle) == 1:
            obj = cycle[0]
            profile = profiles.get(obj)
            object_config = object_config_map.get(obj, {})
            if profile:
                selected_fields = set(object_config.get("fields", []))

                def is_selected(field_name: str) -> bool:
                    return not selected_fields or field_name in selected_fields

                self_fields = [
                    field_name
                    for field_name, refs in profile.lookup_fields.items()
                    if obj in refs and is_selected(field_name)
                ]
                if self_fields:
                    labels = [f"{obj}.{field_name} -> {obj}" for field_name in sorted(self_fields)]
                    return " | ".join(labels)
            return f"{obj} (self-reference)"

        labels = []
        for idx, obj in enumerate(cycle):
            nxt = cycle[(idx + 1) % len(cycle)]
            labels.append(self._format_cycle_edge(obj, nxt, profiles.get(obj), object_config_map.get(obj, {})))
        return " -> ".join(labels)

    def _format_cycle_edge(
        self,
        object_name: str,
        referenced_object: str,
        profile: ObjectFieldProfile | None,
        object_config: dict,
    ) -> str:
        if not profile:
            return f"{object_name} -> {referenced_object}"

        selected_fields = set(object_config.get("fields", []))

        def is_selected(field_name: str) -> bool:
            return not selected_fields or field_name in selected_fields

        for field_name, refs in profile.lookup_fields.items():
            if len(refs) == 1 and refs[0] == referenced_object and is_selected(field_name):
                return f"{object_name}.{field_name} -> {referenced_object}"

        return f"{object_name} -> {referenced_object}"
