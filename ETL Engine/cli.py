"""
SF ETL CLI — entry point for all migration commands.

Commands:
  run       Run full migration or a specific pipeline
  status    Show current run state
  retry     Retry a failed pipeline
  validate  Validate migration config without running
  plan      Show execution plan (dry preview of ordering)
"""

import logging
import os
import re
import sys
import traceback
import csv
import yaml
import click
import polars as pl
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from core.object_hierarchy import generate_object_hierarchy_workbook

# ------------------------------------------------------------------ #
#  Logging setup                                                      #
# ------------------------------------------------------------------ #


def _configure_console_streams() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_configure_console_streams()

_rich_console = Console(file=sys.stderr)
Path("logs").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        RichHandler(rich_tracebacks=True, show_path=False, console=_rich_console),
        logging.FileHandler("logs/sf_etl.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("sf_etl")
console = Console(file=sys.stdout)

_UNICODE_CONSOLE_TRANSLATIONS = str.maketrans({
    "✓": "OK",
    "✗": "X",
    "⚠": "WARN",
    "▶": "==>",
    "↷": "->",
    "→": "->",
    "↓": "v",
    "↑": "^",
    "⇄": "<->",
    "↺": "retry",
    "—": "-",
    "–": "-",
    "…": "...",
})

_UNICODE_CONSOLE_TRANSLATIONS.update(str.maketrans({
    "\u2713": "OK",
    "\u2717": "X",
    "\u26a0": "WARN",
    "\u25b6": "==>",
    "\u21b7": "->",
    "\u2192": "->",
    "\u2193": "v",
    "\u2191": "^",
    "\u21c4": "<->",
    "\u21ba": "retry",
    "\u2014": "-",
    "\u2013": "-",
    "\u2026": "...",
}))


def _normalize_console_text(value: str) -> str:
    return value.translate(_UNICODE_CONSOLE_TRANSLATIONS)


def _normalize_console_arg(value):
    if isinstance(value, str):
        return _normalize_console_text(value)
    return value


class _ConsoleSafeLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _normalize_console_text(record.msg)
        if record.args:
            record.args = tuple(_normalize_console_arg(arg) for arg in record.args)
        return True


for _handler in logging.getLogger().handlers:
    if isinstance(_handler, RichHandler):
        _handler.addFilter(_ConsoleSafeLogFilter())


_raw_console_print = console.print


def _safe_console_print(*args, **kwargs):
    _raw_console_print(*[_normalize_console_arg(arg) for arg in args], **kwargs)


console.print = _safe_console_print

load_dotenv(override=True)


# ------------------------------------------------------------------ #
#  Helpers                                                            #
# ------------------------------------------------------------------ #

def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        console.print(f"[red]Config file not found: {config_path}[/red]")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not str(os.environ.get("ETL_LOAD_BATCH_SIZE") or "").strip():
        yaml_size = (cfg.get("migration") or {}).get("load_batch_size")
        if yaml_size is not None and str(yaml_size).strip() != "":
            from core.loader import resolve_load_batch_size
            try:
                os.environ["ETL_LOAD_BATCH_SIZE"] = str(resolve_load_batch_size(yaml_size))
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                sys.exit(1)
    return cfg


def get_latest_run_dir(base_dir: str = "exports") -> Path | None:
    base = Path(base_dir)
    if not base.exists():
        return None
    run_dirs = [p for p in base.iterdir() if p.is_dir() and p.name.startswith("run_")]
    if not run_dirs:
        return None
    return max(run_dirs, key=lambda p: p.stat().st_mtime)


def find_latest_pipeline_stage_dir(
    base_dir: str,
    pipeline_key: str,
    stage_name: str,
    required_objects: set[str] | None = None,
) -> Path | None:
    base = Path(base_dir)
    if not base.exists():
        return None
    run_dirs = [p for p in base.iterdir() if p.is_dir() and p.name.startswith("run_")]
    run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for run_dir in run_dirs:
        stage_dir = run_dir / pipeline_key / stage_name
        if not stage_dir.exists():
            continue
        csv_files = {path.stem for path in stage_dir.glob("*.csv")}
        if not csv_files:
            continue
        if required_objects and not (csv_files & required_objects):
            continue
        return stage_dir
    return None


def get_or_create_run_dir(base_dir: str = "exports") -> Path:
    run_dir = get_latest_run_dir(base_dir)
    if run_dir:
        return run_dir
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(base_dir) / f"run_{run_ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def create_run_dir(base_dir: str = "exports") -> Path:
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(base_dir) / f"run_{run_ts}"
    suffix = 1
    while run_dir.exists():
        run_dir = Path(base_dir) / f"run_{run_ts}_{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def create_named_run_dir(base_dir: str, run_name: str) -> Path:
    run_dir = Path(base_dir) / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def read_object_list_file(path: str) -> list[str]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Object list file not found: {file_path}")

    objects: list[str] = []
    seen: set[str] = set()
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line in seen:
            continue
        seen.add(line)
        objects.append(line)
    return objects


def resolve_env_run_dir_name() -> str:
    env_run_label = (os.environ.get("ETL_RUN_DIR_NAME") or "").strip()
    if env_run_label:
        sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", env_run_label)
        return sanitized if sanitized.startswith("run_") else f"run_{sanitized}"
    return datetime.now().strftime("run_%Y%m%d_%H%M%S")


def _slugify_pipeline_artifact_label(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", (value or "").strip()).strip("_")
    return text or "Pipeline"


def build_pipeline_artifact_dir_map(pipelines) -> dict[str, str]:
    counts: dict[str, int] = {}
    mapping: dict[str, str] = {}
    for pipe in pipelines:
        base_label = _slugify_pipeline_artifact_label(getattr(pipe, "name", "") or getattr(pipe, "pipeline_id", ""))
        counts[base_label] = counts.get(base_label, 0) + 1
        mapping[pipe.pipeline_id] = f"{base_label}_attempt_{counts[base_label]}"
    return mapping


def ensure_stage_dir(base_dir: Path, stage_name: str) -> Path:
    stage_dir = base_dir / stage_name
    stage_dir.mkdir(parents=True, exist_ok=True)
    return stage_dir


def create_sequenced_dir(base_dir: Path, prefix: str = "rerun_") -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    existing_numbers: list[int] = []
    for child in base_dir.iterdir():
        if not child.is_dir() or not child.name.startswith(prefix):
            continue
        suffix = child.name[len(prefix):]
        if suffix.isdigit():
            existing_numbers.append(int(suffix))
    next_number = (max(existing_numbers) + 1) if existing_numbers else 1
    next_dir = base_dir / f"{prefix}{next_number:03d}"
    next_dir.mkdir(parents=True, exist_ok=False)
    return next_dir


def clear_local_duckdb_state(stager, id_mapper) -> None:
    """Clear all staged and mapping state from local DuckDB files."""
    stager.clear_all()
    id_mapper.clear_all()


def write_stage_csv(stager, object_name: str, csv_file: Path, stage: str = "raw"):
    first_chunk = True
    chunk_iter = stager.read_raw_chunks(object_name) if stage == "raw" else stager.read_xfm_chunks(object_name)
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        for chunk in chunk_iter:
            csv_str = chunk.write_csv(file=None)
            if first_chunk:
                f.write(csv_str)
                first_chunk = False
            else:
                newline_pos = csv_str.index("\n")
                f.write(csv_str[newline_pos + 1:])


def backfill_missing_stage_csvs(
    stager,
    stage_dir: Path,
    object_names: set[str],
    stage: str = "xfm",
) -> tuple[int, list[str]]:
    written: list[str] = []
    stage_dir.mkdir(parents=True, exist_ok=True)
    for object_name in sorted(object_names):
        csv_path = stage_dir / f"{object_name}.csv"
        if csv_path.exists():
            continue
        row_count = stager.get_raw_count(object_name) if stage == "raw" else stager.get_xfm_count(object_name)
        if row_count <= 0:
            continue
        write_stage_csv(stager, object_name, csv_path, stage=stage)
        written.append(object_name)
    return len(written), written


def sync_stage_csvs_to_staging(
    stager,
    stage_dir: Path,
    object_names: set[str],
    stage: str = "xfm",
) -> tuple[int, list[str]]:
    loaded: list[str] = []
    for object_name in sorted(object_names):
        csv_path = stage_dir / f"{object_name}.csv"
        if not csv_path.exists():
            continue
        stager.load_csv_to_stage(object_name, csv_path, stage=stage)
        loaded.append(object_name)
    return len(loaded), loaded


def write_topology_report_csv(report_path: Path, node_stats: list[dict]):
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "pipeline_id",
                "pipeline_name",
                "node_id",
                "object_name",
                "level",
                "direction",
                "parent_id",
                "via_field",
                "scope_id_count",
                "discovered_row_count",
                "discovered_id_count",
            ],
        )
        writer.writeheader()
        writer.writerows(node_stats)


def load_delete_config(config_path: str) -> dict:
    cfg = load_config(config_path) or {}
    targets = cfg.get("targets") or cfg.get("objects") or []
    if not isinstance(targets, list) or not targets:
        console.print("[red]Delete config must include a non-empty 'targets' list[/red]")
        sys.exit(1)
    normalized_targets = []
    for entry in targets:
        object_name = (entry.get("object") or "").strip()
        where_clause = (entry.get("where") or entry.get("query_filter") or "").strip()
        if not object_name:
            console.print(f"[red]Delete target missing object name: {entry}[/red]")
            sys.exit(1)
        normalized_targets.append({
            "object": object_name,
            "where": where_clause,
        })
    return {
        "deletion": cfg.get("deletion", {}),
        "targets": normalized_targets,
    }


def _build_delete_object_configs(target_connector, targets: list[dict]) -> list[dict]:
    from core.field_rules import build_field_profile

    object_configs = []
    for target in targets:
        object_name = target["object"]
        profile = build_field_profile(target_connector, object_name)
        object_configs.append({
            "name": object_name,
            "fields": profile.include_fields,
            "query_filter": target.get("where", ""),
            "external_id": next(iter(profile.external_id_fields), "Id"),
        })
    return object_configs


def _reverse_delete_order(target_connector, targets: list[dict]) -> list[str]:
    from core.orchestrator import Orchestrator

    orchestrator = Orchestrator(target_connector)
    object_configs = _build_delete_object_configs(target_connector, targets)
    profiles = orchestrator._build_profiles(object_configs)
    graph, _reverse_graph = orchestrator._build_dependency_graph(object_configs, profiles)
    cycles = orchestrator._detect_cycles(graph)
    object_levels, _cyclic_pairs, _cycle_messages = orchestrator._topological_sort(
        object_configs, profiles, graph, cycles, None
    )
    return [obj for level in reversed(object_levels) for obj in level]


def _query_delete_candidates(connector, object_name: str, where_clause: str, fields: list[str], profile=None) -> pl.DataFrame:
    from core.field_rules import normalize_query_filter
    import re

    # Guard: check that fields referenced in the WHERE clause exist on the object.
    # If a field is missing, skip the query entirely (object wasn't migrated / field absent).
    if where_clause and profile is not None:
        all_field_names = set(profile.field_meta.keys()) | {"Id"}
        # Extract field names from the WHERE clause — anything that looks like an identifier
        SOQL_KEYWORDS = {"NULL", "NOT", "AND", "OR", "IN", "LIKE", "TRUE", "FALSE", "WHERE", "SELECT", "FROM"}
        referenced = set(re.findall(r'\b([A-Za-z][A-Za-z0-9_]*)\b', where_clause))
        missing = {f for f in referenced if f.upper() not in SOQL_KEYWORDS and f not in all_field_names}
        if missing:
            console.print(
                f"  [dim]Skipping {object_name}: WHERE field(s) {sorted(missing)} not found on object in target org[/dim]"
            )
            schema = {field: pl.Utf8 for field in dict.fromkeys(["Id"] + fields)}
            return pl.DataFrame(schema=schema)

    select_clause = ", ".join(dict.fromkeys(["Id"] + [field for field in fields if field != "Id"]))
    soql = f"SELECT {select_clause} FROM {object_name}"
    normalized_filter = normalize_query_filter(where_clause)
    if normalized_filter:
        soql += f" {normalized_filter}"
    records = connector.query(soql)
    rows = [{k: (str(v) if v is not None else None) for k, v in record.items() if k != "attributes"} for record in records]
    if not rows:
        schema = {field: pl.Utf8 for field in dict.fromkeys(["Id"] + fields)}
        return pl.DataFrame(schema=schema)
    schema = {col: pl.Utf8 for col in rows[0].keys()}
    return pl.DataFrame(rows, schema=schema)


def _build_internal_lookup_null_payload(profile, delete_df: pl.DataFrame, target_objects: set[str]) -> pl.DataFrame:
    if delete_df.is_empty():
        return pl.DataFrame()

    candidate_fields = []
    for field_name, refs in profile.lookup_fields.items():
        meta = profile.field_meta.get(field_name)
        if not meta or not meta.updateable or not meta.nillable:
            continue
        if any(ref in target_objects for ref in refs):
            candidate_fields.append(field_name)

    if not candidate_fields:
        return pl.DataFrame()

    selected_cols = ["Id"] + [field for field in candidate_fields if field in delete_df.columns]
    if len(selected_cols) == 1:
        return pl.DataFrame()

    null_df = delete_df.select(selected_cols)
    fields_to_clear = [
        field_name
        for field_name in selected_cols
        if field_name != "Id" and null_df.get_column(field_name).drop_nulls().len() > 0
    ]
    if not fields_to_clear:
        return pl.DataFrame()

    return null_df.select(["Id"] + fields_to_clear).with_columns([
        pl.lit(None).cast(pl.Utf8).alias(field_name)
        for field_name in fields_to_clear
    ])


def build_connectors():
    from core.connector import connector_from_env
    source = connector_from_env("SOURCE")
    target = connector_from_env("TARGET")
    return source, target


def is_flat_file_mode(config: dict) -> bool:
    """Return True when raw data comes from client files instead of a source org."""
    return str(config.get("migration", {}).get("input_mode") or "").strip().lower() == "flat_file"


def build_mode_connectors(config: dict):
    """Do not even construct a source connector for flat-file migrations."""
    from core.connector import connector_from_env
    target = connector_from_env("TARGET")
    source = None if is_flat_file_mode(config) else connector_from_env("SOURCE")
    return source, target


def _bind_load_progress(state, pipeline_id: str, object_name: str):
    """Flush running failed counts/files to run state after each Bulk batch."""
    def on_progress(progress):
        state.mark_object_load_progress(
            pipeline_id,
            object_name,
            loaded_success=int(progress.get("success_count") or 0),
            loaded_failed=int(progress.get("failed_count") or 0),
            failed_file=progress.get("failed_file"),
            success_file=progress.get("success_file"),
        )
        failed_delta = int(progress.get("failed_delta") or 0)
        failed_file = progress.get("failed_file")
        if failed_delta and failed_file:
            console.print(
                f"  [red]{failed_delta:,} failed in this batch "
                f"({int(progress.get('failed_count') or 0):,} failed so far) → {Path(failed_file).name}[/red]"
            )
    return on_progress


def run_object_migration(
    pipeline_id: str,
    obj_plan,
    source_connector,
    target_connector,
    stager,
    id_mapper,
    state,
    dry_run: bool,
    ref_ext_id_map: dict = None,
    skip_extract: bool = False,
    skip_transform: bool = False,
    stop_after_transform: bool = False,
    load_log_dir: Path | None = None,
    artifact_dir: Path | None = None,
):
    """Execute full ETL for a single object within a pipeline."""
    from core.extractor import Extractor
    from core.transformer import Transformer
    from core.loader import Loader

    object_name = obj_plan.object_name

    if state.is_object_completed(pipeline_id, object_name):
        console.print(f"  [dim]↷ Skipping {object_name} (already completed)[/dim]")
        return

    state.mark_object_running(pipeline_id, object_name)

    try:
        # --- EXTRACT ---
        if not skip_extract:
            console.print(f"  [cyan]↓ Extracting {object_name}...[/cyan]")
            extractor = Extractor(source_connector, stager)
            extracted = extractor.extract(object_name, obj_plan.soql, scope_rules=obj_plan.scope_rules)
            state.mark_object_extracted(pipeline_id, object_name, extracted)
            console.print(f"  [green]  ✓ Extracted {extracted:,} records[/green]")

        # --- TRANSFORM ---
        console.print(f"  [cyan]⇄ Transforming {object_name}...[/cyan]")
        from core.field_rules import build_field_profile
        mask_emails = os.environ.get("ETL_MASK_EMAILS", "").strip().lower() in ("1", "true", "yes")
        remove_email_mask = os.environ.get("ETL_REMOVE_EMAIL_MASK", "").strip().lower() in ("1", "true", "yes")
        profile = build_field_profile(source_connector, object_name)
        transformer = Transformer(
            stager,
            id_mapper,
            source_connector,
            target_connector,
            mask_emails=mask_emails,
            remove_email_mask=remove_email_mask,
        )
        transformed = transformer.transform(
            object_name, profile, obj_plan.transform_rules, obj_plan.cyclic_fields,
            external_id_field=obj_plan.external_id_field,
            ref_ext_id_map=ref_ext_id_map or {},
            lookup_mappings=obj_plan.lookup_mappings,
            record_type_mappings=obj_plan.record_type_mappings,
            source_key_field=obj_plan.source_key_field,
        )
        state.mark_object_transformed(pipeline_id, object_name, transformed)
        console.print(f"  [green]  ✓ Transformed {transformed:,} records[/green]")

        if artifact_dir and transformed > 0:
            xfm_csv = ensure_stage_dir(artifact_dir, "xfm") / f"{object_name}.csv"
            write_stage_csv(stager, object_name, xfm_csv, stage="xfm")
            console.print(f"  [dim]    xfm -> {xfm_csv}[/dim]")

        if stop_after_transform:
            reason = "Paused after transform for review"
            state.mark_object_stopped(pipeline_id, object_name, reason)
            console.print(f"  [yellow]⏸ {object_name}: transform complete, paused before load for review[/yellow]")
            return

        # --- LOAD ---
        console.print(f"  [cyan]↑ Loading {object_name}...[/cyan]")
        loader = Loader(target_connector, stager, id_mapper, log_root=load_log_dir)
        result = loader.load(
            object_name,
            external_id_field=obj_plan.external_id_field,
            operation=obj_plan.operation,
            dry_run=dry_run,
            on_progress=_bind_load_progress(state, pipeline_id, object_name),
        )
        state.mark_object_completed(
            pipeline_id, object_name,
            result.success_count, result.failed_count, result.failed_file, result.success_file
        )

        status_str = f"[green]✓ {result.success_count:,} loaded[/green]"
        if result.failed_count:
            status_str += f" [red]| {result.failed_count:,} failed → {Path(result.failed_file).name}[/red]"
        console.print(f"  {status_str}")

    except Exception as e:
        state.mark_object_failed(pipeline_id, object_name, str(e))
        raise


def run_object_migration_from_stage(
    start_stage: str,
    pipeline_id: str,
    obj_plan,
    source_connector,
    target_connector,
    stager,
    id_mapper,
    state,
    dry_run: bool,
    ref_ext_id_map: dict = None,
    stop_after_transform: bool = False,
    load_log_dir: Path | None = None,
    artifact_dir: Path | None = None,
):
    if start_stage == "extract":
        return run_object_migration(
            pipeline_id,
            obj_plan,
            source_connector,
            target_connector,
            stager,
            id_mapper,
            state,
            dry_run,
            ref_ext_id_map=ref_ext_id_map,
            skip_extract=True,
            stop_after_transform=stop_after_transform,
            load_log_dir=load_log_dir,
            artifact_dir=artifact_dir,
        )

    if start_stage == "transform":
        return run_object_migration(
            pipeline_id,
            obj_plan,
            source_connector,
            target_connector,
            stager,
            id_mapper,
            state,
            dry_run,
            ref_ext_id_map=ref_ext_id_map,
            skip_extract=True,
            stop_after_transform=stop_after_transform,
            load_log_dir=load_log_dir,
            artifact_dir=artifact_dir,
        )

    if start_stage != "load":
        raise ValueError(f"Unsupported start stage: {start_stage}")

    from core.loader import Loader

    object_name = obj_plan.object_name

    if state.is_object_completed(pipeline_id, object_name):
        console.print(f"  [dim]â†· Skipping {object_name} (already completed)[/dim]")
        return

    if not stager.table_exists(stager.xfm_table(object_name)):
        raise RuntimeError(
            f"No transformed staging data found for {object_name}. "
            "Choose 'Start From Transform' or 'Start From Extraction' first."
        )

    state.mark_object_running(pipeline_id, object_name)

    try:
        xfm_count = stager.get_xfm_count(object_name)
        console.print(f"  [dim]â†º Using existing transformed data for {object_name} ({xfm_count:,} records)[/dim]")
        console.print(f"  [cyan]â†‘ Loading {object_name}...[/cyan]")
        loader = Loader(target_connector, stager, id_mapper, log_root=load_log_dir)
        result = loader.load(
            object_name,
            external_id_field=obj_plan.external_id_field,
            operation=obj_plan.operation,
            dry_run=dry_run,
            on_progress=_bind_load_progress(state, pipeline_id, object_name),
        )
        state.mark_object_completed(
            pipeline_id,
            object_name,
            result.success_count,
            result.failed_count,
            result.failed_file,
            result.success_file,
        )

        status_str = f"[green]âœ“ {result.success_count:,} loaded[/green]"
        if result.failed_count:
            status_str += f" [red]| {result.failed_count:,} failed â†’ {Path(result.failed_file).name}[/red]"
        if result.blocked_count:
            status_str += f" [yellow]| {result.blocked_count:,} blocked â†’ {Path(result.blocked_file).name}[/yellow]"
        console.print(f"  {status_str}")
    except Exception as e:
        state.mark_object_failed(pipeline_id, object_name, str(e))
        raise


# ------------------------------------------------------------------ #
#  CLI group                                                          #
# ------------------------------------------------------------------ #

@click.group()
def cli():
    """SF ETL — Salesforce to Salesforce data migration tool."""
    pass


# ------------------------------------------------------------------ #
#  run                                                                #
# ------------------------------------------------------------------ #

@cli.command()
@click.option("--config", "-c", default="config/migration.yaml", help="Path to migration config YAML")
@click.option("--pipeline", "-p", default=None, help="Run only this pipeline ID")
@click.option("--from-pipeline", default=None, help="Run from this pipeline ID onwards")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would happen without moving data")
@click.option("--fresh", is_flag=True, default=False, help="Ignore previous run state, start fresh")
@click.option("--write-artifacts", is_flag=True, default=False, help="Write raw/xfm CSV artifacts to exports/")
@click.option(
    "--start-stage",
    type=click.Choice(["extract", "transform", "load"]),
    default="extract",
    show_default=True,
    help="Start from extraction, transform, or load using existing staged data.",
)
def run(config, pipeline, from_pipeline, dry_run, fresh, write_artifacts, start_stage):
    """Run the migration (all pipelines or a specific one)."""
    from core.orchestrator import Orchestrator
    from core.topology_extractor import TopologyExtractor
    from core.stager import Stager
    from core.id_mapper import IDMapper
    from core.state_manager import StateManager, Status
    from core.field_rules import build_field_profile
    from core.transformer import Transformer
    from core.extension_registry import bootstrap_extensions
    from core.extension_runner import run_pipeline_extensions

    bootstrap_extensions()

    cfg = load_config(config)
    source, target = build_mode_connectors(cfg)
    if source is None:
        source = target
        console.print("[dim]Flat-file mode: source org connection is not required.[/dim]")

    console.print("[bold]Connecting to Salesforce orgs...[/bold]")
    source.connect()
    console.print(f"  [green]✓ Source: {source.instance_url}[/green]")
    target.connect()
    console.print(f"  [green]✓ Target: {target.instance_url}[/green]")

    stager = Stager()
    id_mapper = IDMapper()
    state = StateManager()
    migration_name = cfg.get("migration", {}).get("name", "Migration")
    state.init_run(migration_name, fresh=fresh, run_key=os.environ.get("ETL_RUN_KEY"))
    if fresh and start_stage == "extract":
        console.print("[yellow]Fresh extract run detected - clearing local staging and ID-map DuckDB state.[/yellow]")
        clear_local_duckdb_state(stager, id_mapper)

    orchestrator = Orchestrator(source)
    exec_plan = orchestrator.build_plan(cfg)

    if dry_run:
        console.print("\n[bold yellow]DRY RUN MODE — no data will be moved[/bold yellow]")
        orchestrator.display_plan(exec_plan)
    console.print(f"[dim]Start stage -> {start_stage}[/dim]")

    run_label = resolve_env_run_dir_name()
    export_run_dir = create_named_run_dir("exports", run_label)
    artifact_dir = create_named_run_dir("exports", run_label) if write_artifacts else None
    log_run_dir = create_named_run_dir("logs", run_label)
    if artifact_dir:
        console.print(f"\n[dim]Artifact CSV output -> {artifact_dir}/[/dim]")
    console.print(f"[dim]Stage-wise logs -> {log_run_dir}/[/dim]")

    # Filter pipelines
    pipelines_to_run = exec_plan.pipelines
    if pipeline:
        pipelines_to_run = [p for p in exec_plan.pipelines if p.pipeline_id == pipeline]
        if not pipelines_to_run:
            console.print(f"[red]Pipeline '{pipeline}' not found in config[/red]")
            sys.exit(1)
    elif from_pipeline:
        ids = [p.pipeline_id for p in exec_plan.pipelines]
        if from_pipeline not in ids:
            console.print(f"[red]Pipeline '{from_pipeline}' not found in config[/red]")
            sys.exit(1)
        start_idx = ids.index(from_pipeline)
        pipelines_to_run = exec_plan.pipelines[start_idx:]

    pipeline_dir_map = build_pipeline_artifact_dir_map(pipelines_to_run)
    stop_after_transform = os.environ.get("ETL_STOP_AFTER_TRANSFORM", "").strip().lower() in ("1", "true", "yes")

    if (pipeline or from_pipeline) and not fresh:
        for pipe in pipelines_to_run:
            state.reset_pipeline(pipe.pipeline_id)
        if pipeline:
            console.print(f"[yellow]Pipeline '{pipeline}' reset to PENDING for re-run from stage '{start_stage}'.[/yellow]")
        else:
            console.print(f"[yellow]Pipelines from '{from_pipeline}' onward reset to PENDING for re-run from stage '{start_stage}'.[/yellow]")

    pipeline_dir_map = build_pipeline_artifact_dir_map(pipelines_to_run)

    # Execute pipelines
    for pipe in pipelines_to_run:
        pid = pipe.pipeline_id
        pipeline_dir_name = pipeline_dir_map[pid]
        pipeline_artifact_dir = ensure_stage_dir(artifact_dir, pipeline_dir_name) if artifact_dir else None
        pipeline_log_dir = ensure_stage_dir(log_run_dir, pipeline_dir_name)
        state.mark_pipeline_running(pid)
        state.set_pipeline_artifact_dir_name(pid, pipeline_dir_name)
        pipeline_failed = False
        state.mark_pipeline_running(pid)
        state.set_pipeline_artifact_dir_name(pid, pipeline_dir_name)
        pipeline_failed = False
        pipeline_export_dir = ensure_stage_dir(export_run_dir, pipeline_dir_name)
        pipeline_xfm_dir = ensure_stage_dir(pipeline_export_dir, "xfm")

        if state.is_pipeline_completed(pid) and not pipeline:
            console.print(f"\n[dim]↷ Skipping pipeline '{pipe.name}' (already completed)[/dim]")
            continue

        console.print(f"\n[bold yellow]▶ Pipeline: {pipe.name}[/bold yellow]")
        state.mark_pipeline_running(pid)
        state.set_pipeline_artifact_dir_name(pid, pipeline_dir_name)

        # Build map of {object_name: external_id_field} for this pipeline
        ref_ext_id_map = {
            o.object_name: o.external_id_field
            for level in pipe.object_levels
            for o in level
        }

        try:
            console.print("  [bold]Extraction:[/bold] topology-driven discovery + final fetch")
            profiles = {
                cfg["name"]: build_field_profile(source, cfg["name"])
                for cfg in pipe.object_configs
            }
            topology_extractor = TopologyExtractor(source, stager)
            staged_counts = topology_extractor.extract_pipeline(pipe, profiles, overwrite_existing=True)
            topology_report = topology_extractor.get_last_pipeline_report()
            configured_max_depth = topology_report.get("configured_max_depth", -1)
            actual_max_depth = topology_report.get("actual_max_depth", -1)
            console.print(
                f"  [dim]Actual topology reach: level {actual_max_depth} "
                f"(configured max: {configured_max_depth})[/dim]"
            )
            report_path = ensure_stage_dir(pipeline_log_dir, "extract") / f"{pipeline_dir_name}_topology_actual_depth.csv"
            write_topology_report_csv(report_path, topology_extractor.last_node_stats)
            if pipeline_artifact_dir:
                artifact_report_path = ensure_stage_dir(pipeline_artifact_dir, "extract") / f"{pipeline_dir_name}_topology_actual_depth.csv"
                write_topology_report_csv(artifact_report_path, topology_extractor.last_node_stats)
            console.print(f"  [dim]Topology node report → {report_path}[/dim]")
            for object_name, count in staged_counts.items():
                state.mark_object_extracted(pid, object_name, count)
                if pipeline_artifact_dir and count > 0:
                    raw_csv = ensure_stage_dir(pipeline_artifact_dir, "raw") / f"{object_name}.csv"
                    write_stage_csv(stager, object_name, raw_csv, stage="raw")
                    console.print(f"  [dim]    raw -> {raw_csv}[/dim]")
                console.print(f"  [green]  ✓ {object_name}: {count:,} staged[/green]")

            run_pipeline_extensions(
                stage="pre_transform",
                pipeline=pipe,
                source_connector=source,
                target_connector=target,
                stager=stager,
                id_mapper=id_mapper,
                log_root=pipeline_log_dir,
            )

            for level_idx, level in enumerate(pipe.object_levels):
                console.print(f"  [bold]Load Level {level_idx}:[/bold] {', '.join(o.object_name for o in level)}")
                for obj_plan in level:
                    run_object_migration(
                        pid, obj_plan,
                        source, target,
                        stager, id_mapper, state, dry_run,
                        ref_ext_id_map=ref_ext_id_map,
                        skip_extract=True,
                        stop_after_transform=stop_after_transform,
                        load_log_dir=pipeline_log_dir,
                        artifact_dir=pipeline_artifact_dir,
                    )

            if stop_after_transform:
                reason = "Paused after transform for review"
                state.mark_pipeline_stopped(pid, reason)
                console.print(f"[yellow]⏸ Pipeline '{pipe.name}' paused after transform. Review transformed files and resume at Load.[/yellow]")
                break

            # --- PASS 2: resolve deferred self-lookups (e.g. Location__c.Location__c) ---
            ordered_object_plans = [o for level in pipe.object_levels for o in level]
            cyclic_object_plans = [o for o in ordered_object_plans if o.cyclic_fields]
            if not dry_run and cyclic_object_plans:
                console.print("  [yellow]↺ Pass 2: resolving deferred cyclic/self-lookup references...[/yellow]")
                for obj_plan in cyclic_object_plans:
                    profile = build_field_profile(source, obj_plan.object_name)
                    transformer = Transformer(stager, id_mapper, source, target)
                    results = transformer.resolve_cyclic_pass2(
                        obj_plan.object_name,
                        profile,
                        obj_plan.cyclic_fields,
                        target,
                        attempt_number=2,
                        log_root=pipeline_log_dir,
                        referenced_external_id_field=obj_plan.external_id_field,
                        lookup_mappings=obj_plan.lookup_mappings,
                    )
                    cyclic_failed = len(transformer.last_cyclic_failed_ids)
                    if cyclic_failed:
                        cyclic_failed_file = (
                            transformer.last_cyclic_failed_files[-1]
                            if transformer.last_cyclic_failed_files else None
                        )
                        state.apply_cyclic_outcome(
                            pid, obj_plan.object_name, cyclic_failed, cyclic_failed_file
                        )
                        console.print(
                            f"  [red]✗ {obj_plan.object_name}: {cyclic_failed:,} record(s) "
                            "failed during Pass 2 relationship updates[/red]"
                        )
                    for field_name, count in results.items():
                        console.print(f"  [green]  ✓ {obj_plan.object_name}.{field_name}: {count:,} records updated[/green]")

            if not dry_run:
                run_pipeline_extensions(
                    stage="post_load",
                    pipeline=pipe,
                    source_connector=source,
                    target_connector=target,
                    stager=stager,
                    id_mapper=id_mapper,
                    log_root=pipeline_log_dir,
                )

            state.mark_pipeline_completed(pid)
            console.print(f"[green]✓ Pipeline '{pipe.name}' completed[/green]")

        except Exception as e:
            state.mark_pipeline_failed(pid, str(e))
            console.print(f"[red]✗ Pipeline '{pipe.name}' FAILED: {e}[/red]")
            console.print("[yellow]Downstream pipelines will not run. Use 'retry' to resume.[/yellow]")
            break
        if not pipeline_failed:
            state.mark_pipeline_completed(pid)

    stager.close()
    id_mapper.close()
    console.print("\n[bold]Run complete. Use 'sf-etl status' to view results.[/bold]")


# ------------------------------------------------------------------ #
#  extract                                                            #
# ------------------------------------------------------------------ #

@cli.command()
@click.option("--config", "-c", default="config/migration.yaml", help="Path to migration config YAML")
@click.option("--pipeline", "-p", default=None, help="Extract only this pipeline ID")
@click.option("--fresh", is_flag=True, default=False, help="Clear previous staging data before extracting")
@click.option("--no-csv", is_flag=True, default=False, help="Skip writing CSV files after extraction")
def extract(config, pipeline, fresh, no_csv):
    """Extract data from source Salesforce org into local staging (DuckDB) and CSV."""
    from core.orchestrator import Orchestrator
    from core.topology_extractor import TopologyExtractor
    from core.stager import Stager
    from core.state_manager import StateManager
    from core.field_rules import build_field_profile
    from datetime import datetime

    cfg = load_config(config)
    source, _ = build_connectors()

    console.print("[bold]Connecting to source org...[/bold]")
    source.connect()
    console.print(f"  [green]✓ Source: {source.instance_url}[/green]")

    stager = Stager()
    id_mapper = IDMapper()
    state = StateManager()
    migration_name = cfg.get("migration", {}).get("name", "Migration")
    state.init_run(migration_name, fresh=fresh)
    if fresh:
        console.print("[yellow]Fresh extract detected - clearing local staging and ID-map DuckDB state.[/yellow]")
        clear_local_duckdb_state(stager, id_mapper)

    orchestrator = Orchestrator(source)
    exec_plan = orchestrator.build_plan(cfg)
    orchestrator.display_plan(exec_plan)

    pipelines_to_run = exec_plan.pipelines
    if pipeline:
        pipelines_to_run = [p for p in exec_plan.pipelines if p.pipeline_id == pipeline]
        if not pipelines_to_run:
            console.print(f"[red]Pipeline '{pipeline}' not found in config[/red]")
            sys.exit(1)
    pipeline_dir_map = build_pipeline_artifact_dir_map(pipelines_to_run)

    # Create a unique timestamped folder for this extraction run
    csv_dir = create_run_dir("exports")
    if not no_csv:
        console.print(f"\n[dim]CSV output → {csv_dir}/[/dim]")

    total_extracted = 0
    extracted_objects = 0

    for pipe in pipelines_to_run:
        pid = pipe.pipeline_id
        pipeline_dir_name = pipeline_dir_map[pid]
        pipeline_csv_dir = ensure_stage_dir(csv_dir, pipeline_dir_name)
        console.print(f"\n[bold yellow]▶ Pipeline: {pipe.name}[/bold yellow]")
        if fresh:
            for cfg in pipe.object_configs:
                stager.clear_object(cfg["name"])

        try:
            profiles = {
                cfg["name"]: build_field_profile(source, cfg["name"])
                for cfg in pipe.object_configs
            }
            topology_extractor = TopologyExtractor(source, stager)
            staged_counts = topology_extractor.extract_pipeline(pipe, profiles, overwrite_existing=True)
            topology_report = topology_extractor.get_last_pipeline_report()
            configured_max_depth = topology_report.get("configured_max_depth", -1)
            actual_max_depth = topology_report.get("actual_max_depth", -1)
            console.print(
                f"  [dim]Actual topology reach: level {actual_max_depth} "
                f"(configured max: {configured_max_depth})[/dim]"
            )
            if not no_csv:
                report_path = ensure_stage_dir(pipeline_csv_dir, "extract") / f"topology_{pipeline_dir_name}_actual_depth.csv"
                write_topology_report_csv(report_path, topology_extractor.last_node_stats)
                console.print(f"  [dim]Topology node report → {report_path}[/dim]")

            for object_name, count in staged_counts.items():
                state.mark_object_extracted(pid, object_name, count)
                total_extracted += count
                extracted_objects += 1
                console.print(f"  [green]  ✓ {object_name}: {count:,} records → staging (raw_{object_name})[/green]")

                if not no_csv and count > 0:
                    csv_file = ensure_stage_dir(pipeline_csv_dir, "raw") / f"{object_name}.csv"
                    write_stage_csv(stager, object_name, csv_file, stage="raw")
                    console.print(f"  [dim]    → {csv_file}[/dim]")
        except Exception as e:
            console.print(f"  [red]  ✗ Failed to extract pipeline {pipe.name}: {e}[/red]")
            logger.exception(f"Extract failed for pipeline {pipe.name}")

    stager.close()
    id_mapper.close()
    console.print("\n[bold cyan]Extract Summary[/bold cyan]")
    console.print(f"Objects: {extracted_objects:,}")
    console.print(f"Processed: {total_extracted:,}")
    console.print(f"[green]Staged: {total_extracted:,}[/green]")
    if not no_csv:
        console.print(f"[dim]CSVs saved to: {csv_dir}/[/dim]")


# ------------------------------------------------------------------ #
#  transform                                                          #
# ------------------------------------------------------------------ #

@cli.command()
@click.option("--config", "-c", default="config/migration.yaml", help="Path to migration config YAML")
@click.option("--pipeline", "-p", default=None, help="Transform only this pipeline ID")
@click.option("--object", "-o", "object_name", default=None, help="Transform only this object")
def transform(config, pipeline, object_name):
    """Transform staged raw data (ID remapping, rules) into xfm tables."""
    from core.orchestrator import Orchestrator
    from core.transformer import Transformer
    from core.stager import Stager
    from core.id_mapper import IDMapper
    from core.field_rules import build_field_profile
    from core.state_manager import StateManager

    cfg = load_config(config)
    source, target = build_mode_connectors(cfg)
    flat_file_mode = source is None
    if flat_file_mode:
        source = target
        console.print("[dim]Flat-file mode: source org connection is not required.[/dim]")

    if not flat_file_mode:
        console.print("[bold]Connecting to source org for metadata...[/bold]")
    source.connect()
    console.print(f"  [green]✓ Source: {source.instance_url}[/green]")
    console.print("[bold]Connecting to target org for transform-time lookup mapping...[/bold]")
    target.connect()
    console.print(f"  [green]✓ Target: {target.instance_url}[/green]")

    stager = Stager()
    id_mapper = IDMapper()
    state = StateManager()
    migration_name = cfg.get("migration", {}).get("name", "Migration")
    state.init_run(migration_name, fresh=False, run_key=os.environ.get("ETL_RUN_KEY"))

    orchestrator = Orchestrator(source)
    exec_plan = orchestrator.build_plan(cfg)

    pipelines_to_run = exec_plan.pipelines
    if pipeline:
        pipelines_to_run = [p for p in exec_plan.pipelines if p.pipeline_id == pipeline]
        if not pipelines_to_run:
            console.print(f"[red]Pipeline '{pipeline}' not found[/red]")
            sys.exit(1)

    pipeline_dir_map = build_pipeline_artifact_dir_map(pipelines_to_run)

    planned_objects = {
        obj.object_name
        for pipe in pipelines_to_run
        for level in pipe.object_levels
        for obj in level
    }
    if object_name and object_name not in planned_objects:
        console.print(
            f"[red]Object '{object_name}' not found in the selected execution plan. "
            f"Available: {', '.join(sorted(planned_objects))}[/red]"
        )
        sys.exit(1)

    total_transformed = 0
    transformed_objects = 0
    csv_dir = create_named_run_dir("exports", resolve_env_run_dir_name())
    if csv_dir:
        console.print(f"\n[dim]Transform CSV output → {csv_dir}/[/dim]")
    else:
        console.print("\n[dim]No extract run folder found under exports/. Transform will update staging only.[/dim]")

    for pipe in pipelines_to_run:
        pid = pipe.pipeline_id
        pipeline_dir_name = pipeline_dir_map[pid]
        pipeline_csv_dir = ensure_stage_dir(csv_dir, pipeline_dir_name) if csv_dir else None
        state.mark_pipeline_running(pid)
        state.set_pipeline_artifact_dir_name(pid, pipeline_dir_name)
        pipeline_failed = False
        console.print(f"\n[bold yellow]▶ Pipeline: {pipe.name}[/bold yellow]")

        ref_ext_id_map = {
            o.object_name: o.external_id_field
            for level in pipe.object_levels
            for o in level
        }

        for level_idx, level in enumerate(pipe.object_levels):
            console.print(f"  [bold]Level {level_idx}:[/bold] {', '.join(o.object_name for o in level)}")

            for obj_plan in level:
                current_object_name = obj_plan.object_name
                if object_name and current_object_name != object_name:
                    continue

                raw_count = stager.get_raw_count(current_object_name)
                if raw_count == 0:
                    console.print(f"  [yellow]  ⚠ {current_object_name} — no raw data, run extract first[/yellow]")
                    continue

                console.print(f"  [cyan]⇄ Transforming {current_object_name} ({raw_count:,} records)...[/cyan]")
                try:
                    profile = build_field_profile(source, current_object_name)
                    mask_emails = os.environ.get("ETL_MASK_EMAILS", "").strip().lower() in ("1", "true", "yes")
                    remove_email_mask = os.environ.get("ETL_REMOVE_EMAIL_MASK", "").strip().lower() in ("1", "true", "yes")
                    transformer = Transformer(
                        stager,
                        id_mapper,
                        source,
                        target,
                        mask_emails=mask_emails,
                        remove_email_mask=remove_email_mask,
                    )
                    transformed = transformer.transform(
                        current_object_name, profile, obj_plan.transform_rules, obj_plan.cyclic_fields,
                        external_id_field=obj_plan.external_id_field,
                        ref_ext_id_map=ref_ext_id_map,
                        lookup_mappings=obj_plan.lookup_mappings,
                        record_type_mappings=obj_plan.record_type_mappings,
                        source_key_field=obj_plan.source_key_field,
                    )
                    state.mark_object_transformed(pid, current_object_name, transformed)
                    total_transformed += transformed
                    transformed_objects += 1
                    console.print(f"  [green]  ✓ {transformed:,} records → xfm_{current_object_name}[/green]")
                    if pipeline_csv_dir and transformed > 0:
                        csv_file = ensure_stage_dir(pipeline_csv_dir, "xfm") / f"{current_object_name}.csv"
                        write_stage_csv(stager, current_object_name, csv_file, stage="xfm")
                        console.print(f"  [dim]    → {csv_file}[/dim]")
                except Exception as e:
                    console.print(f"  [red]  ✗ Failed to transform {current_object_name}: {e}[/red]")
                    state.mark_pipeline_failed(pid, str(e))
                    pipeline_failed = True
                    logger.exception(f"Transform failed for {current_object_name}")

    stager.close()
    id_mapper.close()
    console.print("\n[bold cyan]Transform Summary[/bold cyan]")
    console.print(f"Objects: {transformed_objects:,}")
    console.print(f"Processed: {total_transformed:,}")
    console.print(f"[green]Ready to load: {total_transformed:,}[/green]")


# ------------------------------------------------------------------ #
#  load                                                               #
# ------------------------------------------------------------------ #

@cli.command()
@click.option("--config", "-c", default="config/migration.yaml", help="Path to migration config YAML")
@click.option("--pipeline", "-p", default=None, help="Load only this pipeline ID")
@click.option("--dry-run", is_flag=True, default=False, help="Show counts without loading to target")
@click.option("--object", "-o", "object_name", default=None, help="Load only this object")
def load(config, pipeline, dry_run, object_name):
    """Load transformed (xfm) staged data into the target Salesforce org."""
    from core.orchestrator import Orchestrator
    from core.loader import Loader
    from core.stager import Stager
    from core.id_mapper import IDMapper
    from core.state_manager import StateManager
    from core.field_rules import build_field_profile
    from core.transformer import Transformer
    from core.extension_registry import bootstrap_extensions
    from core.extension_runner import run_pipeline_extensions

    bootstrap_extensions()

    cfg = load_config(config)
    source, target = build_mode_connectors(cfg)
    flat_file_mode = source is None
    if flat_file_mode:
        source = target
        console.print("[dim]Flat-file mode: source org connection is not required.[/dim]")

    console.print("[bold]Connecting to Salesforce orgs...[/bold]")
    source.connect()
    console.print(f"  [green]✓ Source: {source.instance_url}[/green]")
    target.connect()
    console.print(f"  [green]✓ Target: {target.instance_url}[/green]")

    stager = Stager()
    id_mapper = IDMapper()
    state = StateManager()
    migration_name = cfg.get("migration", {}).get("name", "Migration")
    state.init_run(migration_name, fresh=False, run_key=os.environ.get("ETL_RUN_KEY"))

    orchestrator = Orchestrator(source)
    exec_plan = orchestrator.build_plan(cfg)

    pipelines_to_run = exec_plan.pipelines
    if pipeline:
        pipelines_to_run = [p for p in exec_plan.pipelines if p.pipeline_id == pipeline]
        if not pipelines_to_run:
            console.print(f"[red]Pipeline '{pipeline}' not found[/red]")
            sys.exit(1)

    planned_objects = {
        obj.object_name
        for pipe in pipelines_to_run
        for level in pipe.object_levels
        for obj in level
    }
    if object_name and object_name not in planned_objects:
        console.print(
            f"[red]Object '{object_name}' not found in the selected execution plan. "
            f"Available: {', '.join(sorted(planned_objects))}[/red]"
        )
        sys.exit(1)

    if dry_run:
        console.print("\n[bold yellow]DRY RUN MODE — no data will be moved to target[/bold yellow]")

    total_success = 0
    total_failed = 0
    total_blocked = 0
    run_label = resolve_env_run_dir_name()
    log_run_dir = create_named_run_dir("logs", run_label)
    export_run_dir = create_named_run_dir("exports", run_label)
    console.print(f"\n[dim]Stage-wise logs -> {log_run_dir}/[/dim]")
    pipeline_dir_map = build_pipeline_artifact_dir_map(pipelines_to_run)

    for pipe in pipelines_to_run:
        pid = pipe.pipeline_id
        pipeline_dir_name = pipeline_dir_map[pid]
        state.mark_pipeline_running(pid)
        state.set_pipeline_artifact_dir_name(pid, pipeline_dir_name)
        pipeline_failed = False
        pipeline_log_dir = ensure_stage_dir(log_run_dir, pipeline_dir_name)
        console.print(f"\n[bold yellow]▶ Pipeline: {pipe.name}[/bold yellow]")
        ordered_object_plans = [o for level in pipe.object_levels for o in level]
        selected_object_names = {
            obj_plan.object_name
            for obj_plan in ordered_object_plans
            if not object_name or obj_plan.object_name == object_name
        }
        artifact_xfm_dir = find_latest_pipeline_stage_dir("exports", pipeline_dir_name, "xfm", selected_object_names)
        pipeline_export_dir = ensure_stage_dir(export_run_dir, pipeline_dir_name)
        pipeline_xfm_dir = ensure_stage_dir(pipeline_export_dir, "xfm")
        if artifact_xfm_dir:
            synced_count, synced_objects = sync_stage_csvs_to_staging(
                stager,
                artifact_xfm_dir,
                selected_object_names,
                stage="xfm",
            )
            if synced_count:
                console.print(
                    f"  [dim]↺ Reused staged xfm tables from {artifact_xfm_dir.parent.parent.name}/{pipeline_dir_name}/xfm "
                    f"for {synced_count} object(s): {', '.join(synced_objects)}[/dim]"
                )
                source_run_dir_name = artifact_xfm_dir.parent.parent.name
                source_id_map = Path("state") / f"{source_run_dir_name}_id_map.duckdb"
                imported_deferred = id_mapper.import_deferred_tables(
                    source_id_map,
                    set(synced_objects),
                )
                if imported_deferred:
                    imported_rows = sum(imported_deferred.values())
                    console.print(
                        f"  [dim]Reused {len(imported_deferred)} deferred reference table(s) "
                        f"({imported_rows:,} row(s)) from {source_run_dir_name}[/dim]"
                    )
        else:
            console.print("  [dim]No xfm artifact folder found for this pipeline. Using existing staged xfm data.[/dim]")
        if artifact_xfm_dir:
            console.print(
                f"  [dim]→ Writing current retry artifacts to {export_run_dir.name}/{pipeline_dir_name}/xfm[/dim]"
            )
        backfilled_count, backfilled_objects = backfill_missing_stage_csvs(
            stager,
            pipeline_xfm_dir,
            selected_object_names,
            stage="xfm",
        )
        if backfilled_count:
            console.print(
                f"  [dim]Backfilled missing xfm CSV artifact(s) from staging for "
                f"{backfilled_count} object(s): {', '.join(backfilled_objects)}[/dim]"
            )
        profile_cache = {
            obj_plan.object_name: build_field_profile(source, obj_plan.object_name)
            for obj_plan in ordered_object_plans
        }

        for level_idx, level in enumerate(pipe.object_levels):
            console.print(f"  [bold]Level {level_idx}:[/bold] {', '.join(o.object_name for o in level)}")

            for obj_plan in level:
                current_object_name = obj_plan.object_name
                if object_name and current_object_name != object_name:
                    continue

                xfm_count = stager.get_xfm_count(current_object_name)
                if xfm_count == 0:
                    console.print(f"  [yellow]  ⚠ {current_object_name} — no transformed data, run transform first[/yellow]")
                    continue

                console.print(
                    f"  [cyan]↑ Pass 1 / Attempt 1: Loading {current_object_name} "
                    f"(level {level_idx}, {xfm_count:,} records)...[/cyan]"
                )
                try:
                    loader = Loader(target, stager, id_mapper, log_root=pipeline_log_dir)
                    profile = profile_cache[current_object_name]
                    result = loader.load(
                        current_object_name,
                        external_id_field=obj_plan.external_id_field,
                        operation=obj_plan.operation,
                        dry_run=dry_run,
                        pass_number=1,
                        attempt_number=1,
                        on_progress=_bind_load_progress(state, pid, current_object_name),
                    )
                    state.mark_object_completed(
                        pid, current_object_name,
                        result.success_count, result.failed_count, result.failed_file, result.success_file
                    )
                    total_success += result.success_count
                    total_failed += result.failed_count
                    total_blocked += result.blocked_count

                    status_str = f"[green]✓ {result.success_count:,} loaded[/green]"
                    if result.failed_count:
                        status_str += f" [red]| {result.failed_count:,} failed → {Path(result.failed_file).name}[/red]"
                    if result.blocked_count:
                        status_str += f" [yellow]| {result.blocked_count:,} blocked → {Path(result.blocked_file).name}[/yellow]"
                    console.print(f"  {status_str}")

                except Exception as e:
                    console.print(f"  [red]  ✗ Failed to load {current_object_name}: {e}[/red]")
                    state.mark_pipeline_failed(pid, str(e))
                    pipeline_failed = True
                    logger.exception(f"Load failed for {current_object_name}")

        if not dry_run:
            console.print("  [yellow]↺ Pass 2 / Attempt 2: resolving deferred cyclic references after all Pass 1 loads...[/yellow]")
            for obj_plan in ordered_object_plans:
                if not obj_plan.cyclic_fields:
                    continue
                profile = profile_cache[obj_plan.object_name]
                transformer = Transformer(stager, id_mapper, source, target)
                transformer.resolve_cyclic_pass2(
                    obj_plan.object_name,
                    profile,
                    obj_plan.cyclic_fields,
                    target,
                    attempt_number=2,
                    log_root=pipeline_log_dir,
                    referenced_external_id_field=obj_plan.external_id_field,
                    lookup_mappings=obj_plan.lookup_mappings,
                )
                cyclic_failed = len(transformer.last_cyclic_failed_ids)
                if cyclic_failed:
                    cyclic_failed_file = (
                        transformer.last_cyclic_failed_files[-1]
                        if transformer.last_cyclic_failed_files else None
                    )
                    state.apply_cyclic_outcome(
                        pid, obj_plan.object_name, cyclic_failed, cyclic_failed_file
                    )
                    total_success = max(0, total_success - cyclic_failed)
                    total_failed += cyclic_failed
                    console.print(
                        f"  [red]✗ {obj_plan.object_name}: {cyclic_failed:,} record(s) "
                        "failed during Pass 2 relationship updates[/red]"
                    )

            run_pipeline_extensions(
                stage="post_load",
                pipeline=pipe,
                source_connector=source,
                target_connector=target,
                stager=stager,
                id_mapper=id_mapper,
                log_root=pipeline_log_dir,
            )

        if not pipeline_failed:
            state.mark_pipeline_completed(pid)

    stager.close()
    id_mapper.close()
    console.print("\n[bold cyan]Load Summary[/bold cyan]")
    console.print(f"Processed: {total_success + total_failed + total_blocked:,}")
    console.print(f"[green]Succeeded: {total_success:,}[/green]")
    console.print(f"[red]Failed: {total_failed:,}[/red]")
    console.print(f"[yellow]Blocked: {total_blocked:,}[/yellow]")
    if total_failed or total_blocked:
        console.print("[yellow]Check logs/ folder for dead letter CSV files.[/yellow]")


# ------------------------------------------------------------------ #
#  export                                                             #
# ------------------------------------------------------------------ #

@cli.command()
@click.option("--stage", "-s", default="raw", type=click.Choice(["raw", "xfm", "both"]), help="Which stage to export: raw, xfm, or both")
@click.option("--object", "-o", "object_name", default=None, help="Export only this object (default: all staged objects)")
@click.option("--out-dir", default="exports", help="Directory to write CSV files to")
def export(stage, object_name, out_dir):
    """Export staged DuckDB data to CSV files for inspection."""
    from core.stager import Stager

    stager = Stager()
    out_path = create_run_dir(out_dir) if out_dir == "exports" else Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    staged = stager.list_staged_objects()
    if not staged:
        console.print("[yellow]No staged data found. Run 'extract' first.[/yellow]")
        stager.close()
        return

    if object_name:
        if object_name not in staged:
            console.print(f"[red]Object '{object_name}' not found in staging. Available: {', '.join(staged.keys())}[/red]")
            stager.close()
            return
        targets = {object_name: staged[object_name]}
    else:
        targets = staged

    stages_to_export = ["raw", "xfm"] if stage == "both" else [stage]
    total_files = 0

    for obj, counts in targets.items():
        for s in stages_to_export:
            count = counts.get(s, 0)
            if count == 0:
                continue

            csv_file = ensure_stage_dir(out_path, s) / f"{obj}.csv"
            console.print(f"  [cyan]→ Exporting {s}_{obj} ({count:,} records)...[/cyan]")

            chunks = stager.read_raw_chunks(obj) if s == "raw" else stager.read_xfm_chunks(obj)
            first_chunk = True
            with open(csv_file, "w", encoding="utf-8", newline="") as f:
                for chunk in chunks:
                    csv_str = chunk.write_csv(file=None)
                    if first_chunk:
                        f.write(csv_str)
                        first_chunk = False
                    else:
                        # Skip header line on subsequent chunks
                        newline_pos = csv_str.index("\n")
                        f.write(csv_str[newline_pos + 1:])

            console.print(f"  [green]  ✓ {csv_file}[/green]")
            total_files += 1

    stager.close()
    console.print(f"\n[bold green]Exported {total_files} file(s) to '{out_path}/'[/bold green]")


# ------------------------------------------------------------------ #
#  status                                                             #
# ------------------------------------------------------------------ #

@cli.command()
def status():
    """Show the current migration run status."""
    from core.state_manager import StateManager
    state = StateManager()
    state.print_summary()


# ------------------------------------------------------------------ #
#  verify-self-lookup                                                #
# ------------------------------------------------------------------ #

@cli.command("verify-self-lookup")
@click.option("--config", "-c", default="config/migration.yaml", help="Path to migration config YAML")
@click.option("--pipeline", "-p", required=True, help="Pipeline ID containing the object")
@click.option("--object", "-o", "object_name", required=True, help="Object API name, e.g. Location__c")
@click.option("--field", "-f", "field_name", default=None, help="Self-lookup field API name (auto-detected if omitted)")
@click.option("--sample-size", default=25, show_default=True, help="How many missing rows to include in the CSV sample")
def verify_self_lookup(config, pipeline, object_name, field_name, sample_size):
    """Verify self-lookup backfill status in the target org and write a CSV sample of missing rows."""
    from core.orchestrator import Orchestrator
    from core.field_rules import build_field_profile

    cfg = load_config(config)
    source, target = build_mode_connectors(cfg)
    if source is None:
        source = target
        console.print("[dim]Flat-file mode: source org connection is not required.[/dim]")
    if source is None:
        source = target
        console.print("[dim]Flat-file mode: source org connection is not required.[/dim]")

    console.print("[bold]Connecting to Salesforce orgs...[/bold]")
    if source is not None:
        source.connect()
        console.print(f"  [green]âœ“ Source: {source.instance_url}[/green]")
    target.connect()
    console.print(f"  [green]âœ“ Target: {target.instance_url}[/green]")
    if source is None:
        source = target
        console.print("  [dim]Flat-file mode: source connection skipped; using target metadata.[/dim]")

    orchestrator = Orchestrator(source)
    exec_plan = orchestrator.build_plan(cfg)
    selected_pipeline = next((p for p in exec_plan.pipelines if p.pipeline_id == pipeline), None)
    if not selected_pipeline:
        console.print(f"[red]Pipeline '{pipeline}' not found[/red]")
        sys.exit(1)

    selected_object_plan = None
    for level in selected_pipeline.object_levels:
        for obj_plan in level:
            if obj_plan.object_name == object_name:
                selected_object_plan = obj_plan
                break
        if selected_object_plan:
            break

    if not selected_object_plan:
        console.print(f"[red]Object '{object_name}' not found in pipeline '{pipeline}'[/red]")
        sys.exit(1)

    profile = build_field_profile(source, object_name)
    self_lookup_fields = sorted(
        field for field, refs in profile.lookup_fields.items()
        if object_name in refs
    )
    if field_name is None:
        field_name = next((field for field in selected_object_plan.cyclic_fields if field in self_lookup_fields), None)
        if field_name is None and self_lookup_fields:
            field_name = self_lookup_fields[0]

    if not field_name:
        console.print(f"[red]No self-lookup field could be detected for {object_name}[/red]")
        sys.exit(1)

    if field_name not in self_lookup_fields:
        console.print(
            f"[red]Field '{field_name}' is not a self-lookup on {object_name}. "
            f"Detected self-lookups: {', '.join(self_lookup_fields) or 'none'}[/red]"
        )
        sys.exit(1)

    def count_rows(soql: str) -> int:
        try:
            rows = target.query(soql)
        except Exception as exc:
            raise RuntimeError(f"Self-lookup count query failed: {soql}") from exc
        if not rows:
            return 0
        first_row = rows[0]
        if "expr0" in first_row:
            return int(first_row["expr0"] or 0)
        numeric_values = [value for value in first_row.values() if isinstance(value, (int, float))]
        if numeric_values:
            return int(numeric_values[0] or 0)
        raise RuntimeError(f"Could not parse COUNT() result for query: {soql}")

    total_soql = f"SELECT COUNT() FROM {object_name}"
    populated_soql = f"SELECT COUNT() FROM {object_name} WHERE {field_name} != null"
    missing_soql = f"SELECT COUNT() FROM {object_name} WHERE {field_name} = null"

    total_count = count_rows(total_soql)
    populated_count = count_rows(populated_soql)
    missing_count = count_rows(missing_soql)

    select_fields = ["Id", field_name]
    if selected_object_plan.external_id_field and selected_object_plan.external_id_field not in select_fields:
        select_fields.append(selected_object_plan.external_id_field)
    if "Name" in profile.field_meta and "Name" not in select_fields:
        select_fields.append("Name")

    sample_soql = (
        f"SELECT {', '.join(select_fields)} "
        f"FROM {object_name} WHERE {field_name} = null "
        f"ORDER BY CreatedDate DESC LIMIT {int(sample_size)}"
    )
    missing_sample = target.query(sample_soql)

    report_dir = get_or_create_run_dir("exports")
    report_path = report_dir / f"verify_{object_name}_{field_name}_missing.csv"
    with open(report_path, "w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=select_fields, extrasaction="ignore")
        writer.writeheader()
        for row in missing_sample:
            cleaned = {key: value for key, value in row.items() if key in select_fields}
            writer.writerow(cleaned)

    completion_pct = (populated_count / total_count * 100.0) if total_count else 0.0
    console.print(f"\n[bold yellow]Self-lookup verification: {object_name}.{field_name}[/bold yellow]")
    console.print(f"  [green]Populated:[/green] {populated_count:,}")
    console.print(f"  [yellow]Missing:[/yellow] {missing_count:,}")
    console.print(f"  [cyan]Total:[/cyan] {total_count:,}")
    console.print(f"  [cyan]Completion:[/cyan] {completion_pct:.2f}%")
    console.print(f"  [dim]Missing sample CSV -> {report_path}[/dim]")


# ------------------------------------------------------------------ #
#  retry                                                              #
# ------------------------------------------------------------------ #

@cli.command()
@click.option("--config", "-c", default="config/migration.yaml")
@click.option("--pipeline", "-p", required=True, help="Pipeline ID to retry")
@click.option("--dry-run", is_flag=True, default=False)
def retry(config, pipeline, dry_run):
    """Retry a failed pipeline."""
    from core.state_manager import StateManager
    state = StateManager()
    state.reset_pipeline(pipeline)
    console.print(f"[yellow]Pipeline '{pipeline}' reset to PENDING. Re-running...[/yellow]")

    # Delegate to run command
    from click.testing import CliRunner
    runner = CliRunner()
    args = ["--config", config, "--pipeline", pipeline]
    if dry_run:
        args.append("--dry-run")
    runner.invoke(run, args, catch_exceptions=False)


# ------------------------------------------------------------------ #
#  plan                                                               #
# ------------------------------------------------------------------ #

@cli.command()
@click.option("--config", "-c", default="config/migration.yaml")
def plan(config):
    """Show the full execution plan without running anything."""
    from core.orchestrator import Orchestrator
    from core.connector import SFConnector

    cfg = load_config(config)
    source, _ = build_connectors()

    console.print("[bold]Connecting to source org for metadata...[/bold]")
    source.connect()

    orchestrator = Orchestrator(source)
    exec_plan = orchestrator.build_plan(cfg)
    orchestrator.display_plan(exec_plan)


# ------------------------------------------------------------------ #
#  retry-failed                                                      #
# ------------------------------------------------------------------ #

@cli.command("retry-failed")
@click.option("--config", "-c", default="config/migration.yaml", help="Path to migration config YAML")
@click.option("--file", "failed_files", multiple=True, required=True, help="Path to a failed/blocked-record CSV from logs/")
@click.option("--object", "-o", "object_name", default=None, help="Object name (optional if it can be inferred from filename)")
@click.option("--pipeline-id", default=None, help="Pipeline ID for routing retry logs to a pipeline-specific folder")
@click.option("--dry-run", is_flag=True, default=False, help="Validate and preview retry payload without loading")
def retry_failed(config, failed_files, object_name, pipeline_id, dry_run):
    """Retry one or more dead-letter CSVs produced by a previous load."""
    from core.orchestrator import Orchestrator
    from core.loader import Loader
    from core.stager import Stager
    from core.id_mapper import IDMapper

    cfg = load_config(config)
    source, target = build_mode_connectors(cfg)
    if source is None:
        # Flat-file retries need target metadata only; avoid requiring source auth.
        source = target

    console.print("[bold]Connecting to Salesforce orgs...[/bold]")
    source.connect()
    console.print(f"  [green]✓ Source: {source.instance_url}[/green]")
    target.connect()
    console.print(f"  [green]✓ Target: {target.instance_url}[/green]")

    orchestrator = Orchestrator(source)
    exec_plan = orchestrator.build_plan(cfg)
    plan_by_object = {
        obj.object_name: obj
        for pipe in exec_plan.pipelines
        for level in pipe.object_levels
        for obj in level
    }
    pipeline_dir_map = build_pipeline_artifact_dir_map(exec_plan.pipelines)
    retry_stage_dir_names = {"load_failed", "load_blocked", "load_cyclic_failed", "delete_failed"}
    resolved_paths: list[Path] = []
    for raw_path in failed_files:
        failed_path = Path(raw_path)
        if not failed_path.exists():
            console.print(f"[red]Failed CSV not found: {raw_path}[/red]")
            sys.exit(1)
        resolved_paths.append(failed_path)

    source_run_dirs: set[Path] = set()
    for path in resolved_paths:
        if path.parent.name not in retry_stage_dir_names:
            continue
        stage_parent = path.parent.parent
        if stage_parent.name.startswith("run_"):
            source_run_dirs.add(stage_parent)
            continue
        if stage_parent.parent.name.startswith("run_"):
            source_run_dirs.add(stage_parent.parent)
    if len(source_run_dirs) == 1 and pipeline_id:
        source_run_dir = next(iter(source_run_dirs))
        retry_root = ensure_stage_dir(ensure_stage_dir(source_run_dir, pipeline_dir_map.get(pipeline_id, pipeline_id)), "retry_failed")
        log_run_dir = create_sequenced_dir(retry_root)
        console.print(f"[dim]Retry logs -> {log_run_dir}/[/dim]")
    elif len(resolved_paths) == 1 and resolved_paths[0].parent.name in retry_stage_dir_names:
        log_run_dir = resolved_paths[0].parent.parent
        console.print(f"[dim]Retry logs will be written back under -> {log_run_dir}/[/dim]")
    elif pipeline_id:
        log_run_dir = create_sequenced_dir(
            ensure_stage_dir(
                ensure_stage_dir(create_run_dir("logs"), pipeline_dir_map.get(pipeline_id, pipeline_id)),
                "retry_failed",
            )
        )
        console.print(f"[dim]Retry logs -> {log_run_dir}/[/dim]")
    else:
        log_run_dir = create_run_dir("logs")
        console.print(f"[dim]Stage-wise logs -> {log_run_dir}/[/dim]")

    total_processed = 0
    total_success = 0
    total_failed = 0
    total_blocked = 0
    processed_files = 0
    object_totals: dict[str, dict[str, int]] = {}

    stager = Stager()
    id_mapper = IDMapper()
    try:
        loader = Loader(target, stager, id_mapper, log_root=log_run_dir)
        for failed_path in resolved_paths:
            inferred_object = (
                failed_path.name
                .split("_failed_", 1)[0]
                .split("_retry_failed_", 1)[0]
                .split("_blocked_", 1)[0]
                .split("_cyclic_failed_", 1)[0]
                .split("_delete_failed_", 1)[0]
            )
            resolved_object_name = object_name or inferred_object

            obj_plan = plan_by_object.get(resolved_object_name)
            if not obj_plan:
                console.print(
                    f"[red]Object '{resolved_object_name}' not found in the execution plan. "
                    f"Available: {', '.join(sorted(plan_by_object))}[/red]"
                )
                sys.exit(1)

            # Failed Salesforce payloads can contain mixed-looking values (for
            # example numeric prefixes followed by picklist/text values). Keep
            # every CSV field as text so inference cannot corrupt or reject a
            # valid retry payload.
            retry_df = pl.read_csv(str(failed_path), infer_schema_length=0)
            drop_cols = [c for c in ("sf__Id", "sf__Error") if c in retry_df.columns]
            if drop_cols:
                retry_df = retry_df.drop(drop_cols)

            console.print(
                f"[cyan]Retrying {len(retry_df):,} record(s) for {resolved_object_name} from {failed_path.name}...[/cyan]"
            )

            result = loader.load_dataframe(
                resolved_object_name,
                retry_df,
                external_id_field=obj_plan.external_id_field,
                operation=obj_plan.operation,
                dry_run=dry_run,
            )

            total_processed += result.total_processed
            total_success += result.success_count
            total_failed += result.failed_count
            total_blocked += result.blocked_count
            processed_files += 1
            object_summary = object_totals.setdefault(
                resolved_object_name,
                {"files": 0, "processed": 0, "success": 0, "failed": 0, "blocked": 0},
            )
            object_summary["files"] += 1
            object_summary["processed"] += result.total_processed
            object_summary["success"] += result.success_count
            object_summary["failed"] += result.failed_count
            object_summary["blocked"] += result.blocked_count

            status = f"[green]✓ {result.success_count:,} retried successfully[/green]"
            if result.failed_count:
                failed_name = Path(result.failed_file).name if result.failed_file else "retry failure log"
                status += f" [red]| {result.failed_count:,} failed → {failed_name}[/red]"
            if result.blocked_count:
                blocked_name = Path(result.blocked_file).name if result.blocked_file else "retry blocked log"
                status += f" [yellow]| {result.blocked_count:,} blocked → {blocked_name}[/yellow]"
            console.print(status)
    finally:
        stager.close()
        id_mapper.close()

    console.print("\n[bold cyan]Reprocess Summary[/bold cyan]")
    console.print(f"Files: {processed_files:,}")
    console.print(f"Processed: {total_processed:,}")
    console.print(f"[green]Succeeded: {total_success:,}[/green]")
    console.print(f"[red]Failed: {total_failed:,}[/red]")
    console.print(f"[yellow]Blocked: {total_blocked:,}[/yellow]")
    if object_totals:
        console.print("\n[bold cyan]Object-wise Summary[/bold cyan]")
        for object_name in sorted(object_totals):
            summary = object_totals[object_name]
            console.print(
                f"{object_name}: files={summary['files']:,} | "
                f"processed={summary['processed']:,} | "
                f"[green]succeeded={summary['success']:,}[/green] | "
                f"[red]failed={summary['failed']:,}[/red] | "
                f"[yellow]blocked={summary['blocked']:,}[/yellow]"
            )


# ------------------------------------------------------------------ #
#  delete-data                                                       #
# ------------------------------------------------------------------ #

@cli.command("delete-data")
@click.option("--config", "-c", default="config/delete.yaml", help="Path to delete config YAML")
@click.option("--dry-run", is_flag=True, default=False, help="Preview counts and order without changing target data")
def delete_data(config, dry_run):
    """Delete target-org records by object-specific WHERE clauses, using reverse metadata order."""
    from core.field_rules import build_field_profile
    from core.loader import Loader
    from core.stager import Stager
    from core.id_mapper import IDMapper

    delete_cfg = load_delete_config(config)
    _source, target = build_connectors()

    console.print("[bold]Connecting to target Salesforce org...[/bold]")
    target.connect()
    console.print(f"  [green]âœ“ Target: {target.instance_url}[/green]")

    targets = delete_cfg["targets"]
    target_map = {entry["object"]: entry for entry in targets}
    delete_order = _reverse_delete_order(target, targets)
    target_objects = set(target_map)

    console.print("\n[bold]Delete order (reverse dependency):[/bold]")
    for idx, object_name in enumerate(delete_order, start=1):
        console.print(f"  {idx}. {object_name}")

    stager = Stager()
    id_mapper = IDMapper()
    log_run_dir = create_run_dir("logs")
    console.print(f"\n[dim]Stage-wise logs -> {log_run_dir}/[/dim]")
    loader = Loader(target, stager, id_mapper, log_root=log_run_dir)

    total_candidates = 0
    total_deleted = 0
    total_delete_failed = 0
    total_null_updates = 0

    try:
        for object_name in delete_order:
            entry = target_map[object_name]
            profile = build_field_profile(target, object_name)
            lookup_fields = [
                field_name
                for field_name, refs in profile.lookup_fields.items()
                if any(ref in target_objects for ref in refs)
            ]
            delete_df = _query_delete_candidates(target, object_name, entry.get("where", ""), lookup_fields, profile=profile)
            candidate_count = len(delete_df)
            total_candidates += candidate_count

            console.print(f"\n[bold yellow]â–¶ {object_name}[/bold yellow]")
            console.print(f"  [cyan]WHERE:[/cyan] {entry.get('where') or '<none>'}")
            console.print(f"  [cyan]Matched:[/cyan] {candidate_count:,} record(s)")

            if candidate_count == 0:
                continue

            null_payload = _build_internal_lookup_null_payload(profile, delete_df, target_objects)
            if not null_payload.is_empty():
                console.print(
                    f"  [yellow]Pass 1:[/yellow] clearing {len(null_payload.columns) - 1} internal/self lookup field(s) "
                    f"across {len(null_payload):,} record(s)"
                )
                if not dry_run:
                    updated_count = loader.update_records(object_name, null_payload)
                    total_null_updates += updated_count
                    console.print(f"  [green]  âœ“ Cleared lookups on {updated_count:,} record(s)[/green]")

            console.print(f"  [yellow]Pass 2:[/yellow] deleting {candidate_count:,} record(s)")
            if dry_run:
                continue

            deleted_count, failed_df = loader.delete_records(object_name, delete_df.select(["Id"]))
            total_deleted += deleted_count
            total_delete_failed += len(failed_df)
            status = f"  [green]âœ“ Deleted {deleted_count:,} record(s)[/green]"
            if not failed_df.is_empty():
                status += f" [red]| {len(failed_df):,} failed[/red]"
            console.print(status)

        console.print("\n[bold cyan]Delete Summary[/bold cyan]")
        console.print(f"Processed: {total_candidates:,}")
        console.print(f"Candidates matched: {total_candidates:,}")
        if dry_run:
            console.print("Dry run only - no target records were changed.")
        else:
            console.print(f"Lookup clears applied: {total_null_updates:,}")
            console.print(f"[green]Deleted: {total_deleted:,}[/green]")
            console.print(f"[red]Failed: {total_delete_failed:,}[/red]")
    finally:
        stager.close()
        id_mapper.close()


# ------------------------------------------------------------------ #
#  validate                                                           #
# ------------------------------------------------------------------ #

@cli.command()
@click.option("--config", "-c", default="config/migration.yaml")
def validate(config):
    """Validate migration config file structure."""
    cfg = load_config(config)
    errors = []

    # Credentials are in .env — validate they are set
    for prefix in ("SOURCE", "TARGET"):
        for var in ("SF_LOGIN_URL", "SF_CLIENT_ID", "SF_CLIENT_SECRET"):
            if not os.environ.get(f"{prefix}_{var}"):
                errors.append(f"Missing env var: {prefix}_{var} (check your .env file)")

    if "pipelines" not in cfg or not cfg["pipelines"]:
        errors.append("Missing or empty 'pipelines' list")

    for pipe in cfg.get("pipelines", []):
        if "id" not in pipe:
            errors.append(f"Pipeline missing 'id': {pipe}")
        for obj in pipe.get("objects", []):
            if "name" not in obj:
                errors.append(f"Object missing 'name' in pipeline {pipe.get('id')}")
            if "external_id" not in obj:
                errors.append(
                    f"Object '{obj.get('name')}' missing 'external_id' — "
                    f"required for upsert. Add External_Id__c field or specify match key."
                )

    if errors:
        console.print("[red]Config validation FAILED:[/red]")
        for err in errors:
            console.print(f"  [red]✗ {err}[/red]")
        sys.exit(1)
    else:
        console.print("[green]✓ Config is valid[/green]")


# ------------------------------------------------------------------ #
#  validate-metadata                                                  #
# ------------------------------------------------------------------ #

@cli.command("validate-metadata")
@click.option("--config", "-c", default="config/migration.yaml", help="Path to migration config YAML")
@click.option("--pipeline", "-p", default=None, help="Validate only this pipeline ID")
@click.option("--with-data", is_flag=True, default=False, help="Also check staged data (picklists + nulls) — requires prior extract")
def validate_metadata(config, pipeline, with_data):
    """Validate config fields and external IDs against source + target org metadata."""
    from core.orchestrator import Orchestrator
    from core.stager import Stager
    from core.validator import MetadataValidator

    cfg = load_config(config)
    source, target = build_mode_connectors(cfg)
    if source is None:
        source = target
        console.print("[dim]Flat-file mode: source org connection is not required.[/dim]")

    console.print("[bold]Connecting to Salesforce orgs...[/bold]")
    source.connect()
    console.print(f"  [green]✓ Source: {source.instance_url}[/green]")
    target.connect()
    console.print(f"  [green]✓ Target: {target.instance_url}[/green]")

    stager = Stager() if with_data else None

    orchestrator = Orchestrator(source)
    exec_plan = orchestrator.build_plan(cfg)

    pipelines_to_check = exec_plan.pipelines
    if pipeline:
        pipelines_to_check = [p for p in exec_plan.pipelines if p.pipeline_id == pipeline]
        if not pipelines_to_check:
            console.print(f"[red]Pipeline '{pipeline}' not found in config[/red]")
            sys.exit(1)

    validator = MetadataValidator(source, target, stager)

    total_errors = 0
    total_warnings = 0

    for pipe in pipelines_to_check:
        console.print(f"\n[bold yellow]▶ Pipeline: {pipe.name}[/bold yellow]")

        # Flatten all object configs from this pipeline
        object_configs = []
        pcfg = next(p for p in cfg["pipelines"] if p["id"] == pipe.pipeline_id)
        object_configs = pcfg.get("objects", [])

        results = validator.validate_pipeline(object_configs)

        for result in results:
            errors   = [i for i in result.issues if i.severity == "error"]
            warnings = [i for i in result.issues if i.severity == "warning"]

            if result.passed and not warnings:
                console.print(f"  [green]✓ {result.object_name} — all checks passed[/green]")
            elif result.passed and warnings:
                console.print(f"  [yellow]⚠ {result.object_name} — passed with warnings[/yellow]")
            else:
                console.print(f"  [red]✗ {result.object_name} — FAILED[/red]")

            for issue in errors:
                console.print(f"      [red]✗ {issue.field_name}: {issue.issue}[/red]")
                total_errors += 1
            for issue in warnings:
                console.print(f"      [yellow]⚠ {issue.field_name}: {issue.issue}[/yellow]")
                total_warnings += 1

    if stager:
        stager.close()

    console.print()
    if total_errors == 0 and total_warnings == 0:
        console.print("[bold green]✓ All validations passed — safe to run migration.[/bold green]")
    elif total_errors == 0:
        console.print(f"[bold yellow]⚠ {total_warnings} warning(s) — review before running.[/bold yellow]")
    else:
        console.print(f"[bold red]✗ {total_errors} error(s), {total_warnings} warning(s) — fix before running.[/bold red]")
        sys.exit(1)


@cli.command("build-object-hierarchy")
@click.option("--org", type=click.Choice(["source", "target"]), default="source", show_default=True, help="Which Salesforce org metadata to inspect")
@click.option("--profile", default="default", show_default=True, help="Env profile to use for SOURCE/TARGET connector")
@click.option("--objects-file", default="config/object_hierarchy_scope.txt", show_default=True, help="Text file containing in-scope object API names, one per line")
@click.option("--out", "output_path", default="ui/object_hierarchy.xlsx", show_default=True, help="Workbook output path")
def build_object_hierarchy(org, profile, objects_file, output_path):
    """Build object hierarchy workbook for the relationship viewer."""
    from core.connector import connector_from_env

    prefix = org.upper()
    object_names = read_object_list_file(objects_file)
    if not object_names:
        console.print(f"[red]No objects found in {objects_file}[/red]")
        sys.exit(1)

    connector = connector_from_env(prefix, None if profile == "default" else profile)

    console.print(f"[bold]Building object hierarchy from {org} org...[/bold]")
    console.print(f"  [dim]Object list: {objects_file} ({len(object_names)} object(s))[/dim]")
    connector.connect()
    console.print(f"  [green]OK Connected: {connector.instance_url}[/green]")

    result = generate_object_hierarchy_workbook(
        connector,
        object_names,
        Path(output_path),
    )

    console.print(f"  [green]OK Workbook written: {result['path']}[/green]")
    console.print(f"  [green]OK Mappings: {result['mapping_count']}[/green]")
    console.print(f"  [green]OK Cycle groups: {result['cycle_group_count']}[/green]")
    console.print(f"  [green]OK Cycle objects: {result['cycle_object_count']}[/green]")
    if result.get("skipped_object_count"):
        console.print(
            f"  [yellow]Skipped unavailable objects: "
            f"{result['skipped_object_count']}[/yellow]"
        )
        for item in result.get("skipped_objects") or []:
            console.print(
                f"    [yellow]- {item['object']}: {item['reason']}[/yellow]"
            )


if __name__ == "__main__":
    cli()
