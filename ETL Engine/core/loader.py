"""
Loader — loads transformed data into target Salesforce org using Bulk API 2.0.

Flow:
  1. Read xfm chunks from Stager
  2. Serialize each chunk to CSV
  3. Create Bulk API 2.0 ingest job (upsert / insert)
  4. Upload CSV batches
  5. Close job and poll for completion
  6. Collect successful results → populate ID Mapper
  7. Collect failed results → write to dead letter queue

Dead letter queue: logs/<run>/load_failed/{ObjectName}_failed_{timestamp}.csv
"""

import io
import os
import time
import logging
import csv
import polars as pl
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence, TypeVar
from core.connector import SFConnector
from core.stager import Stager
from core.id_mapper import IDMapper
from core.exclusion_rules import ExclusionRules
from core.field_rules import (
    MIGRATABLE_AUDIT_FIELDS,
    is_polymorphic_reference_field,
    resolve_polymorphic_owner_reference_object,
)
from core.salesforce_ids import (
    restore_salesforce_id_casing,
    restore_salesforce_ids_in_frame,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _chunked(values: Sequence[_T], size: int) -> Iterator[list[_T]]:
    if size <= 0:
        raise ValueError(f"chunk size must be positive, got {size}")
    for start in range(0, len(values), size):
        yield list(values[start : start + size])

POLL_INTERVAL_SECONDS = 5
MAX_POLL_ATTEMPTS = 720          # 60 minutes max wait
LOGS_DIR = Path(__file__).parent.parent / "logs"
DEFAULT_LOAD_BATCH_SIZE = 50_000
MIN_LOAD_BATCH_SIZE = 100
MAX_LOAD_BATCH_SIZE = 100_000
BATCH_UPLOAD_SIZE = DEFAULT_LOAD_BATCH_SIZE  # records per CSV upload to Bulk API
MATCH_QUERY_BATCH_SIZE = 200     # source values per target match query (SOQL IN clause)


def resolve_load_batch_size(value: object | None = None) -> int:
    """Return a valid Bulk load batch size from an explicit value, env, or default."""
    raw = value
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get("ETL_LOAD_BATCH_SIZE") or DEFAULT_LOAD_BATCH_SIZE
    try:
        size = int(str(raw).strip().replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("Load batch size must be a whole number") from exc
    if not MIN_LOAD_BATCH_SIZE <= size <= MAX_LOAD_BATCH_SIZE:
        raise ValueError(
            f"Load batch size must be between {MIN_LOAD_BATCH_SIZE} and {MAX_LOAD_BATCH_SIZE}"
        )
    return size


class LoadError(Exception):
    pass


class LoadResult:
    def __init__(self):
        self.total_processed = 0
        self.success_count = 0
        self.success_file: str | None = None
        self.failed_count = 0
        self.failed_file: str | None = None
        self.blocked_count = 0
        self.blocked_file: str | None = None

    def __repr__(self):
        return (
            f"LoadResult(processed={self.total_processed}, "
            f"success={self.success_count}, failed={self.failed_count}, blocked={self.blocked_count})"
        )


class Loader:
    def __init__(
        self,
        connector: SFConnector,
        stager: Stager,
        id_mapper: IDMapper | None = None,
        log_root: Path | None = None,
        batch_size: int | None = None,
    ):
        self.connector = connector
        self.stager = stager
        self.id_mapper = id_mapper
        self.exclusion_rules = ExclusionRules()
        self.log_root = Path(log_root) if log_root else LOGS_DIR
        self.log_root.mkdir(parents=True, exist_ok=True)
        self.batch_size = resolve_load_batch_size(batch_size)
        self._describe_cache: dict[str, dict] = {}
        self.last_update_failed_count = 0
        self.last_update_failed_file: str | None = None
        self.last_update_failed_ids: set[str] = set()

    def _stage_log_dir(self, stage_name: str) -> Path:
        stage_dir = self.log_root / stage_name
        stage_dir.mkdir(parents=True, exist_ok=True)
        return stage_dir

    def _publish_migration_result(self, path: Path, object_name: str) -> str:
        """Copy a load result into Migration Result / {Object API} / {run}."""
        from core.workspace_layout import MIGRATION_RESULT_DIR, publish_file

        published = publish_file(path, object_api=object_name, kind="migration")
        root = MIGRATION_RESULT_DIR.resolve()
        for dest in published:
            resolved = dest.resolve()
            if resolved == root or root in resolved.parents:
                return str(resolved)
        return str(path)

    # ------------------------------------------------------------------ #
    #  Main load entry point                                              #
    # ------------------------------------------------------------------ #

    def load(
        self,
        object_name: str,
        external_id_field: str,
        operation: str = "upsert",
        dry_run: bool = False,
        pass_number: int = 1,
        attempt_number: int = 1,
        on_progress=None,
    ) -> LoadResult:
        """
        Load all xfm-staged records for object_name into the target org.
        operation: 'upsert', 'insert', 'update'
        Returns a LoadResult with success/failure counts.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        logger.info(
            f"[{object_name}] Starting load | pass={pass_number} | attempt={attempt_number} | "
            f"operation={operation} | dry_run={dry_run} | batch_size={self.batch_size}"
        )
        self._validate_operation_metadata(object_name, external_id_field, operation)
        strategy = self._decide_load_strategy(object_name, external_id_field, operation)
        writable_fields = self._get_writable_fields(object_name, external_id_field)
        if operation != "insert":
            logger.warning(
                f"[{object_name}] Selected audit fields are preserved for records created by "
                f"this {operation}; Salesforce cannot rewrite audit fields on existing records."
            )

        # PricebookEntry: Salesforce requires standard prices to exist before custom prices
        if object_name == "PricebookEntry":
            self._ensure_standard_prices(writable_fields)

        return self._load_chunks(
            object_name,
            self.stager.read_xfm_chunks(object_name, chunk_size=self.batch_size),
            external_id_field,
            operation,
            dry_run,
            writable_fields,
            timestamp,
            pass_number,
            attempt_number,
            strategy,
            on_progress=on_progress,
        )

    def load_dataframe(
        self,
        object_name: str,
        df: pl.DataFrame,
        external_id_field: str,
        operation: str = "upsert",
        dry_run: bool = False,
        pass_number: int = 1,
        attempt_number: int = 1,
        on_progress=None,
    ) -> LoadResult:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._validate_operation_metadata(object_name, external_id_field, operation)
        strategy = self._decide_load_strategy(object_name, external_id_field, operation)
        writable_fields = self._get_writable_fields(object_name, external_id_field)
        return self._load_chunks(
            object_name,
            [df],
            external_id_field,
            operation,
            dry_run,
            writable_fields,
            timestamp,
            pass_number,
            attempt_number,
            strategy,
            on_progress=on_progress,
        )

    @staticmethod
    def _append_csv(path: Path, frame: pl.DataFrame, fieldnames: list[str] | None) -> list[str]:
        """Append rows immediately so failed batches can be downloaded mid-run."""
        if frame.is_empty():
            return fieldnames or []
        names = list(fieldnames or frame.columns)
        for column in frame.columns:
            if column not in names:
                names.append(column)
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a" if not new_file else "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            for row in frame.to_dicts():
                writer.writerow({name: row.get(name, "") for name in names})
        return names

    def _emit_load_progress(self, on_progress, result: LoadResult, failed_delta: int = 0) -> None:
        if not on_progress:
            return
        on_progress({
            "success_count": result.success_count,
            "failed_count": result.failed_count,
            "blocked_count": result.blocked_count,
            "failed_file": result.failed_file,
            "success_file": result.success_file,
            "blocked_file": result.blocked_file,
            "failed_delta": failed_delta,
        })

    def _load_chunks(
        self,
        object_name: str,
        chunks,
        external_id_field: str,
        operation: str,
        dry_run: bool,
        writable_fields: set[str] | None,
        timestamp: str,
        pass_number: int,
        attempt_number: int,
        strategy: str = "upsert",
        on_progress=None,
    ) -> LoadResult:
        result = LoadResult()
        all_success: list[pl.DataFrame] = []
        all_failed: list[pl.DataFrame] = []
        all_blocked: list[pl.DataFrame] = []
        failed_path = self._stage_log_dir("load_failed") / f"{object_name}_failed_{timestamp}.csv"
        failed_fields: list[str] | None = None

        for chunk in chunks:
            try:
                chunk_result, success_df, failed_df, blocked_df = self._process_chunk(
                    object_name,
                    chunk,
                    operation,
                    external_id_field,
                    writable_fields,
                    dry_run,
                    strategy,
                )
            except Exception as e:
                if not dry_run and not chunk.is_empty():
                    crash_failed_df = self._build_crash_failed_df(chunk, str(e))
                    self._append_csv(failed_path, crash_failed_df, failed_fields)
                    result.failed_count += len(crash_failed_df)
                    result.total_processed += len(crash_failed_df)
                    result.failed_file = self._publish_migration_result(failed_path, object_name)
                    logger.error(
                        f"[{object_name}] Load crashed while processing a chunk - "
                        f"wrote {len(crash_failed_df):,} record(s) to {failed_path.name}"
                    )
                    self._emit_load_progress(on_progress, result, failed_delta=len(crash_failed_df))
                    raise LoadError(
                        f"[{object_name}] Load crashed before failed rows could be collected. "
                        f"Source payload written to {failed_path.name}. Original error: {e}"
                    ) from e
                raise
            result.total_processed += chunk_result.total_processed
            result.success_count += chunk_result.success_count
            result.failed_count += chunk_result.failed_count
            result.blocked_count += chunk_result.blocked_count

            failed_delta = 0
            if not failed_df.is_empty():
                all_failed.append(failed_df)
                if not dry_run:
                    failed_fields = self._append_csv(failed_path, failed_df, failed_fields)
                    result.failed_file = str(failed_path)
                    failed_delta = len(failed_df)
                    logger.warning(
                        f"[{object_name}] Failed batch written | {failed_delta:,} records | "
                        f"running failed={result.failed_count:,} | {failed_path.name}"
                    )
            if not success_df.is_empty():
                all_success.append(success_df)
            if not blocked_df.is_empty():
                all_blocked.append(blocked_df)
            if not dry_run:
                self._emit_load_progress(on_progress, result, failed_delta=failed_delta)

        if dry_run:
            logger.info(
                f"[{object_name}] DRY RUN — would load {result.success_count:,} records "
                f"| blocked={result.blocked_count:,}"
            )
            return result

        if all_failed:
            pl.concat(all_failed, how="diagonal_relaxed").write_csv(str(failed_path))
            result.failed_file = self._publish_migration_result(failed_path, object_name)
            logger.warning(
                f"[{object_name}] {result.failed_count:,} records failed — "
                f"see {failed_path.name}"
            )

        if all_success:
            success_path = self._stage_log_dir("load_success") / f"{object_name}_success_{timestamp}.csv"
            pl.concat(all_success, how="diagonal_relaxed").write_csv(str(success_path))
            result.success_file = self._publish_migration_result(success_path, object_name)
            logger.info(f"[{object_name}] Successful results written to {success_path.name}")

        if all_blocked:
            blocked_path = self._stage_log_dir("load_blocked") / f"{object_name}_blocked_{timestamp}.csv"
            pl.concat(all_blocked, how="diagonal_relaxed").write_csv(str(blocked_path))
            result.blocked_file = self._publish_migration_result(blocked_path, object_name)
            logger.warning(
                f"[{object_name}] {result.blocked_count:,} records blocked by unresolved dependencies — "
                f"see {blocked_path.name}"
            )

        logger.info(
            f"[{object_name}] Load complete | "
            f"success={result.success_count:,} | failed={result.failed_count:,} | blocked={result.blocked_count:,}"
        )
        return result

    @staticmethod
    def _build_crash_failed_df(chunk: pl.DataFrame, error_text: str) -> pl.DataFrame:
        crash_df = chunk.clone()
        if "sf__Id" not in crash_df.columns:
            crash_df = crash_df.with_columns(pl.lit(None).cast(pl.Utf8).alias("sf__Id"))
        crash_df = crash_df.with_columns(
            pl.lit(error_text).cast(pl.Utf8).alias("sf__Error")
        )
        return crash_df

    def _validate_operation_metadata(self, object_name: str, external_id_field: str, operation: str):
        """
        Validate that the external_id_field exists on the target. The External
        ID flag itself is no longer a hard requirement here — `_decide_load_strategy`
        decides whether to take the Bulk upsert path (requires the flag) or the
        match-by path (works for plain text fields, e.g. Knowledge__kav where
        Salesforce disallows the External ID flag).
        """
        if operation != "upsert":
            return

        try:
            describe = self._describe_object(object_name)
        except Exception as e:
            logger.warning(
                f"[{object_name}] Could not validate upsert external ID field '{external_id_field}' "
                f"against target metadata: {e}"
            )
            return

        target_field = next(
            (f for f in describe.get("fields", []) if f.get("name") == external_id_field),
            None,
        )
        if not target_field:
            raise LoadError(
                f"[{object_name}] Upsert external ID field '{external_id_field}' does not exist on the target object."
            )

    def _decide_load_strategy(
        self, object_name: str, external_id_field: str, operation: str
    ) -> str:
        """
        Decide between the Bulk-upsert path and the match-by-text-field path.

        Returns one of:
          - "upsert"   — the field is flagged External ID + Unique on target;
                        existing fast path applies.
          - "match_by" — operation was 'upsert' but the configured field cannot
                        support Bulk upsert (no External ID flag, e.g. Knowledge__kav).
                        We fall back to query-target → split insert/update.
          - operation  — for non-upsert operations, the operator's choice is
                        respected verbatim (returned as-is).
        """
        if operation != "upsert":
            return operation

        is_external_id, is_unique = self._field_external_id_capable(
            object_name, external_id_field
        )
        if is_external_id and is_unique:
            logger.info(
                f"[{object_name}] external_id_field='{external_id_field}' confirmed External ID + Unique "
                "on target — using Bulk upsert path"
            )
            return "upsert"

        reasons: list[str] = []
        if not is_external_id:
            reasons.append("not flagged External ID")
        if not is_unique:
            reasons.append("not flagged Unique")
        logger.info(
            f"[{object_name}] external_id_field='{external_id_field}' is "
            f"{', '.join(reasons) or 'unsupported'} on target — using match-by load path "
            "(query target → split insert/update)"
        )
        return "match_by"

    def _field_external_id_capable(
        self, object_name: str, field_name: str
    ) -> tuple[bool, bool]:
        """
        Return (is_external_id, is_unique) for the field on the target object.
        Falls back to (True, True) if describe fails — preserves the legacy
        behaviour of attempting Bulk upsert and surfacing the Salesforce error.
        """
        try:
            describe = self._describe_object(object_name)
        except Exception as e:
            logger.warning(
                f"[{object_name}] Could not check External ID metadata for '{field_name}': {e}. "
                "Defaulting to upsert path."
            )
            return True, True

        target_field = next(
            (f for f in describe.get("fields", []) if f.get("name") == field_name),
            None,
        )
        if not target_field:
            return False, False
        return (
            bool(target_field.get("externalId", False)),
            bool(target_field.get("unique", False)),
        )

    def _relationship_reference_target(
        self,
        relationship_name: str,
        relationship_field: dict,
    ) -> str | None:
        ref_objects = [r for r in (relationship_field.get("referenceTo") or []) if r]
        if len(ref_objects) == 1:
            return ref_objects[0]
        return resolve_polymorphic_owner_reference_object(relationship_name, ref_objects)

    def _parse_relationship_reference_column(
        self,
        column_name: str,
    ) -> tuple[str | None, str, str] | None:
        if "." not in column_name:
            return None
        left, ref_field_name = column_name.split(".", 1)
        object_type = None
        relationship_name = left
        if ":" in left:
            object_type, relationship_name = left.split(":", 1)
        if not relationship_name or not ref_field_name:
            return None
        return object_type, relationship_name, ref_field_name

    def _validate_payload_chunk_metadata(
        self,
        object_name: str,
        chunk: pl.DataFrame,
        operation: str,
        external_id_field: str,
    ):
        describe = self._describe_object(object_name)
        fields = describe.get("fields", [])
        field_map = {f.get("name"): f for f in fields}
        relationship_map = {
            f.get("relationshipName"): f
            for f in fields
            if f.get("relationshipName")
        }

        if operation == "upsert" and external_id_field not in chunk.columns:
            raise LoadError(
                f"[{object_name}] Upsert payload is missing the configured external ID column '{external_id_field}'."
            )

        for column_name in chunk.columns:
            if not self._is_relationship_reference_column(column_name):
                continue
            parsed = self._parse_relationship_reference_column(column_name)
            if not parsed:
                raise LoadError(
                    f"[{object_name}] Relationship reference column '{column_name}' is invalid."
                )
            object_type, relationship_name, ref_field_name = parsed
            relationship_field = relationship_map.get(relationship_name)
            if not relationship_field:
                raise LoadError(
                    f"[{object_name}] Relationship reference column '{column_name}' is invalid because "
                    f"'{relationship_name}' is not a known relationship name on the target object."
                )

            ref_objects = [r for r in (relationship_field.get("referenceTo") or []) if r]
            ref_object = self._relationship_reference_target(relationship_name, relationship_field)
            if not ref_object:
                raise LoadError(
                    f"[{object_name}] Relationship reference column '{column_name}' is ambiguous because "
                    f"'{relationship_name}' does not point to exactly one target object."
                )
            if object_type and object_type != ref_object:
                raise LoadError(
                    f"[{object_name}] Relationship reference column '{column_name}' is invalid because "
                    f"typed object '{object_type}' does not match resolved target '{ref_object}'."
                )

            ref_describe = self._describe_object(ref_object)
            ref_field = next(
                (f for f in ref_describe.get("fields", []) if f.get("name") == ref_field_name),
                None,
            )
            if not ref_field:
                raise LoadError(
                    f"[{object_name}] Relationship reference column '{column_name}' is invalid because "
                    f"field '{ref_field_name}' does not exist on referenced object '{ref_object}'."
                )
            if not ref_field.get("externalId", False):
                raise LoadError(
                    f"[{object_name}] Relationship reference column '{column_name}' is invalid because "
                    f"'{ref_object}.{ref_field_name}' is not marked as an External ID on the target org."
                )

        required_fields = [
            f.get("name")
            for f in fields
            if (not f.get("nillable", True)) and f.get("createable", False)
        ]
        ignored_required_fields = self.exclusion_rules.get_ignored_required_fields(object_name)
        for field_name in required_fields:
            if field_name == "Id":
                continue
            if field_name in ignored_required_fields:
                logger.info(
                    f"[{object_name}] Skipping required-field preflight for '{field_name}' due to exclusion rules."
                )
                continue
            if field_name in chunk.columns:
                null_count = chunk[field_name].null_count()
                if null_count > 0:
                    logger.warning(
                        f"[{object_name}] Payload field '{field_name}' has {null_count} null value(s). "
                        "Salesforce will decide whether those rows are acceptable."
                    )
                continue

            relationship_name = field_map.get(field_name, {}).get("relationshipName")
            if relationship_name and any(col.startswith(f"{relationship_name}.") for col in chunk.columns):
                continue

            logger.info(
                f"[{object_name}] Payload is missing field '{field_name}'. "
                "Salesforce will decide whether it is required for this operation."
            )

    def _describe_object(self, object_name: str) -> dict:
        if object_name not in self._describe_cache:
            self._describe_cache[object_name] = self.connector.describe_object(object_name)
        return self._describe_cache[object_name]

    def _process_chunk(
        self,
        object_name: str,
        chunk: pl.DataFrame,
        operation: str,
        external_id_field: str,
        writable_fields: set[str] | None,
        dry_run: bool,
        strategy: str = "upsert",
    ) -> tuple[LoadResult, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
        if strategy == "match_by":
            return self._process_chunk_match_by(
                object_name, chunk, external_id_field, writable_fields, dry_run
            )

        result = LoadResult()
        success_df = pl.DataFrame()
        failed_df = pl.DataFrame()
        blocked_df = pl.DataFrame()

        chunk = self._prepare_upload_chunk(object_name, chunk, writable_fields)
        chunk, blocked_df = self._split_blocked_dependency_rows(object_name, chunk)
        self._validate_payload_chunk_metadata(object_name, chunk, operation, external_id_field)

        result.total_processed = len(chunk) + len(blocked_df)
        result.blocked_count = len(blocked_df)
        if dry_run:
            result.success_count = len(chunk)
            return result, success_df, failed_df, blocked_df

        if chunk.is_empty():
            return result, success_df, failed_df, blocked_df

        success_df, failed_df = self._run_bulk_job(
            object_name, operation, external_id_field, chunk
        )
        result.success_count = len(success_df)
        result.failed_count = len(failed_df)

        if self.id_mapper and not success_df.is_empty() and "sf__Id" in success_df.columns:
            self._record_id_mappings(object_name, success_df, chunk, external_id_field)

        return result, success_df, failed_df, blocked_df

    def _process_chunk_match_by(
        self,
        object_name: str,
        chunk: pl.DataFrame,
        external_id_field: str,
        writable_fields: set[str] | None,
        dry_run: bool,
    ) -> tuple[LoadResult, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
        """
        Match-by-text-field load path. Used when the configured external_id_field
        exists on the target but is not flagged External ID (e.g. Knowledge__kav,
        where Salesforce disallows the flag).

        Steps:
          1. Prepare + filter blocked rows (same as upsert path).
          2. Query target on external_id_field for matches against the chunk's
             source values (batched IN clauses).
          3. Annotate chunk with target Id; split into UPDATE (matched) and
             INSERT (unmatched) sub-payloads.
          4. Run two Bulk jobs: update-by-Id and insert.
          5. Populate IDMapper from both — known mappings for updates,
             returned sf__Id for inserts.
        """
        result = LoadResult()
        success_frames: list[pl.DataFrame] = []
        failed_frames: list[pl.DataFrame] = []
        blocked_df = pl.DataFrame()

        chunk = self._prepare_upload_chunk(object_name, chunk, writable_fields)
        chunk, blocked_df = self._split_blocked_dependency_rows(object_name, chunk)
        if external_id_field not in chunk.columns:
            raise LoadError(
                f"[{object_name}] match-by payload is missing the configured key column "
                f"'{external_id_field}' — needed to match existing target records."
            )
        self._validate_payload_chunk_metadata(
            object_name, chunk, "update", external_id_field
        )

        result.total_processed = len(chunk) + len(blocked_df)
        result.blocked_count = len(blocked_df)
        if dry_run:
            result.success_count = len(chunk)
            return result, pl.DataFrame(), pl.DataFrame(), blocked_df

        if chunk.is_empty():
            return result, pl.DataFrame(), pl.DataFrame(), blocked_df

        source_values = (
            chunk.get_column(external_id_field)
            .cast(pl.Utf8)
            .drop_nulls()
            .unique()
            .to_list()
        )
        target_matches = self._query_target_match_ids(
            object_name, external_id_field, source_values
        )
        logger.info(
            f"[{object_name}] match-by: {len(target_matches):,} of {len(source_values):,} "
            "source value(s) found on target"
        )

        update_df, insert_df = self._split_match_by_chunk(
            chunk, external_id_field, target_matches
        )

        success_total = 0
        failed_total = 0

        if not update_df.is_empty():
            update_payload = update_df.drop(external_id_field)
            update_payload = self._restrict_payload_columns(
                object_name,
                update_payload,
                self._get_updateable_fields(object_name),
                keep_columns={"Id"},
                operation_label="match-by update",
            )
            success_df, failed_df = self._run_bulk_job(
                object_name, "update", "Id", update_payload
            )
            success_total += len(success_df)
            if not success_df.is_empty():
                success_frames.append(success_df)
            failed_total += len(failed_df)
            if not failed_df.is_empty():
                failed_frames.append(failed_df)
            if self.id_mapper and not success_df.is_empty():
                self._record_match_by_update_mappings(
                    object_name, update_df, external_id_field, success_df
                )

        if not insert_df.is_empty():
            insert_payload = self._restrict_payload_columns(
                object_name,
                insert_df,
                self._get_createable_fields(object_name),
                keep_columns={external_id_field},
                operation_label="match-by insert",
            )
            success_df, failed_df = self._run_bulk_job(
                object_name, "insert", "Id", insert_payload
            )
            success_total += len(success_df)
            if not success_df.is_empty():
                success_frames.append(success_df)
            failed_total += len(failed_df)
            if not failed_df.is_empty():
                failed_frames.append(failed_df)
            if self.id_mapper and not success_df.is_empty() and "sf__Id" in success_df.columns:
                self._record_id_mappings(
                    object_name, success_df, insert_df, external_id_field
                )

        result.success_count = success_total
        result.failed_count = failed_total
        combined_failed = (
            pl.concat(failed_frames, how="diagonal_relaxed")
            if failed_frames
            else pl.DataFrame()
        )
        combined_success = (
            pl.concat(success_frames, how="diagonal_relaxed")
            if success_frames else pl.DataFrame()
        )
        return result, combined_success, combined_failed, blocked_df

    def _run_bulk_job(
        self,
        object_name: str,
        operation: str,
        external_id_field: str,
        payload: pl.DataFrame,
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        """
        Run a single Bulk API 2.0 ingest job to completion. Returns (success_df, failed_df).
        Centralised so both the upsert and match-by paths share the same lifecycle.
        """
        job_id = self._create_ingest_job(object_name, operation, external_id_field)
        self._upload_csv(job_id, payload)
        self._close_job(job_id)
        self._wait_for_job(object_name, job_id)
        return self._collect_results(object_name, job_id, payload)

    def _query_target_match_ids(
        self,
        object_name: str,
        external_id_field: str,
        source_values: list[str],
    ) -> dict[str, str]:
        """
        Query the target org for existing records whose `external_id_field`
        matches one of `source_values`. Returns a {source_value: target_id} map.

        Batched at MATCH_QUERY_BATCH_SIZE to keep individual SOQL queries
        comfortably under Salesforce's 20K-character limit. Duplicates on the
        target side are logged and resolved by keeping the first match.
        """
        if not source_values:
            return {}

        matches: dict[str, str] = {}
        duplicates: list[str] = []

        for batch in _chunked(source_values, MATCH_QUERY_BATCH_SIZE):
            safe_batch = [self._escape_soql_string(v) for v in batch if v]
            if not safe_batch:
                continue
            in_clause = ", ".join(f"'{v}'" for v in safe_batch)
            soql = (
                f"SELECT Id, {external_id_field} FROM {object_name} "
                f"WHERE {external_id_field} IN ({in_clause})"
            )
            try:
                rows = self.connector.query(soql)
            except Exception as e:
                logger.error(
                    f"[{object_name}] match-by target query failed for batch of "
                    f"{len(safe_batch)} value(s): {e}"
                )
                raise

            for row in rows:
                key = row.get(external_id_field)
                target_id = row.get("Id")
                if key is None or target_id is None:
                    continue
                key_str = str(key)
                if key_str in matches and matches[key_str] != target_id:
                    duplicates.append(key_str)
                    continue
                matches[key_str] = target_id

        if duplicates:
            unique_dupes = sorted(set(duplicates))
            logger.warning(
                f"[{object_name}] match-by: {len(unique_dupes)} source value(s) matched multiple "
                f"target records on '{external_id_field}'. Keeping first match each. "
                f"Examples: {unique_dupes[:5]}"
            )
        return matches

    @staticmethod
    def _escape_soql_string(value: str) -> str:
        """Escape SOQL string literal characters per Salesforce's rules."""
        return str(value).replace("\\", "\\\\").replace("'", "\\'")

    def _split_match_by_chunk(
        self,
        chunk: pl.DataFrame,
        external_id_field: str,
        target_matches: dict[str, str],
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        """
        Annotate chunk with the matched target Id (left-join on external_id_field)
        and split into (update_df, insert_df).

        update_df has an "Id" column populated and retains the external_id_field
        column for IDMapper recording. insert_df has neither an "Id" nor any
        match metadata.
        """
        if not target_matches:
            return pl.DataFrame(schema=chunk.schema), chunk

        match_df = pl.DataFrame(
            {
                external_id_field: list(target_matches.keys()),
                "__matched_target_id__": list(target_matches.values()),
            },
            schema={external_id_field: pl.Utf8, "__matched_target_id__": pl.Utf8},
        )
        annotated = chunk.with_columns(
            pl.col(external_id_field).cast(pl.Utf8)
        ).join(match_df, on=external_id_field, how="left")

        update_df = (
            annotated.filter(pl.col("__matched_target_id__").is_not_null())
            .rename({"__matched_target_id__": "Id"})
        )
        insert_df = (
            annotated.filter(pl.col("__matched_target_id__").is_null())
            .drop("__matched_target_id__")
        )
        return update_df, insert_df

    def _record_match_by_update_mappings(
        self,
        object_name: str,
        update_df: pl.DataFrame,
        external_id_field: str,
        success_df: pl.DataFrame,
    ) -> None:
        """
        For the match-by update branch we already know source→target before
        the Bulk job runs, so we don't need to read sf__Id back. We do still
        intersect with the success rows so failed updates don't contaminate
        the IDMapper (Bulk update reports per-row status via sf__Id == row Id).
        """
        if "Id" not in update_df.columns:
            return

        successful_ids: set[str] = set()
        if "sf__Id" in success_df.columns:
            successful_ids = {
                str(v)
                for v in success_df.get_column("sf__Id").drop_nulls().to_list()
                if v is not None
            }

        mappings = update_df.select(
            [
                pl.col(external_id_field).cast(pl.Utf8).alias("source_id"),
                pl.col("Id").cast(pl.Utf8).alias("target_id"),
            ]
        ).filter(
            pl.col("source_id").is_not_null() & pl.col("target_id").is_not_null()
        )
        if successful_ids:
            mappings = mappings.filter(pl.col("target_id").is_in(list(successful_ids)))
        mappings = mappings.unique(subset=["source_id"], keep="last")

        if mappings.is_empty():
            return
        self.id_mapper.bulk_add_mappings(object_name, mappings)

    def _prepare_upload_chunk(
        self,
        object_name: str,
        chunk: pl.DataFrame,
        writable_fields: set[str] | None,
    ) -> pl.DataFrame:
        chunk = self._normalize_relationship_reference_columns(chunk)

        if "Id" in chunk.columns:
            chunk = chunk.drop("Id")

        if object_name == "PricebookEntry" and "UseStandardPrice" in chunk.columns:
            chunk = chunk.sort("UseStandardPrice", descending=True)

        if writable_fields:
            keep = [
                c for c in chunk.columns
                if c in writable_fields or self._is_relationship_reference_column(c)
            ]
            dropped = [
                c for c in chunk.columns
                if c not in writable_fields and not self._is_relationship_reference_column(c)
            ]
            if dropped:
                logger.warning(
                    f"[{object_name}] Dropping {len(dropped)} non-writable column(s) "
                    f"from upload payload: {dropped}"
                )
            chunk = chunk.select(keep)

        chunk = self._normalize_temporal_fields(object_name, chunk)
        chunk = self._truncate_text_fields_to_target_limits(object_name, chunk)

        if object_name == "Account":
            chunk = self._sanitize_account_payload(chunk)

        return self._resolve_relationship_reference_columns(object_name, chunk)

    def _normalize_temporal_fields(self, object_name: str, chunk: pl.DataFrame) -> pl.DataFrame:
        if chunk.is_empty():
            return chunk

        describe = self._describe_object(object_name)
        field_map = {
            f.get("name"): f
            for f in describe.get("fields", [])
            if f.get("name")
        }

        expressions: list[pl.Expr] = []
        normalized_fields: list[str] = []
        for column_name in chunk.columns:
            if self._is_relationship_reference_column(column_name):
                continue

            field_meta = field_map.get(column_name)
            field_type = (field_meta or {}).get("type", "").lower()
            if field_type == "date":
                expressions.append(self._normalized_date_expr(column_name))
                normalized_fields.append(column_name)
            elif field_type == "datetime":
                expressions.append(self._normalized_datetime_expr(column_name))
                normalized_fields.append(column_name)

        if expressions:
            chunk = chunk.with_columns(expressions)
            logger.debug(
                f"[{object_name}] Normalized temporal fields for upload: {sorted(normalized_fields)}"
            )
        return chunk

    @staticmethod
    def _normalized_date_expr(column_name: str) -> pl.Expr:
        text_expr = (
            pl.when(pl.col(column_name).is_null())
            .then(pl.lit(None))
            .otherwise(pl.col(column_name).cast(pl.Utf8).str.strip_chars())
        )
        def shaped(pattern: str, date_format: str) -> pl.Expr:
            return (
                pl.when(text_expr.str.contains(pattern))
                .then(text_expr.str.strptime(pl.Date, format=date_format, strict=False))
                .otherwise(pl.lit(None, dtype=pl.Date))
            )

        parsed_expr = pl.coalesce([
            shaped(r"^\d{4}-\d{1,2}-\d{1,2}$", "%Y-%m-%d"),
            shaped(r"^\d{4}/\d{1,2}/\d{1,2}$", "%Y/%m/%d"),
            shaped(r"^\d{1,2}/\d{1,2}/\d{4}$", "%m/%d/%Y"),
            shaped(r"^\d{1,2}-\d{1,2}-\d{4}$", "%m-%d-%Y"),
            shaped(r"^\d{1,2}/\d{1,2}/\d{2}$", "%m/%d/%y"),
            shaped(r"^\d{1,2}-\d{1,2}-\d{2}$", "%m-%d-%y"),
        ])
        return (
            pl.when(text_expr.is_null() | (text_expr == ""))
            .then(pl.lit(None))
            .otherwise(pl.when(parsed_expr.is_not_null()).then(parsed_expr.dt.strftime("%Y-%m-%d")).otherwise(text_expr))
            .alias(column_name)
        )

    @staticmethod
    def _normalized_datetime_expr(column_name: str) -> pl.Expr:
        text_expr = (
            pl.when(pl.col(column_name).is_null())
            .then(pl.lit(None))
            .otherwise(pl.col(column_name).cast(pl.Utf8).str.strip_chars())
        )
        parsed_expr = pl.coalesce([
            text_expr.str.strptime(pl.Datetime, format="%Y-%m-%dT%H:%M:%S%.fZ", strict=False),
            text_expr.str.strptime(pl.Datetime, format="%Y-%m-%d %H:%M:%S", strict=False),
            text_expr.str.strptime(pl.Datetime, format="%d-%m-%Y %H:%M:%S", strict=False),
            text_expr.str.strptime(pl.Datetime, format="%d/%m/%Y %H:%M:%S", strict=False),
            text_expr.str.strptime(pl.Datetime, format="%Y-%m-%d", strict=False),
            text_expr.str.strptime(pl.Datetime, format="%d-%m-%Y", strict=False),
            text_expr.str.strptime(pl.Datetime, format="%d/%m/%Y", strict=False),
        ])
        return (
            pl.when(text_expr.is_null() | (text_expr == ""))
            .then(pl.lit(None))
            .otherwise(pl.when(parsed_expr.is_not_null()).then(parsed_expr.dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")).otherwise(text_expr))
            .alias(column_name)
        )

    def _truncate_text_fields_to_target_limits(self, object_name: str, chunk: pl.DataFrame) -> pl.DataFrame:
        if chunk.is_empty():
            return chunk

        describe = self._describe_object(object_name)
        field_map = {
            f.get("name"): f
            for f in describe.get("fields", [])
            if f.get("name")
        }
        truncatable_types = {"string", "textarea", "phone", "email", "url", "encryptedstring"}

        exprs: list[pl.Expr] = []
        truncated_fields: list[str] = []
        for column_name in chunk.columns:
            if self._is_relationship_reference_column(column_name):
                continue

            field_meta = field_map.get(column_name)
            if not field_meta:
                continue

            max_len = field_meta.get("length")
            if not isinstance(max_len, int) or max_len <= 0:
                continue

            if (field_meta.get("type") or "").lower() not in truncatable_types:
                continue

            series = chunk.get_column(column_name).cast(pl.Utf8)
            over_limit = series.str.len_chars() > max_len
            if not over_limit.any():
                continue

            truncated_fields.append(f"{column_name}({int(over_limit.sum())} rows->{max_len})")
            exprs.append(
                pl.when(pl.col(column_name).is_not_null())
                .then(pl.col(column_name).cast(pl.Utf8).str.slice(0, max_len))
                .otherwise(pl.lit(None))
                .alias(column_name)
            )

        if truncated_fields:
            logger.warning(
                f"[{object_name}] Truncating text fields to target limits: {', '.join(truncated_fields)}"
            )
            chunk = chunk.with_columns(exprs)

        return chunk

    def _split_blocked_dependency_rows(
        self,
        object_name: str,
        chunk: pl.DataFrame,
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        if chunk.is_empty() or not self.id_mapper:
            return chunk, pl.DataFrame()

        describe = self._describe_object(object_name)
        relationship_map = {
            f.get("relationshipName"): f
            for f in describe.get("fields", [])
            if f.get("relationshipName")
        }

        working = chunk.with_row_index("__row_idx__")
        reason_field_col = "__blocked_dependency_field__"
        reason_object_col = "__blocked_dependency_object__"
        reason_source_col = "__blocked_dependency_source_id__"
        working = working.with_columns([
            pl.lit(None).cast(pl.Utf8).alias(reason_field_col),
            pl.lit(None).cast(pl.Utf8).alias(reason_object_col),
            pl.lit(None).cast(pl.Utf8).alias(reason_source_col),
        ])

        for idx, column_name in enumerate(chunk.columns):
            if not self._is_relationship_reference_column(column_name):
                continue

            parsed = self._parse_relationship_reference_column(column_name)
            if not parsed:
                continue
            _, relationship_name, _ = parsed
            relationship_field = relationship_map.get(relationship_name)
            if not relationship_field:
                continue

            ref_objects = [r for r in (relationship_field.get("referenceTo") or []) if r]
            if len(ref_objects) != 1:
                continue

            ref_object = ref_objects[0]
            if self.id_mapper.mapping_count(ref_object) == 0:
                continue

            temp_col = f"__blocked_lookup_resolved_{idx}__"
            working = self.id_mapper.resolve_column(ref_object, working, column_name, temp_col)
            blocked_expr = pl.col(column_name).is_not_null() & pl.col(temp_col).is_null()

            working = working.with_columns([
                pl.when(pl.col(reason_field_col).is_null() & blocked_expr)
                .then(pl.lit(relationship_field.get("name") or column_name))
                .otherwise(pl.col(reason_field_col))
                .alias(reason_field_col),
                pl.when(pl.col(reason_object_col).is_null() & blocked_expr)
                .then(pl.lit(ref_object))
                .otherwise(pl.col(reason_object_col))
                .alias(reason_object_col),
                pl.when(pl.col(reason_source_col).is_null() & blocked_expr)
                .then(pl.col(column_name).cast(pl.Utf8))
                .otherwise(pl.col(reason_source_col))
                .alias(reason_source_col),
            ])
            working = working.drop(temp_col)

        blocked = working.filter(pl.col(reason_field_col).is_not_null()).drop("__row_idx__")
        ready = working.filter(pl.col(reason_field_col).is_null()).drop(
            "__row_idx__", reason_field_col, reason_object_col, reason_source_col
        )
        return ready, blocked

    def _sanitize_account_payload(self, chunk: pl.DataFrame) -> pl.DataFrame:
        if chunk.is_empty():
            return chunk

        person_only_columns = [
            column_name
            for column_name in chunk.columns
            if self._is_account_person_only_column(column_name)
        ]
        person_name_columns = [column_name for column_name in ("Name",) if column_name in chunk.columns]
        if not person_only_columns and not person_name_columns:
            return chunk

        person_mask = self._account_person_row_expr(chunk.columns)
        business_mask = ~person_mask
        business_rows = chunk.select(
            business_mask.cast(pl.Int8).sum().alias("business_count")
        ).item()
        person_rows = chunk.select(
            person_mask.cast(pl.Int8).sum().alias("person_count")
        ).item()
        if not business_rows and not person_rows:
            return chunk

        expressions = []
        if person_only_columns and business_rows:
            expressions.extend([
                pl.when(business_mask)
                .then(pl.lit(None).cast(pl.Utf8))
                .otherwise(pl.col(column_name).cast(pl.Utf8))
                .alias(column_name)
                for column_name in person_only_columns
            ])

        if person_name_columns and person_rows:
            expressions.extend([
                pl.when(person_mask)
                .then(pl.lit(None).cast(pl.Utf8))
                .otherwise(pl.col(column_name).cast(pl.Utf8))
                .alias(column_name)
                for column_name in person_name_columns
            ])

        if expressions:
            chunk = chunk.with_columns(expressions)

        if person_only_columns and business_rows:
            logger.info(
                f"[Account] Sanitized {len(person_only_columns)} person-only column(s) "
                f"for {business_rows:,} business-account row(s)"
            )
        if person_name_columns and person_rows:
            logger.info(
                f"[Account] Sanitized {len(person_name_columns)} business-only column(s) "
                f"for {person_rows:,} person-account row(s)"
            )
        return chunk

    def _is_account_person_only_column(self, column_name: str) -> bool:
        if column_name.endswith("__pc"):
            return True
        if ".__pr." in column_name:
            return True
        if "__pr." in column_name:
            return True
        if column_name.startswith("Person"):
            return True
        return column_name in {"FirstName", "MiddleName", "LastName", "Salutation"}

    def _account_person_row_expr(self, columns: list[str]) -> pl.Expr:
        exprs: list[pl.Expr] = []
        explicit_business_exprs: list[pl.Expr] = []

        def non_blank(column_name: str) -> pl.Expr:
            return (
                pl.col(column_name)
                .cast(pl.Utf8)
                .fill_null("")
                .str.strip_chars()
                .ne("")
            )

        if "IsPersonAccount" in columns:
            exprs.append(
                pl.col("IsPersonAccount")
                .cast(pl.Utf8)
                .fill_null("")
                .str.to_lowercase()
                .is_in(["true", "1", "yes"])
            )

        for column_name in ("FirstName", "LastName"):
            if column_name in columns:
                exprs.append(non_blank(column_name))

        if "Record_Type_Name__c" in columns:
            record_type_expr = (
                pl.col("Record_Type_Name__c")
                .cast(pl.Utf8)
                .fill_null("")
                .str.to_lowercase()
            )
            exprs.append(
                record_type_expr.str.contains(r"\b(client|person account|personaccount)\b")
            )
            explicit_business_exprs.append(
                record_type_expr.str.contains(r"\b(travel agent|travel_agent|agency|partner|business|b2b)\b")
            )

        if "Type" in columns:
            exprs.append(
                pl.col("Type")
                .cast(pl.Utf8)
                .fill_null("")
                .str.to_lowercase()
                .str.contains(r"\bb2c\b")
            )

        if not exprs:
            return pl.lit(False)

        combined = exprs[0]
        for expr in exprs[1:]:
            combined = combined | expr
        if explicit_business_exprs:
            explicit_business = explicit_business_exprs[0]
            for expr in explicit_business_exprs[1:]:
                explicit_business = explicit_business | expr
            combined = combined & ~explicit_business
        return combined


    # ------------------------------------------------------------------ #
    #  Bulk API 2.0 — ingest job lifecycle                               #
    # ------------------------------------------------------------------ #

    def _create_ingest_job(self, object_name: str, operation: str, external_id_field: str) -> str:
        payload = {
            "object": object_name,
            "operation": operation,
            "contentType": "CSV",
            "lineEnding": "LF",
        }
        if operation == "upsert":
            payload["externalIdFieldName"] = external_id_field

        # Use the connector session so proxy handling, authentication retry,
        # and HTTP error reporting are consistent with every other Bulk call.
        try:
            result = self.connector.bulk_post("jobs/ingest", payload)
        except Exception as exc:
            raise LoadError(
                f"[{object_name}] Failed to create ingest job | payload={payload} | {exc}"
            ) from exc
        job_id = str(result.get("id") or "").strip()
        if not job_id:
            raise LoadError(
                f"[{object_name}] Salesforce returned no ingest job ID | "
                f"payload={payload} | response={result}"
            )
        return job_id

    def _upload_csv(self, job_id: str, df: pl.DataFrame):
        buf = io.StringIO()
        df.write_csv(buf)
        csv_data = buf.getvalue()
        self.connector.bulk_put_csv(f"jobs/ingest/{job_id}/batches", csv_data)

    def _close_job(self, job_id: str):
        self.connector.bulk_patch(f"jobs/ingest/{job_id}", {"state": "UploadComplete"})

    def _wait_for_job(self, object_name: str, job_id: str):
        last_state = None
        for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
            result = self.connector.bulk_get(f"jobs/ingest/{job_id}").json()
            state = result.get("state")
            processed = result.get("numberRecordsProcessed")
            failed = result.get("numberRecordsFailed")

            if state == "JobComplete":
                return
            if state in ("Failed", "Aborted"):
                error_msg = result.get("errorMessage") or result.get("stateMessage") or "(no details)"
                raise LoadError(
                    f"[{object_name}] Ingest job {job_id} failed | state={state} | "
                    f"Salesforce: {error_msg}"
                )

            if state != last_state or attempt == 1 or attempt % 6 == 0:
                logger.info(
                    f"[{object_name}] Waiting on ingest job {job_id} | poll={attempt}/{MAX_POLL_ATTEMPTS} "
                    f"| state={state} | processed={processed or 0} | failed={failed or 0}"
                )
                last_state = state
            time.sleep(POLL_INTERVAL_SECONDS)

        raise LoadError(f"[{object_name}] Ingest job timed out after {MAX_POLL_ATTEMPTS} polls")

    def _collect_results(
        self, object_name: str, job_id: str, uploaded_df: pl.DataFrame
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Fetch successful and failed results from the completed job."""
        success_df = restore_salesforce_ids_in_frame(
            self._fetch_result_csv(f"jobs/ingest/{job_id}/successfulResults")
        )
        failed_df = restore_salesforce_ids_in_frame(
            self._fetch_result_csv(f"jobs/ingest/{job_id}/failedResults")
        )
        return success_df, failed_df

    def _fetch_result_csv(self, path: str) -> pl.DataFrame:
        resp = self.connector.bulk_get_csv(path)
        csv_text = self.connector.decode_csv_response(resp)
        if not csv_text.strip():
            return pl.DataFrame()
        header_line = csv_text.splitlines()[0] if csv_text else ""
        header_columns = next(csv.reader([header_line])) if header_line else []
        schema_overrides = {
            column_name: pl.Utf8
            for column_name in header_columns
            if column_name
        }
        return pl.read_csv(
            io.StringIO(csv_text),
            infer_schema_length=0,
            schema_overrides=schema_overrides,
            null_values=[""],
        )

    def _record_id_mappings(
        self,
        object_name: str,
        success_df: pl.DataFrame,
        uploaded_df: pl.DataFrame,
        external_id_field: str,
    ):
        """
        Map source IDs to newly created target IDs.
        success_df from Bulk API contains: sf__Id (new target ID), sf__Created
        We join back on external_id_field to find the source ID.
        """
        if external_id_field not in uploaded_df.columns:
            return
        if "sf__Id" not in success_df.columns:
            return

        attempted_source_ids = (
            uploaded_df
            .select(pl.col(external_id_field).cast(pl.Utf8).alias("source_id"))
            .filter(pl.col("source_id").is_not_null())
            .get_column("source_id")
            .to_list()
        )
        self.id_mapper.remove_mappings(object_name, attempted_source_ids)

        if external_id_field in success_df.columns:
            mappings = success_df.select([
                pl.col(external_id_field).cast(pl.Utf8).alias("source_id"),
                pl.col("sf__Id").cast(pl.Utf8).alias("target_id"),
            ]).filter(
                pl.col("source_id").is_not_null() & pl.col("target_id").is_not_null()
            )
        else:
            logger.warning(
                f"[{object_name}] Bulk success results did not include '{external_id_field}'. "
                "Falling back to positional ID mapping, which may be inaccurate."
            )
            target_ids = success_df["sf__Id"].cast(pl.Utf8)
            source_ids = uploaded_df[external_id_field][: len(target_ids)].cast(pl.Utf8)
            mappings = pl.DataFrame({
                "source_id": source_ids,
                "target_id": target_ids,
            }).filter(
                pl.col("source_id").is_not_null() & pl.col("target_id").is_not_null()
            )

        mappings = mappings.unique(subset=["source_id"], keep="last")
        self.id_mapper.bulk_add_mappings(object_name, mappings)

    # ------------------------------------------------------------------ #
    #  PricebookEntry: standard price pre-requisite                      #
    # ------------------------------------------------------------------ #

    def _field_createable(self, object_name: str, field_name: str) -> bool:
        """True if the field exists on the object and is createable (Bulk insert safe)."""
        try:
            describe = self._describe_object(object_name)
            for f in describe.get("fields", []) or []:
                if f.get("name") == field_name:
                    return bool(f.get("createable"))
        except Exception as e:
            logger.warning(
                f"[{object_name}] Could not verify field '{field_name}' via describe: {e}"
            )
        return False

    def _ensure_standard_prices(self, writable_fields: set[str] | None):
        """
        Salesforce requires a Standard Pricebook entry for a product before
        a custom pricebook entry can be created.
        This method auto-creates any missing standard prices (UnitPrice=0)
        on the target org before the main PricebookEntry load.
        """
        # Find the Product2 external ID reference column e.g. "Product2.Migration_Key__c"
        product_col = None
        for chunk in self.stager.read_xfm_chunks("PricebookEntry", chunk_size=self.batch_size):
            chunk = self._normalize_relationship_reference_columns(chunk)
            product_col = next(
                (c for c in chunk.columns if c.startswith("Product2.")),
                None,
            )
            break
        if not product_col:
            logger.warning("[PricebookEntry] No Product2 reference column — skipping standard price check")
            return

        ext_id_field = product_col.split(".", 1)[1] if "." in product_col else product_col.split(":", 1)[1]

        # Get Standard Pricebook ID on target
        std_pbs = self.connector.query("SELECT Id FROM Pricebook2 WHERE IsStandard=true LIMIT 1")
        if not std_pbs:
            logger.warning("[PricebookEntry] Standard Pricebook not found on target org")
            return
        std_pb_id = std_pbs[0]["Id"]

        # Collect all unique product external IDs across all chunks
        all_product_ids: set[str] = set()
        product_currency: dict[str, str] = {}
        for chunk in self.stager.read_xfm_chunks("PricebookEntry", chunk_size=self.batch_size):
            chunk = self._normalize_relationship_reference_columns(chunk)
            for row in chunk.select([product_col] + (["CurrencyIsoCode"] if "CurrencyIsoCode" in chunk.columns else [])).iter_rows(named=True):
                pid = row.get(product_col)
                if pid:
                    all_product_ids.add(pid)
                    if "CurrencyIsoCode" in row and pid not in product_currency:
                        product_currency[pid] = row["CurrencyIsoCode"] or "USD"

        if not all_product_ids:
            return

        # Check which products already have standard prices on target
        ids_csv = "', '".join(all_product_ids)
        existing = self.connector.query(
            f"SELECT Product2.{ext_id_field} FROM PricebookEntry "
            f"WHERE Pricebook2.IsStandard = true "
            f"AND Product2.{ext_id_field} IN ('{ids_csv}')"
        )
        existing_ids = {
            r.get("Product2", {}).get(ext_id_field)
            for r in existing
            if r.get("Product2")
        }
        missing = [pid for pid in all_product_ids if pid not in existing_ids]

        if not missing:
            logger.info("[PricebookEntry] All products already have standard prices on target")
            return

        include_currency = (
            "CurrencyIsoCode" in writable_fields
            if writable_fields is not None
            else self._field_createable("PricebookEntry", "CurrencyIsoCode")
        )
        if not include_currency:
            logger.info(
                "[PricebookEntry] Omitting CurrencyIsoCode from standard-price inserts "
                "(field not createable on target — typical for single-currency orgs)."
            )

        logger.info(f"[PricebookEntry] Auto-creating {len(missing)} missing standard price(s) on target...")

        rows = []
        for pid in missing:
            row = {
                "Pricebook2Id": std_pb_id,
                product_col: pid,
                "UnitPrice": 0,
                "IsActive": True,
                "UseStandardPrice": False,
            }
            if include_currency:
                row["CurrencyIsoCode"] = product_currency.get(pid, "USD")
            rows.append(row)
        std_df = pl.DataFrame(rows)

        logger.info(f"[PricebookEntry] Standard price payload columns: {std_df.columns}")
        logger.info(f"[PricebookEntry] Standard price payload:\n{std_df}")
        job_id = self._create_ingest_job("PricebookEntry", "insert", "Id")
        self._upload_csv(job_id, std_df)
        self._close_job(job_id)
        self._wait_for_job("PricebookEntry", job_id)
        success_df, failed_df = self._collect_results("PricebookEntry", job_id, std_df)

        logger.info(f"[PricebookEntry] Standard prices created: {len(success_df)} success | {len(failed_df)} failed")
        if not failed_df.is_empty():
            logger.warning(f"[PricebookEntry] Standard price failures:\n{failed_df}")

    # ------------------------------------------------------------------ #
    #  Target field filtering                                            #
    # ------------------------------------------------------------------ #

    def _get_writable_fields(self, object_name: str, external_id_field: str) -> set[str] | None:
        """
        Return the set of field names that are createable or updateable on the target org.
        Always includes external_id_field (needed as the upsert key).
        Returns None if describe fails (fail open — don't block the load).
        """
        try:
            describe = self._describe_object(object_name)
            writable = {
                f["name"]
                for f in describe.get("fields", [])
                if f.get("createable", False) or f.get("updateable", False)
            }
            writable.add(external_id_field)  # always keep upsert key
            writable.update(MIGRATABLE_AUDIT_FIELDS)
            return writable
        except Exception as e:
            logger.warning(
                f"[{object_name}] Could not describe target fields — "
                f"uploading all columns without filtering: {e}"
            )
            return None

    def _get_updateable_fields(self, object_name: str) -> set[str] | None:
        """
        Field names flagged updateable on the target. Used to filter the
        match-by *update* sub-payload, since Salesforce rejects createable-only
        fields (e.g. Knowledge__kav.Language, Knowledge__kav.UrlName) on update
        with INVALID_FIELD_FOR_INSERT_UPDATE.
        Returns None if describe fails (caller falls back to no filtering).
        """
        try:
            describe = self._describe_object(object_name)
            return {
                f["name"]
                for f in describe.get("fields", [])
                if f.get("updateable", False)
            }
        except Exception as e:
            logger.warning(
                f"[{object_name}] Could not describe target fields for updateable filter: {e}"
            )
            return None

    def _get_createable_fields(self, object_name: str) -> set[str] | None:
        """
        Field names flagged createable on the target. Used to filter the
        match-by *insert* sub-payload, dropping updateable-only fields that
        Salesforce would reject on insert.
        Returns None if describe fails (caller falls back to no filtering).
        """
        try:
            describe = self._describe_object(object_name)
            createable = {
                f["name"]
                for f in describe.get("fields", [])
                if f.get("createable", False)
            }
            createable.update(MIGRATABLE_AUDIT_FIELDS)
            return createable
        except Exception as e:
            logger.warning(
                f"[{object_name}] Could not describe target fields for createable filter: {e}"
            )
            return None

    def _restrict_payload_columns(
        self,
        object_name: str,
        chunk: pl.DataFrame,
        allowed_fields: set[str] | None,
        keep_columns: set[str],
        operation_label: str,
    ) -> pl.DataFrame:
        """
        Drop columns from `chunk` that are not in `allowed_fields`, except for
        columns explicitly listed in `keep_columns` (e.g. 'Id' for updates) and
        relationship reference columns (e.g. 'Account.Prod_Record_Id__c').

        No-ops when `allowed_fields` is None (describe failed — fail open).
        """
        if allowed_fields is None:
            return chunk

        keep = [
            c for c in chunk.columns
            if c in allowed_fields
            or c in keep_columns
            or self._is_relationship_reference_column(c)
        ]
        dropped = [c for c in chunk.columns if c not in keep]
        if dropped:
            logger.info(
                f"[{object_name}] {operation_label}: dropping {len(dropped)} column(s) "
                f"not allowed for this operation on target: {dropped}"
            )
        return chunk.select(keep)

    def _is_relationship_reference_column(self, column_name: str) -> bool:
        return "." in column_name or ":" in column_name

    def _normalize_relationship_reference_columns(self, df: pl.DataFrame) -> pl.DataFrame:
        rename_map = {
            c: c.replace(":", ".", 1)
            for c in df.columns
            if ":" in c and c.split(":", 1)[0]
        }
        return df.rename(rename_map) if rename_map else df

    def _map_external_ids_to_target_ids(
        self,
        object_name: str,
        field_name: str,
        source_values: list[str],
    ) -> dict[str, str]:
        originals = [str(value).strip() for value in source_values if str(value or "").strip()]
        if not originals:
            return {}
        query_values: list[str] = []
        seen: set[str] = set()
        for value in originals:
            for candidate in (value, value.lower(), restore_salesforce_id_casing(value)):
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    query_values.append(candidate)
        matches = self._query_target_match_ids(object_name, field_name, query_values)
        folded = {key.casefold(): target_id for key, target_id in matches.items()}
        mapped: dict[str, str] = {}
        for value in originals:
            target_id = matches.get(value) or folded.get(value.casefold())
            if target_id:
                mapped[value] = target_id
        return mapped

    def _resolve_relationship_reference_columns(self, object_name: str, df: pl.DataFrame) -> pl.DataFrame:
        relationship_columns = [c for c in df.columns if self._is_relationship_reference_column(c)]
        if not relationship_columns or df.is_empty():
            return df

        describe = self._describe_object(object_name)
        fields = describe.get("fields", [])
        relationship_map = {
            f.get("relationshipName"): f
            for f in fields
            if f.get("relationshipName")
        }

        for column_name in relationship_columns:
            if column_name not in df.columns:
                continue
            parsed = self._parse_relationship_reference_column(column_name)
            if not parsed:
                continue
            object_type, relationship_name, ext_field_name = parsed
            relationship_field = relationship_map.get(relationship_name)
            if not relationship_field:
                continue

            ref_objects = [r for r in (relationship_field.get("referenceTo") or []) if r]
            ref_object = self._relationship_reference_target(relationship_name, relationship_field)
            if not ref_object:
                continue

            target_field_name = relationship_field.get("name")
            if not target_field_name:
                continue

            # Polymorphic lookups (Owner → User/Group) must stay as
            # ObjectType:Relationship.ExternalId in Bulk API CSV; raw OwnerId
            # values fail ingest and untyped relationship headers are rejected.
            if is_polymorphic_reference_field(ref_objects):
                typed_column_name = f"{ref_object}:{relationship_name}.{ext_field_name}"
                if object_type != ref_object and typed_column_name != column_name:
                    df = df.rename({column_name: typed_column_name})
                    column_name = typed_column_name
                if target_field_name in df.columns:
                    df = df.drop(target_field_name)
                logger.info(
                    f"[{object_name}] Keeping polymorphic relationship column '{column_name}' "
                    f"for Bulk API upload."
                )
                continue

            df = df.with_columns(pl.col(column_name).cast(pl.Utf8))
            if "__from_map__" in df.columns:
                df = df.drop("__from_map__")
            if "__from_query__" in df.columns:
                df = df.drop("__from_query__")

            if self.id_mapper and self.id_mapper.mapping_count(ref_object) > 0:
                df = self.id_mapper.resolve_column(ref_object, df, column_name, "__from_map__")
            else:
                df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias("__from_map__"))

            unresolved = (
                df.filter(pl.col("__from_map__").is_null())
                .get_column(column_name)
                .drop_nulls()
                .unique()
                .to_list()
            )
            mapped: dict[str, str] = {}
            if unresolved:
                try:
                    mapped = self._map_external_ids_to_target_ids(
                        ref_object, ext_field_name, unresolved
                    )
                except Exception as exc:
                    logger.warning(
                        f"[{object_name}] Could not resolve '{column_name}' against "
                        f"target {ref_object}.{ext_field_name}: {exc}"
                    )

            if mapped:
                query_df = pl.DataFrame(
                    {
                        column_name: list(mapped.keys()),
                        "__from_query__": list(mapped.values()),
                    },
                    schema={column_name: pl.Utf8, "__from_query__": pl.Utf8},
                )
                df = df.join(query_df, on=column_name, how="left")
            else:
                df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias("__from_query__"))

            resolved_expr = pl.coalesce(
                [pl.col("__from_map__"), pl.col("__from_query__")]
            ).alias("__resolved_lookup_id__")
            df = df.with_columns(resolved_expr)
            if df.filter(pl.col("__resolved_lookup_id__").is_not_null()).is_empty():
                df = df.drop(
                    [col for col in ("__from_map__", "__from_query__", "__resolved_lookup_id__") if col in df.columns]
                )
                continue

            if target_field_name in df.columns:
                df = df.with_columns(
                    pl.when(pl.col(target_field_name).is_not_null())
                    .then(pl.col(target_field_name).cast(pl.Utf8))
                    .otherwise(pl.col("__resolved_lookup_id__"))
                    .alias(target_field_name)
                )
                df = df.drop("__resolved_lookup_id__")
            else:
                df = df.rename({"__resolved_lookup_id__": target_field_name})

            df = df.drop(
                [col for col in ("__from_map__", "__from_query__") if col in df.columns]
            )

            df = df.with_columns(
                pl.when(pl.col(target_field_name).is_not_null())
                .then(pl.lit(None).cast(pl.Utf8))
                .otherwise(pl.col(column_name))
                .alias(column_name)
            )
            remaining = (
                df.get_column(column_name)
                .drop_nulls()
                .cast(pl.Utf8)
                .str.strip_chars()
            )
            if remaining.filter(remaining.ne("")).is_empty():
                df = df.drop(column_name)
                logger.info(
                    f"[{object_name}] Resolved relationship '{column_name}' to "
                    f"'{target_field_name}' using target {ref_object}.{ext_field_name}."
                )
            else:
                resolved_count = df.filter(pl.col(target_field_name).is_not_null()).height
                logger.info(
                    f"[{object_name}] Resolved {resolved_count:,} '{column_name}' value(s) to "
                    f"'{target_field_name}'; leaving unmatched rows as relationship references."
                )

        return df

    # ------------------------------------------------------------------ #
    #  Update (for Pass 2 cyclic resolution)                             #
    # ------------------------------------------------------------------ #

    def update_records(self, object_name: str, df: pl.DataFrame) -> int:
        """
        Perform a Bulk API 2.0 update using Id as the record identifier.
        Used for Pass 2 cyclic field resolution.
        Returns number of records successfully updated.
        """
        self.last_update_failed_count = 0
        self.last_update_failed_file = None
        self.last_update_failed_ids = set()
        if df.is_empty():
            return 0

        job_id = self._create_ingest_job(object_name, "update", "Id")
        self._upload_csv(job_id, df)
        self._close_job(job_id)
        self._wait_for_job(object_name, job_id)

        success_df, failed_df = self._collect_results(object_name, job_id, df)

        if not failed_df.is_empty():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            failed_path = self._stage_log_dir("load_cyclic_failed") / f"{object_name}_cyclic_failed_{timestamp}.csv"
            failed_df.write_csv(str(failed_path))
            self.last_update_failed_count = len(failed_df)
            self.last_update_failed_file = self._publish_migration_result(failed_path, object_name)
            id_column = "Id" if "Id" in failed_df.columns else "sf__Id"
            if id_column in failed_df.columns:
                self.last_update_failed_ids = {
                    str(value)
                    for value in failed_df.get_column(id_column).drop_nulls().to_list()
                }
            logger.warning(f"[{object_name}] {len(failed_df)} cyclic updates failed — {failed_path.name}")

        return len(success_df)

    def delete_records(self, object_name: str, df: pl.DataFrame) -> tuple[int, pl.DataFrame]:
        """
        Perform a Bulk API 2.0 delete using Id as the record identifier.
        Returns (successful_count, failed_df).
        """
        if df.is_empty():
            return 0, pl.DataFrame()
        if "Id" not in df.columns:
            raise LoadError(f"[{object_name}] Delete payload must contain an 'Id' column")

        delete_df = df.select(["Id"]).unique(subset=["Id"]).filter(pl.col("Id").is_not_null())
        if delete_df.is_empty():
            return 0, pl.DataFrame()

        job_id = self._create_ingest_job(object_name, "delete", "Id")
        self._upload_csv(job_id, delete_df)
        self._close_job(job_id)
        self._wait_for_job(object_name, job_id)

        success_df, failed_df = self._collect_results(object_name, job_id, delete_df)
        if not failed_df.is_empty():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            failed_path = self._stage_log_dir("delete_failed") / f"{object_name}_delete_failed_{timestamp}.csv"
            failed_df.write_csv(str(failed_path))
            published = self._publish_migration_result(failed_path, object_name)
            logger.warning(f"[{object_name}] {len(failed_df)} deletes failed - {Path(published).name}")

        return len(success_df), failed_df
