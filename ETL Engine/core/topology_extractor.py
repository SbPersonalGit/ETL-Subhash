from __future__ import annotations

import io
import logging
import re
import time
from collections import defaultdict
from urllib.parse import quote_plus

import polars as pl
import requests

from core.connector import SFConnector
from core.field_rules import ObjectFieldProfile, split_query_filter
from core.polars_coerce import coerce_dataframe_columns_to_utf8
from core.stager import Stager

logger = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 5
MAX_POLL_ATTEMPTS = 360
PHASE_A_REST_SCOPE_CHUNK_SIZE = 200
PHASE_A_BULK_SCOPE_CHUNK_SIZE = 1000
PHASE_A_BULK_THRESHOLD = 500
PHASE_B_ID_CHUNK_SIZE = 1000
REST_FALLBACK_MAX_ENCODED_SOQL_LENGTH = 12000


class TopologyExtractionError(Exception):
    pass


class TopologyExtractor:
    def __init__(self, connector: SFConnector, stager: Stager):
        self.connector = connector
        self.stager = stager
        self.last_node_stats: list[dict] = []
        self.last_pipeline_report: dict[str, object] = {}

    def extract_pipeline(
        self,
        pipe_plan,
        profiles: dict[str, ObjectFieldProfile],
        overwrite_existing: bool = True,
    ) -> dict[str, int]:
        self.last_node_stats = []
        self.last_pipeline_report = {}
        object_cfg_by_name = {cfg["name"]: cfg for cfg in pipe_plan.object_configs}
        node_by_id = {node["id"]: node for node in pipe_plan.nodes_cfg}
        children_by_parent: dict[str, list[dict]] = defaultdict(list)
        for node in pipe_plan.nodes_cfg:
            parent_id = node.get("parent_id")
            if parent_id:
                children_by_parent[parent_id].append(node)

        discovered_ids_by_node: dict[str, set[str]] = defaultdict(set)
        discovered_ids_by_object: dict[str, set[str]] = defaultdict(set)
        object_level_by_name: dict[str, int] = {}
        thin_rows_by_node: dict[str, list[dict]] = {}

        for level in pipe_plan.extract_levels:
            for extract_plan in level:
                node = node_by_id.get(extract_plan.node_id)
                if not node:
                    continue

                object_name = extract_plan.object_name
                object_level_by_name[object_name] = min(
                    extract_plan.level,
                    object_level_by_name.get(object_name, extract_plan.level),
                )
                profile = profiles.get(object_name)
                explicit_filter = (
                    node.get("query_filter")
                    or (pipe_plan.seed_cfg.get("query_filter", "") if node.get("direction") == "seed" else "")
                    or object_cfg_by_name.get(object_name, {}).get("query_filter", "")
                )
                object_cfg = object_cfg_by_name.get(object_name, {})
                selected_fields = object_cfg.get("fields") or profile.include_fields if profile else []
                thin_fields = self._get_thin_fields(
                    node["id"],
                    children_by_parent,
                    profile,
                    selected_fields,
                )
                scope_ids = self._get_scope_ids(node, node_by_id, discovered_ids_by_node, thin_rows_by_node)
                if node.get("direction") != "seed" and not scope_ids:
                    logger.warning(self._build_missing_scope_message(node, node_by_id, thin_rows_by_node))
                clean_rows = self._run_phase_a_discovery(
                    object_name=object_name,
                    node=node,
                    level=extract_plan.level,
                    thin_fields=thin_fields,
                    explicit_filter=explicit_filter,
                    scope_ids=scope_ids,
                )
                thin_rows_by_node[node["id"]] = clean_rows

                ids = {
                    str(row["Id"])
                    for row in clean_rows
                    if row.get("Id")
                }
                self.last_node_stats.append({
                    "pipeline_id": pipe_plan.pipeline_id,
                    "pipeline_name": pipe_plan.name,
                    "node_id": node.get("id"),
                    "object_name": object_name,
                    "level": extract_plan.level,
                    "direction": node.get("direction", ""),
                    "parent_id": node.get("parent_id", ""),
                    "via_field": node.get("via_field", ""),
                    "scope_id_count": len(scope_ids),
                    "discovered_row_count": len(clean_rows),
                    "discovered_id_count": len(ids),
                })
                if scope_ids and not ids:
                    parent_node = node_by_id.get(node.get("parent_id"))
                    parent_object = parent_node.get("object") if parent_node else "<unknown>"
                    via_field = node.get("via_field") or "<unknown>"
                    logger.warning(
                        f"[{object_name}][{node['id']}] Scoped discovery returned 0 records "
                        f"even though {len(scope_ids)} candidate ID(s) were available from "
                        f"{parent_object}.{via_field}. Check whether the parent lookup field is populated, "
                        f"the node direction is correct, and the IDs belong to {object_name}."
                    )
                discovered_ids_by_node[node["id"]] = ids
                discovered_ids_by_object[object_name].update(ids)

        staged_counts: dict[str, int] = {}
        for object_name, ids in discovered_ids_by_object.items():
            if overwrite_existing:
                self.stager.drop_table(object_name, "raw")
            if not ids:
                staged_counts[object_name] = 0
                continue

            object_cfg = object_cfg_by_name.get(object_name, {})
            selected_fields = object_cfg.get("fields") or profiles[object_name].include_fields
            final_count = self._fetch_full_records(
                object_name,
                sorted(ids),
                selected_fields,
                level=object_level_by_name.get(object_name),
            )
            if final_count == 0:
                logger.warning(
                    f"[{object_name}] Discovery found {len(ids)} record ID(s), but Phase B fetch wrote 0 raw rows. "
                    "Check selected fields, object access, and Bulk API query results."
                )
            staged_counts[object_name] = final_count

        configured_max_depth = max(
            (int(stat["level"]) for stat in self.last_node_stats),
            default=-1,
        )
        actual_max_depth = max(
            (int(stat["level"]) for stat in self.last_node_stats if int(stat["discovered_row_count"]) > 0),
            default=-1,
        )
        self.last_pipeline_report = {
            "pipeline_id": pipe_plan.pipeline_id,
            "pipeline_name": pipe_plan.name,
            "configured_max_depth": configured_max_depth,
            "actual_max_depth": actual_max_depth,
            "last_level_nodes": [
                stat for stat in self.last_node_stats
                if int(stat["level"]) == configured_max_depth
            ],
            "reached_last_level_nodes": [
                stat for stat in self.last_node_stats
                if int(stat["level"]) == configured_max_depth and int(stat["discovered_row_count"]) > 0
            ],
        }
        return staged_counts

    def get_last_pipeline_report(self) -> dict[str, object]:
        return dict(self.last_pipeline_report)

    def _run_phase_a_discovery(
        self,
        object_name: str,
        node: dict,
        level: int,
        thin_fields: list[str],
        explicit_filter: str,
        scope_ids: list[str],
    ) -> list[dict]:
        if scope_ids:
            use_bulk = len(scope_ids) > PHASE_A_BULK_THRESHOLD
            chunk_size = PHASE_A_BULK_SCOPE_CHUNK_SIZE if use_bulk else PHASE_A_REST_SCOPE_CHUNK_SIZE
            mode_label = "Bulk API 2.0" if use_bulk else "REST"
            all_rows: dict[str, dict] = {}
            for scope_chunk in self._chunk(scope_ids, chunk_size):
                soql = self._build_phase_a_soql(
                    object_name=object_name,
                    thin_fields=thin_fields,
                    explicit_filter=explicit_filter,
                    node=node,
                    scope_ids=scope_chunk,
                )
                if not soql:
                    continue
                logger.info(
                    f"[{object_name}][{node['id']}][L{level}] Phase A discovery ({mode_label}) | scoped IDs: {len(scope_chunk)}/{len(scope_ids)} | SOQL: {self._summarize_soql(soql)}"
                )
                rows = (
                    self._query_phase_a_bulk_rows(object_name, soql)
                    if use_bulk
                    else [{k: v for k, v in rec.items() if k != "attributes"} for rec in self.connector.query(soql)]
                )
                for clean in rows:
                    record_id = clean.get("Id")
                    if record_id:
                        all_rows[str(record_id)] = clean
            return list(all_rows.values())

        soql = self._build_phase_a_soql(
            object_name=object_name,
            thin_fields=thin_fields,
            explicit_filter=explicit_filter,
            node=node,
            scope_ids=scope_ids,
        )
        if not soql:
            return []

        logger.info(
            f"[{object_name}][{node['id']}][L{level}] Phase A discovery | SOQL: {self._summarize_soql(soql)}"
        )
        records = self.connector.query(soql)
        return [{k: v for k, v in rec.items() if k != "attributes"} for rec in records]

    def _query_phase_a_bulk_rows(self, object_name: str, soql: str) -> list[dict]:
        df = self._bulk_query_to_df(object_name, soql)
        if df is None or df.is_empty():
            return []
        df = coerce_dataframe_columns_to_utf8(df)
        return df.to_dicts()

    def _get_thin_fields(
        self,
        node_id: str,
        children_by_parent: dict[str, list[dict]],
        profile: ObjectFieldProfile | None = None,
        selected_fields: list[str] | None = None,
    ) -> list[str]:
        fields = {"Id"}
        for child in children_by_parent.get(node_id, []):
            if child.get("direction") == "parent" and child.get("via_field"):
                fields.add(child["via_field"])
        if profile:
            selected_lookup_fields = set(profile.lookup_fields.keys())
            for field_name in selected_fields or []:
                if field_name in selected_lookup_fields:
                    fields.add(field_name)
        return sorted(fields)

    def _get_scope_ids(
        self,
        node: dict,
        node_by_id: dict[str, dict],
        discovered_ids_by_node: dict[str, set[str]],
        thin_rows_by_node: dict[str, list[dict]],
    ) -> list[str]:
        direction = node.get("direction")
        parent_id = node.get("parent_id")
        via_field = node.get("via_field")

        if direction == "seed" or not parent_id:
            return []
        if direction == "child":
            return sorted(discovered_ids_by_node.get(parent_id, set()))
        if direction == "parent" and via_field:
            parent_object = (node_by_id.get(parent_id) or {}).get("object")
            if not parent_object:
                return sorted({
                    str(row[via_field])
                    for row in thin_rows_by_node.get(parent_id, [])
                    if row.get(via_field)
                })
            return sorted({
                str(row[via_field])
                for candidate_node_id, rows in thin_rows_by_node.items()
                if (node_by_id.get(candidate_node_id) or {}).get("object") == parent_object
                for row in rows
                if row.get(via_field)
            })
        return []

    def _build_missing_scope_message(
        self,
        node: dict,
        node_by_id: dict[str, dict],
        thin_rows_by_node: dict[str, list[dict]],
    ) -> str:
        object_name = node.get("object") or "<unknown>"
        node_id = node.get("id") or "<unknown>"
        parent_node = node_by_id.get(node.get("parent_id"))
        parent_object = parent_node.get("object") if parent_node else "<unknown>"
        via_field = node.get("via_field") or "<unknown>"
        parent_rows = thin_rows_by_node.get(node.get("parent_id"), [])
        parent_row_count = len(parent_rows)
        available_columns = sorted({
            key
            for row in parent_rows[:5]
            for key in row.keys()
        })

        if not parent_rows:
            reason = "the parent node produced 0 Phase A rows"
            sample_values = []
        elif via_field not in available_columns:
            reason = f"the expected scope field '{via_field}' was not present in the parent Phase A rows"
            sample_values = []
        else:
            non_null_values = []
            for row in parent_rows[:5]:
                value = row.get(via_field)
                if value:
                    non_null_values.append(str(value))
            if non_null_values:
                reason = (
                    f"sample parent values existed but none became scope IDs after normalization: "
                    f"{', '.join(non_null_values[:3])}"
                )
            else:
                reason = f"the parent Phase A rows had only blank/null values for '{via_field}'"
            sample_values = non_null_values[:3]

        columns_text = ", ".join(available_columns[:8]) if available_columns else "<none>"
        if len(available_columns) > 8:
            columns_text += ", ..."

        message = (
            f"[{object_name}][{node_id}] No scope IDs were available from "
            f"{parent_object}.{via_field}, so this node was skipped during discovery. "
            f"Parent Phase A rows: {parent_row_count}. Available columns: {columns_text}. "
            f"Reason: {reason}."
        )
        if sample_values:
            message += f" Sample values: {', '.join(sample_values)}."
        return message

    def _fetch_full_records(self, object_name: str, ids: list[str], selected_fields: list[str], level: int | None = None) -> int:
        fields = ["Id"] + [field for field in selected_fields if field != "Id"]
        select_clause = ", ".join(dict.fromkeys(fields))

        for id_chunk in self._chunk(ids, PHASE_B_ID_CHUNK_SIZE):
            where_clause = self._build_in_clause("Id", id_chunk)
            soql = f"SELECT {select_clause} FROM {object_name} WHERE {where_clause}"
            level_text = f"[L{level}]" if level is not None else ""
            logger.info(
                f"[{object_name}]{level_text} Phase B fetch (Bulk API 2.0) | ID chunk: {len(id_chunk)} | SOQL: {self._summarize_soql(soql)}"
            )
            try:
                df = self._bulk_query_to_df(object_name, soql)
            except requests.HTTPError as exc:
                logger.warning(
                    f"[{object_name}]{level_text} Bulk Phase B fetch rejected by Salesforce. "
                    f"Falling back to REST for this chunk. Error: {exc}"
                )
                logger.info(
                    f"[{object_name}]{level_text} Phase B fetch (REST fallback) | ID chunk: {len(id_chunk)} | "
                    f"SOQL: {self._summarize_soql(soql)}"
                )
                df = self._rest_fetch_full_records_chunk(
                    object_name=object_name,
                    select_clause=select_clause,
                    ids=id_chunk,
                    level_text=level_text,
                )
            if df is None or df.is_empty():
                continue
            df = coerce_dataframe_columns_to_utf8(df)
            self.stager.write_raw_chunk(object_name, df)

        self.stager.deduplicate_object(object_name, "raw")
        return self.stager.get_raw_count(object_name)

    def _bulk_query_to_df(self, object_name: str, soql: str) -> pl.DataFrame | None:
        job_id = self._create_query_job(soql)
        self._wait_for_job(object_name, job_id)

        frames: list[pl.DataFrame] = []
        locator = None
        while True:
            resp = self.connector.bulk_get_csv(f"jobs/query/{job_id}/results", locator=locator)
            csv_text = self.connector.decode_csv_response(resp)
            if not csv_text.strip():
                break

            df = pl.read_csv(io.StringIO(csv_text), infer_schema_length=1000, null_values=[""])
            if not df.is_empty():
                frames.append(df)

            locator = resp.headers.get("Sforce-Locator")
            if not locator or locator == "null":
                break

        if not frames:
            return None
        return pl.concat(frames, how="diagonal_relaxed")

    def _rest_query_to_df(self, soql: str) -> pl.DataFrame | None:
        records = self.connector.query(soql)
        rows = [{k: v for k, v in rec.items() if k != "attributes"} for rec in records]
        if not rows:
            return None
        return pl.DataFrame(rows)

    def _rest_fetch_full_records_chunk(
        self,
        object_name: str,
        select_clause: str,
        ids: list[str],
        level_text: str = "",
    ) -> pl.DataFrame | None:
        frames: list[pl.DataFrame] = []
        for rest_chunk in self._chunk_ids_for_rest_fallback(object_name, select_clause, ids):
            where_clause = self._build_in_clause("Id", rest_chunk)
            soql = f"SELECT {select_clause} FROM {object_name} WHERE {where_clause}"
            logger.info(
                f"[{object_name}]{level_text} REST fallback subchunk | ID chunk: {len(rest_chunk)} | "
                f"SOQL: {self._summarize_soql(soql)}"
            )
            df = self._rest_query_to_df(soql)
            if df is not None and not df.is_empty():
                frames.append(df)
        if not frames:
            return None
        return pl.concat(frames, how="diagonal_relaxed")

    def _chunk_ids_for_rest_fallback(
        self,
        object_name: str,
        select_clause: str,
        ids: list[str],
    ) -> list[list[str]]:
        chunks: list[list[str]] = []
        current_chunk: list[str] = []

        for record_id in ids:
            candidate_chunk = current_chunk + [record_id]
            if self._rest_fallback_query_too_large(object_name, select_clause, candidate_chunk):
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = [record_id]
                else:
                    chunks.append([record_id])
                    current_chunk = []
            else:
                current_chunk = candidate_chunk

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def _rest_fallback_query_too_large(self, object_name: str, select_clause: str, ids: list[str]) -> bool:
        where_clause = self._build_in_clause("Id", ids)
        soql = f"SELECT {select_clause} FROM {object_name} WHERE {where_clause}"
        encoded_soql = quote_plus(soql)
        return len(encoded_soql) > REST_FALLBACK_MAX_ENCODED_SOQL_LENGTH

    def _create_query_job(self, soql: str) -> str:
        payload = {
            "operation": "query",
            "query": soql,
        }
        result = self.connector.bulk_post("jobs/query", payload)
        return result["id"]

    def _wait_for_job(self, object_name: str, job_id: str):
        for _ in range(MAX_POLL_ATTEMPTS):
            result = self.connector.bulk_get(f"jobs/query/{job_id}").json()
            state = result.get("state")

            if state == "JobComplete":
                return
            if state in ("Failed", "Aborted"):
                raise TopologyExtractionError(
                    f"[{object_name}] Bulk query job {job_id} failed with state: {state}"
                )

            time.sleep(POLL_INTERVAL_SECONDS)

        raise TopologyExtractionError(f"[{object_name}] Bulk query job timed out after {MAX_POLL_ATTEMPTS} polls")

    def _build_phase_a_soql(
        self,
        object_name: str,
        thin_fields: list[str],
        explicit_filter: str,
        node: dict,
        scope_ids: list[str],
    ) -> str:
        filter_text = (explicit_filter or "").strip()
        scope_clause = ""

        if node.get("direction") == "child":
            if not scope_ids:
                return ""
            scope_clause = self._build_in_clause(node.get("via_field"), scope_ids)
        elif node.get("direction") == "parent":
            if not scope_ids:
                return ""
            scope_clause = self._build_in_clause("Id", scope_ids)

        merged_filter = self._compose_query_filter(scope_clause, filter_text)
        fields_clause = ", ".join(dict.fromkeys(thin_fields))
        soql = f"SELECT {fields_clause} FROM {object_name}"
        if merged_filter:
            soql += f" {merged_filter}"
        return soql

    def _build_in_clause(self, field_name: str, values: list[str]) -> str:
        if not field_name or not values:
            return ""
        escaped = [value.replace("\\", "\\\\").replace("'", "\\'") for value in values if value]
        if not escaped:
            return ""
        joined = "', '".join(escaped)
        return f"{field_name} IN ('{joined}')"

    def _compose_query_filter(self, scope_clause: str, explicit_filter: str) -> str:
        exp_where, exp_tail = split_query_filter(explicit_filter)
        where_parts = [part for part in (scope_clause, exp_where) if part]

        merged = ""
        if where_parts:
            merged = "WHERE " + " AND ".join(f"({part})" for part in where_parts)
        if exp_tail:
            merged = f"{merged} {exp_tail}".strip()
        return merged

    def _summarize_soql(self, soql: str) -> str:
        text = (soql or "").strip()
        text = re.sub(r"(?is)^SELECT\s+.+?\s+FROM\s+", "SELECT Id FROM ", text, count=1)
        text = self._replace_in_clause_values(text)
        return " ".join(text.split())

    def _replace_in_clause_values(self, text: str) -> str:
        pattern = re.compile(r"\b([A-Za-z0-9_.]+)\s+IN\s+\((.*?)\)", re.IGNORECASE)

        def repl(match):
            field_name = match.group(1)
            raw_values = match.group(2).strip()
            count = raw_values.count("','") + 1 if raw_values else 0
            return f"{field_name} IN (<{count} IDs>)"

        return pattern.sub(repl, text)

    def _chunk(self, values: list[str], size: int) -> list[list[str]]:
        return [values[i:i + size] for i in range(0, len(values), size)]
