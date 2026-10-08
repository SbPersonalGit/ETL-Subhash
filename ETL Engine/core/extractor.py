"""
Extractor — pulls data from source Salesforce org using Bulk API 2.0.

Flow:
  1. Create a Bulk API 2.0 query job
  2. Poll until job is complete
  3. Stream CSV results in pages → write each page as a chunk to Stager
  4. Track watermark for incremental extraction support

Bulk API 2.0 handles up to 100M rows/day per org.
We stream in pages (locator-based) so memory stays bounded.
"""

import time
import io
import logging
import re
import polars as pl
from datetime import datetime, timezone
from pathlib import Path
from core.connector import SFConnector
from core.polars_coerce import coerce_dataframe_columns_to_utf8
from core.stager import Stager

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
MAX_POLL_ATTEMPTS = 360          # 30 minutes max wait
WATERMARK_FILE = Path(__file__).parent.parent / "state" / "watermarks.json"


class ExtractionError(Exception):
    pass


class Extractor:
    def __init__(self, connector: SFConnector, stager: Stager):
        self.connector = connector
        self.stager = stager

    # ------------------------------------------------------------------ #
    #  Main extract entry point                                           #
    # ------------------------------------------------------------------ #

    def extract(
        self,
        object_name: str,
        soql: str,
        scope_rules: list[dict] | None = None,
        overwrite: bool = True,
    ) -> int:
        """
        Extract records for object_name using the given SOQL query.
        Writes raw chunks to staging.
        Returns total records extracted.
        """
        if overwrite:
            self.stager.drop_table(object_name, "raw")

        if scope_rules:
            soql = self._apply_scope_rules(object_name, soql, scope_rules)
        logger.info(f"[{object_name}] Starting extraction | SOQL: {self._summarize_soql(soql)}")

        job_id = self._create_query_job(soql)
        logger.info(f"[{object_name}] Bulk query job created: {job_id}")

        self._wait_for_job(object_name, job_id)

        total = self._stream_results(object_name, job_id)
        self.stager.deduplicate_object(object_name, "raw")
        logger.info(f"[{object_name}] Extraction complete | {total:,} records")

        self._save_watermark(object_name)
        return total

    def _apply_scope_rules(self, object_name: str, soql: str, scope_rules: list[dict]) -> str:
        clauses = []
        for rule in scope_rules:
            source_object = rule.get("source_object")
            via_field = rule.get("via_field")
            mode = rule.get("mode")

            if not source_object:
                continue

            if mode == "child":
                ids = self.stager.get_distinct_values(source_object, "Id", "raw")
                if ids:
                    clauses.append(self._build_in_clause(via_field, ids))
            elif mode == "parent" and via_field:
                ids = self.stager.get_distinct_values(source_object, via_field, "raw")
                if ids:
                    clauses.append(self._build_in_clause("Id", ids))

        clauses = [c for c in clauses if c]
        if not clauses:
            return soql
        combined = " OR ".join(f"({c})" for c in clauses)
        return self._inject_where_clause(soql, combined)

    def _build_in_clause(self, field_name: str, values: list[str]) -> str:
        if not field_name or not values:
            return ""
        escaped = [v.replace("\\", "\\\\").replace("'", "\\'") for v in values if v]
        if not escaped:
            return ""
        joined = "', '".join(escaped)
        return f"{field_name} IN ('{joined}')"

    def _inject_where_clause(self, soql: str, clause: str) -> str:
        split_points = [idx for idx in (
            self._find_top_level_keyword(soql, " ORDER BY "),
            self._find_top_level_keyword(soql, " LIMIT "),
            self._find_top_level_keyword(soql, " OFFSET "),
        ) if idx != -1]
        split_at = min(split_points) if split_points else len(soql)
        head = soql[:split_at].rstrip()
        tail = soql[split_at:]

        if self._find_top_level_keyword(soql[:split_at], " WHERE ") != -1:
            return f"{head} AND ({clause}){tail}"
        return f"{head} WHERE {clause}{tail}"

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

    def _find_top_level_keyword(self, soql: str, keyword: str) -> int:
        upper = soql.upper()
        target = keyword.upper()
        depth = 0
        in_quote = False
        i = 0

        while i <= len(soql) - len(target):
            char = soql[i]

            if char == "'" and (i == 0 or soql[i - 1] != "\\"):
                in_quote = not in_quote
                i += 1
                continue

            if not in_quote:
                if char == "(":
                    depth += 1
                elif char == ")" and depth > 0:
                    depth -= 1
                elif depth == 0 and upper[i:i + len(target)] == target:
                    return i

            i += 1

        return -1

    # ------------------------------------------------------------------ #
    #  Bulk API 2.0 — query job lifecycle                                 #
    # ------------------------------------------------------------------ #

    def _create_query_job(self, soql: str) -> str:
        payload = {
            "operation": "query",
            "query": soql,
        }
        result = self.connector.bulk_post("jobs/query", payload)
        return result["id"]

    def _wait_for_job(self, object_name: str, job_id: str):
        for attempt in range(MAX_POLL_ATTEMPTS):
            result = self.connector.bulk_get(f"jobs/query/{job_id}").json()
            state = result.get("state")

            if state == "JobComplete":
                return
            if state in ("Failed", "Aborted"):
                raise ExtractionError(
                    f"[{object_name}] Bulk query job {job_id} failed with state: {state}"
                )

            logger.debug(f"[{object_name}] Job {job_id} state={state} | waiting...")
            time.sleep(POLL_INTERVAL_SECONDS)

        raise ExtractionError(f"[{object_name}] Bulk query job timed out after {MAX_POLL_ATTEMPTS} polls")

    def _stream_results(self, object_name: str, job_id: str) -> int:
        """
        Paginate through Bulk API 2.0 results using locator.
        Each page is parsed as CSV → Polars DataFrame → written to staging.
        """
        total = 0
        locator = None

        while True:
            resp = self.connector.bulk_get_csv(f"jobs/query/{job_id}/results", locator=locator)
            csv_text = self.connector.decode_csv_response(resp)

            if not csv_text.strip():
                break

            df = pl.read_csv(io.StringIO(csv_text), infer_schema_length=1000, null_values=[""])
            if df.is_empty():
                break

            # Cast all columns to string to avoid type conflicts between pages
            df = coerce_dataframe_columns_to_utf8(df)

            self.stager.write_raw_chunk(object_name, df)
            total += len(df)
            logger.info(f"[{object_name}] Streamed {total:,} records so far...")

            # Check for next page locator
            locator = resp.headers.get("Sforce-Locator")
            if not locator or locator == "null":
                break

        return total

    # ------------------------------------------------------------------ #
    #  Incremental extract support (watermark)                            #
    # ------------------------------------------------------------------ #

    def _save_watermark(self, object_name: str):
        import json
        watermarks = {}
        if WATERMARK_FILE.exists():
            watermarks = json.loads(WATERMARK_FILE.read_text())
        watermarks[object_name] = datetime.now(timezone.utc).isoformat()
        WATERMARK_FILE.write_text(json.dumps(watermarks, indent=2))

    def get_watermark(self, object_name: str) -> str | None:
        import json
        if not WATERMARK_FILE.exists():
            return None
        watermarks = json.loads(WATERMARK_FILE.read_text())
        return watermarks.get(object_name)

    def build_incremental_filter(self, object_name: str, date_field: str = "LastModifiedDate") -> str:
        """
        Returns a WHERE clause for incremental extraction based on last run watermark.
        Returns empty string if no watermark exists (full extract).
        """
        watermark = self.get_watermark(object_name)
        if not watermark:
            return ""
        return f"WHERE {date_field} > {watermark}"
