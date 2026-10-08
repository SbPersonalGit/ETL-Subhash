"""Compile UI identity intent into a node-wise runtime identity graph.

The compiler is additive: it also emits the lookup mapping shape consumed by
the existing orchestrator/transformer, so two-pass behavior remains unchanged.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass


@dataclass
class IdentityCompilation:
    objects: list[dict]
    graph: dict


def _text(value) -> str:
    return str(value or "").strip()


def compile_stage_identities(objects: list[dict], input_mode: str) -> IdentityCompilation:
    compiled = deepcopy(objects)
    by_name = {_text(obj.get("name")): obj for obj in compiled if _text(obj.get("name"))}
    graph_nodes: dict[str, dict] = {}
    edges: list[dict] = []

    for name, obj in by_name.items():
        target_key = _text(obj.get("external_id"))
        source_key = _text(obj.get("source_key_field"))
        if not source_key:
            if input_mode == "flat_file":
                source_key = target_key
            else:
                source_key = next((
                    _text(rule.get("source"))
                    for rule in (obj.get("transforms") or [])
                    if rule.get("type") == "field_rename"
                    and _text(rule.get("target")) == target_key
                ), "Id")
        identity = {
            "source_field": source_key,
            "source_key_type": "external_key" if input_mode == "flat_file" else "salesforce_key",
            "target_field": target_key,
        }
        obj["identity"] = identity
        graph_nodes[name] = {"object": name, "load": True, "identity": dict(identity)}

    for child_name, obj in by_name.items():
        rename_targets = {
            _text(rule.get("source")): _text(rule.get("target"))
            for rule in (obj.get("transforms") or [])
            if rule.get("type") == "field_rename"
        }
        for source_field, mapping in (obj.get("lookup_mappings") or {}).items():
            if not isinstance(mapping, dict):
                continue
            source_field = _text(source_field)
            mapping.setdefault("source_field", source_field)
            mapping.setdefault("target_field", rename_targets.get(source_field, source_field))
            resolution = _text(mapping.get("lookup_resolution")).lower() or "auto"
            referenced_object = _text(mapping.get("referenced_object"))
            edge = {
                "object": child_name,
                "source_field": source_field,
                "target_field": _text(mapping.get("target_field")) or source_field,
                "referenced_object": referenced_object,
                "resolution": resolution,
            }
            edges.append(edge)

            if resolution in {"preserve", "target_id", "idmapper"} or not referenced_object:
                continue

            # A reference without relationshipName cannot be submitted as
            # Relationship.ExternalId. Keep its explicit deferred policy even
            # when the referenced object also appears in this pipeline.
            if resolution == "deferred_external_id":
                target_key = _text(mapping.get("external_id"))
                source_key = _text(mapping.get("referenced_source_key")) or target_key
                if target_key and referenced_object not in graph_nodes:
                    graph_nodes[referenced_object] = {
                        "object": referenced_object,
                        "load": False,
                        "identity": {
                            "source_field": source_key,
                            "source_key_type": "external_key",
                            "target_field": target_key,
                        },
                    }
                continue

            parent = by_name.get(referenced_object)
            if parent:
                parent_identity = parent.get("identity") or {}
                target_key = _text(parent_identity.get("target_field"))
                explicit_key = _text(mapping.get("external_id"))
                if explicit_key and target_key and explicit_key != target_key:
                    raise ValueError(
                        f"{child_name}.{source_field} uses {referenced_object}.{explicit_key}, "
                        f"but the in-pipeline parent identity is {referenced_object}.{target_key}."
                    )
                if not target_key:
                    raise ValueError(
                        f"{child_name}.{source_field} references in-pipeline object "
                        f"'{referenced_object}', which has no target match field."
                    )
                mapping["lookup_resolution"] = "pipeline_object"
                mapping["external_id"] = target_key
                mapping["referenced_source_key"] = _text(parent_identity.get("source_field"))
                edge["resolution"] = "pipeline_object"
                continue

            target_key = _text(mapping.get("external_id"))
            source_key = _text(mapping.get("referenced_source_key")) or target_key
            if not target_key:
                # Compatibility: legacy mappings without an explicit reference
                # identity continue to the existing runtime fallback/IDMapper.
                continue
            identity = {
                "source_field": source_key,
                "source_key_type": "external_key",
                "target_field": target_key,
            }
            existing = graph_nodes.get(referenced_object)
            if existing and existing.get("identity") != identity:
                raise ValueError(f"Conflicting Reference Identity definitions for '{referenced_object}'.")
            graph_nodes[referenced_object] = {
                "object": referenced_object,
                "load": False,
                "identity": identity,
            }

    return IdentityCompilation(
        objects=compiled,
        graph={"nodes": list(graph_nodes.values()), "lookup_edges": edges},
    )
