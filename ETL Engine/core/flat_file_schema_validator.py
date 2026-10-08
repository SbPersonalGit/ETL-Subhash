"""Validate mapped flat-file values against Salesforce describe metadata."""

from __future__ import annotations

import csv
import re
import sqlite3
import tempfile
import time as time_module
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from core.field_rules import MIGRATABLE_AUDIT_FIELDS, SYSTEM_FIELD_BLACKLIST
from core.flat_file import open_csv


_TRUE_FALSE = {"true", "false"}
_SF_ID = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")
_SF_ID_15 = re.compile(r"^[A-Za-z0-9]{15}$")
_SF_ID_18 = re.compile(r"^[A-Za-z0-9]{18}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?[0-9][0-9 ().-]{5,38}$")
_URL = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)
_SYSTEM_MANAGED_FIELDS = {name.casefold() for name in SYSTEM_FIELD_BLACKLIST if name != "Id"}
_MIGRATABLE_AUDIT_FIELDS = {name.casefold() for name in MIGRATABLE_AUDIT_FIELDS}


def is_system_managed_field(field_name: str | None) -> bool:
    """True for standard audit/system columns that normal DML never loads."""
    return str(field_name or "").strip().casefold() in _SYSTEM_MANAGED_FIELDS


def is_migratable_audit_field(field_name: str | None) -> bool:
    """True for audit fields Salesforce may accept during record creation."""
    return str(field_name or "").strip().casefold() in _MIGRATABLE_AUDIT_FIELDS


def is_salesforce_derived_field(field: dict | None) -> bool:
    """True for fields whose value Salesforce computes or generates."""
    return bool(field) and bool(
        field.get("calculated")
        or field.get("autoNumber")
        or field.get("type") in {"address", "location"}
    )


def validate_migration_field_access(
    object_name: str,
    described_fields: list[dict],
    mappings: list[dict] | None = None,
) -> list[dict]:
    """Return target-field and dependent-controller access problems."""
    visible = {
        str(field.get("name") or "").casefold(): field
        for field in described_fields
        if field.get("name")
    }
    issues: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(code: str, field_name: str, message: str, controller: str = "") -> None:
        key = (code, field_name.casefold())
        if key in seen:
            return
        seen.add(key)
        issues.append({
            "code": code,
            "object": object_name,
            "field": field_name,
            "controller": controller,
            "severity": "error",
            "message": message,
        })

    for mapping in mappings or []:
        if mapping.get("ignored"):
            continue
        source_name = str(mapping.get("source") or "").strip()
        if is_system_managed_field(source_name):
            continue
        if not mapping.get("target"):
            if mapping.get("status") == "read_only":
                source = str(mapping.get("source") or "").strip()
                add(
                    "target_field_read_only",
                    source,
                    f"Source column {source} matches {object_name}.{source}, but the "
                    "target field is read-only for the migration user. Remove the column "
                    "or mark it ignored; it cannot be loaded by insert or upsert.",
                )
            continue
        target = str(mapping["target"]).strip()
        if is_system_managed_field(target):
            continue
        if "." in target or target.casefold() == "id":
            continue
        field = visible.get(target.casefold())
        if not field:
            add(
                "target_field_not_visible",
                target,
                f"{object_name}.{target} is not visible in target Describe metadata. "
                "The field may not exist or the migration user may lack field access.",
            )
        elif is_salesforce_derived_field(field):
            continue
        elif not (field.get("createable") or field.get("updateable")) and not is_migratable_audit_field(target):
            add(
                "target_field_not_writable",
                target,
                f"Target field {object_name}.{target} is read-only for the migration user. "
                "Remove the mapping or mark the source column ignored; it cannot be loaded "
                "by insert or upsert.",
            )

    for field in described_fields:
        field_name = str(field.get("name") or "").strip()
        is_required = (
            field_name
            and field.get("createable")
            and not field.get("nillable", True)
            and not field.get("defaultedOnCreate", False)
            and not field.get("calculated", False)
            and not field.get("autoNumber", False)
        )
        controller = str(field.get("controllerName") or "").strip()
        if not is_required or not field.get("dependentPicklist") or not controller:
            continue
        controller_field = visible.get(controller.casefold())
        if not controller_field:
            add(
                "required_controller_not_visible",
                field_name,
                f"Cannot migrate required field {object_name}.{field_name}: its controlling "
                f"field {object_name}.{controller} is not visible to the migration user. "
                f"Grant field-level read/create access to {controller}, then refresh metadata.",
                controller,
            )
        elif not controller_field.get("createable"):
            add(
                "required_controller_not_createable",
                field_name,
                f"Cannot migrate required field {object_name}.{field_name}: migration user "
                f"cannot create {object_name}.{controller}. Grant create access and refresh metadata.",
                controller,
            )
    return issues


def _decimal_error(value: str, field: dict) -> str | None:
    try:
        number = Decimal(value.replace(",", "").strip())
    except InvalidOperation:
        return "expected a number"
    if not number.is_finite():
        return "expected a finite number"

    precision = field.get("precision")
    scale = field.get("scale")
    if precision is None:
        return None
    precision = int(precision)
    scale = int(scale or 0)
    normalized = format(abs(number), "f")
    integral, _, fractional = normalized.partition(".")
    integral_digits = len(integral.lstrip("0"))
    fractional_digits = len(fractional.rstrip("0"))
    allowed_integral = max(0, precision - scale)
    if integral_digits > allowed_integral or fractional_digits > scale:
        field_type = str(field.get("type") or "number")
        maximum = (
            ("9" * allowed_integral if allowed_integral else "0")
            + (("." + ("9" * scale)) if scale else "")
        )
        return (
            f"{field_type}({precision},{scale}) allows at most {maximum}; "
            f"received {value}"
        )
    return None


def _temporal_error(value: str, field_type: str) -> str | None:
    try:
        if field_type == "date":
            date.fromisoformat(value)
        elif field_type == "time":
            time.fromisoformat(value.replace("Z", "+00:00"))
        else:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        expected = {
            "date": "YYYY-MM-DD",
            "time": "HH:MM:SS.sssZ",
            "datetime": "ISO-8601 datetime",
        }[field_type]
        return f"expected {expected}"
    return None


def validate_value(value: str, field: dict, *, enabled_checks: set[str] | None = None, salesforce_id_length: str = "15_or_18") -> str | None:
    """Return a human-readable constraint violation, or ``None``."""
    if value is None or not str(value).strip():
        return None
    value = str(value).strip()
    field_type = str(field.get("type") or "").lower()
    field_name = str(field.get("name") or "")
    value_policy = str(field.get("_value_policy") or "").strip().lower()
    enabled = lambda name: enabled_checks is None or name in enabled_checks

    migration_allowed = field.get("_migrationAllowedValues")
    if migration_allowed is not None and value not in set(migration_allowed):
        reason = str(field.get("_migrationAllowedReason") or "migration preflight policy")
        return f"value is not allowed by {reason}"

    if field_name == "IsConverted" and value.casefold() == "true":
        return "system Lead conversion state cannot be set during normal insertion"

    if field_type in {"double", "currency", "percent"}:
        if not enabled("numeric_precision"):
            return None
        decimal_error = _decimal_error(value, field)
        if decimal_error:
            return decimal_error
        if field_name == "Probability":
            probability = Decimal(value.replace(",", "").strip())
            if probability < 0 or probability > 100:
                return "value must be between 0 and 100"
        if field_name.casefold().endswith("latitude"):
            latitude = Decimal(value.replace(",", "").strip())
            if latitude < -90 or latitude > 90:
                return "latitude must be between -90 and 90"
        if field_name.casefold().endswith("longitude"):
            longitude = Decimal(value.replace(",", "").strip())
            if longitude < -180 or longitude > 180:
                return "longitude must be between -180 and 180"
        return None
    if field_type in {"int", "integer", "long"}:
        if not enabled("numeric_precision"):
            return None
        try:
            number = int(value)
        except ValueError:
            return "expected a whole number"
        if field_type in {"int", "integer"} and not -(2**31) <= number < 2**31:
            return "integer is outside the 32-bit range"
    elif field_type == "boolean" and enabled("scalar_formats") and value.lower() not in _TRUE_FALSE:
        return "expected true or false"
    elif field_type in {"date", "datetime", "time"}:
        if field_type == "date" and value_policy == "date_bypass":
            return None
        if not enabled("date_formats"):
            return None
        return _temporal_error(value, field_type)
    elif field_type in {"reference", "id"} and enabled("salesforce_id_format"):
        id_patterns = {"15": _SF_ID_15, "18": _SF_ID_18, "15_or_18": _SF_ID}
        id_pattern = id_patterns.get(str(salesforce_id_length), _SF_ID)
        if not id_pattern.fullmatch(value):
            expected = "15- or 18-character" if salesforce_id_length == "15_or_18" else f"{salesforce_id_length}-character"
            article = "an" if salesforce_id_length == "18" else "a"
            return f"expected {article} {expected} Salesforce ID"
    elif field_type == "email" and enabled("contact_formats") and not _EMAIL.fullmatch(value):
        return "expected an email address"
    elif field_type == "phone" and enabled("contact_formats") and not _PHONE.fullmatch(value):
        return "expected a phone number containing digits and common phone punctuation"
    elif field_type == "url" and enabled("contact_formats"):
        if value_policy in {"url_bypass", "url_salesforce"}:
            pass
        elif not _URL.fullmatch(value):
            return "expected an absolute HTTP or HTTPS URL"

    length = field.get("length")
    byte_length = field.get("byteLength")
    if enabled("text_limits") and byte_length and len(value.encode("utf-8")) > int(byte_length):
        return f"UTF-8 byte length {len(value.encode('utf-8'))} exceeds maximum {byte_length}"
    if enabled("text_limits") and length and len(value) > int(length):
        return f"length {len(value)} exceeds maximum {length}"

    if enabled("restricted_picklists") and field.get("restrictedPicklist"):
        allowed = {
            str(item.get("value"))
            for item in field.get("picklistValues") or []
            if item.get("active")
        }
        values = value.split(";") if field_type == "multipicklist" else [value]
        invalid = [item for item in values if item not in allowed]
        if allowed and invalid:
            return f"value is not an active target picklist option: {', '.join(invalid)}"
    return None


def validate_strict_api_names(file_config: dict) -> list[str]:
    """Enforce unambiguous object and field API names for flat-file packages."""
    filename = str(file_config.get("file") or file_config.get("stored_name") or "").strip()
    object_name = str(file_config.get("object") or "").strip()
    errors = []
    if filename and object_name and not _file_path_matches_object(filename, object_name):
        errors.append(
            f"{filename}: file path must identify object API name {object_name} "
            f"via filename stem (Account.csv) or parent folder (Account/part1.csv)."
        )
    for mapping in file_config.get("mappings") or []:
        if mapping.get("ignored"):
            continue
        source = str(mapping.get("source") or "").strip()
        target = str(mapping.get("target") or "").strip()
        is_id_to_external_id = (
            source.casefold() == "id"
            and bool(external_id := str(file_config.get("external_id") or "").strip())
            and target.casefold() == external_id.casefold()
        )
        is_record_type_developer_name = (
            source.casefold() in {
                "recordtype.developername",
                "recordtypedevelopername",
                "recordtype_developername",
            }
            and target.casefold() == "recordtypeid"
        )
        if (
            source
            and target
            and source.casefold() != target.casefold()
            and not is_id_to_external_id
            and not is_record_type_developer_name
        ):
            errors.append(
                f"{filename}: header '{source}' must exactly equal field API name '{target}'."
            )
    return errors


def _file_path_matches_object(filename: str, object_name: str) -> bool:
    """True when the CSV path identifies the object by stem or parent folder."""
    path = Path(str(filename or ""))
    object_key = str(object_name or "").strip().casefold()
    if not object_key:
        return False
    if path.stem.casefold() == object_key:
        return True
    # Drop-folder split files: Account/CMF Approved_1.csv
    return any(str(part).strip().casefold() == object_key for part in path.parts[:-1])


def _record_type_source_column(file_config: dict) -> str:
    """Return the CSV header mapped to target RecordTypeId, if configured."""
    for mapping in file_config.get("mappings") or []:
        if mapping.get("ignored"):
            continue
        if str(mapping.get("target") or "").strip().casefold() == "recordtypeid":
            return str(mapping.get("source") or "").strip()
    return ""


def validate_object_and_record_type_access(
    object_name: str,
    object_describe: dict,
    file_config: dict,
) -> list[str]:
    """Validate object CRUD and mapped Record Type availability."""
    errors = []
    operation = str(file_config.get("operation") or "upsert").lower()
    if operation in {"insert", "upsert"} and not object_describe.get("createable"):
        errors.append(f"Migration user cannot create {object_name} records.")
    if operation in {"update", "upsert"} and not object_describe.get("updateable"):
        errors.append(f"Migration user cannot update {object_name} records.")
    available_ids = {
        str(info.get("recordTypeId") or "").strip()
        for info in object_describe.get("recordTypeInfos") or []
        if info.get("available", False)
    }
    mapped_ids = {
        str(value).strip()
        for value in (file_config.get("record_type_mappings") or {}).values()
        if str(value).strip()
    }
    unavailable = sorted(mapped_ids - available_ids)
    if unavailable:
        errors.append(
            f"Mapped Record Types are unavailable to the migration user: {', '.join(unavailable)}."
        )
    return errors


def validate_csv_against_schema(
    path: str | Path,
    file_config: dict,
    described_fields: list[dict],
    *,
    detail_limit: int = 500,
) -> dict:
    """Validate mapped CSV cells and return a compact, serializable report."""
    metadata = {
        str(field.get("name") or "").casefold(): field
        for field in described_fields
        if field.get("name")
    }
    mapped = {}
    for mapping in file_config.get("mappings") or []:
        if mapping.get("ignored") or not mapping.get("target"):
            continue
        target = str(mapping["target"]).strip()
        # Relationship external-ID values are intentionally not Salesforce IDs.
        if "." in target:
            continue
        field = metadata.get(target.casefold())
        if field:
            policy_field = dict(field)
            policy_field["_value_policy"] = str(mapping.get("value_policy") or "")
            mapped[str(mapping.get("source") or "")] = (target, policy_field)

    issues: list[dict] = []
    issue_count = 0
    field_counts: dict[str, int] = {}
    with open_csv(path) as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            for source, (target, field) in mapped.items():
                message = validate_value(row.get(source, ""), field)
                if not message:
                    continue
                issue_count += 1
                field_counts[target] = field_counts.get(target, 0) + 1
                if len(issues) < detail_limit:
                    raw_value = str(row.get(source, ""))
                    issues.append({
                        "row": row_number,
                        "source": source,
                        "target": target,
                        "type": field.get("type"),
                        "value": raw_value[:100],
                        "message": message,
                    })
    return {
        "issue_count": issue_count,
        "issues": issues,
        "suppressed_count": max(0, issue_count - len(issues)),
        "field_counts": field_counts,
    }


def _safe_normalize(value: str, field: dict) -> str:
    """Apply only deterministic, non-lossy normalizations."""
    value = str(value or "").strip()
    field_type = str(field.get("type") or "").lower()
    value_policy = str(field.get("_value_policy") or "").strip().lower()
    if not value:
        return value
    if field_type == "boolean":
        folded = value.casefold()
        if folded in {"1", "yes", "y"}:
            return "true"
        if folded in {"0", "no", "n"}:
            return "false"
        if folded in _TRUE_FALSE:
            return folded
    if field_type in {"double", "currency", "percent", "int", "integer", "long"}:
        # Commas are formatting characters; removing them does not change value.
        return value.replace(",", "")
    if field_type == "date" and value_policy in {"date_mdy4", "date_mdy2"}:
        date_format = "%m/%d/%Y" if value_policy == "date_mdy4" else "%m/%d/%y"
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            return value
    if field_type == "url" and value_policy == "url_prepend_https":
        if not re.match(r"^https?://", value, re.IGNORECASE):
            return f"https://{value}"
    return value


_MULTISELECT_PICKLIST_TYPES = {"multipicklist", "multiselectpicklist"}


def _selected_picklist_values(value: str, field_type: str = "") -> list[str]:
    """Split multi-select cells only. Single picklist labels may contain ';'."""
    if not isinstance(value, str):
        return [value]
    if str(field_type or "").lower() in _MULTISELECT_PICKLIST_TYPES:
        return [item for item in value.split(";") if item]
    return [value] if value else []


def _record_type_picklist_error(
    value: str,
    field_name: str,
    record_type_id: str,
    record_type_fields: dict[str, object],
    *,
    controller_value: str = "",
    field_type: str = "",
) -> str | None:
    config = record_type_fields.get(field_name)
    if isinstance(config, dict):
        allowed = config.get("values")
    else:
        allowed = config
    values = _selected_picklist_values(value, field_type)
    invalid = [item for item in values if item and allowed is not None and item not in allowed]
    if invalid:
        return f"value is not available for target Record Type {record_type_id}"
    if value and isinstance(config, dict) and config.get("controller_values"):
        controller_index = config["controller_values"].get(controller_value)
        invalid_dependents = [
            item
            for item in values
            if item
            and (
                controller_index is None
                or controller_index not in config.get("valid_for", {}).get(item, set())
            )
        ]
        if invalid_dependents:
            return (
                f"value {', '.join(invalid_dependents)} is not valid for controlling value "
                f"'{controller_value or '(blank)'}' on target Record Type {record_type_id}"
            )
    return None


def _enforce_record_type_picklist(field: dict) -> bool:
    """Whether UI API Record-Type values are a DML allow-list for this field."""
    return bool(field.get("restrictedPicklist"))


def _dependent_picklist_controller_error(
    value: str,
    field: dict,
    target_to_source: dict[str, str],
    row: dict,
) -> str | None:
    """Reject a populated dependent picklist when its controller is unavailable."""
    if not value or not field.get("dependentPicklist"):
        return None
    controller = str(field.get("controllerName") or "").strip()
    if not controller:
        return None
    source = target_to_source.get(controller)
    if not source:
        return f"dependent picklist requires mapped controlling field {controller}"
    if not str(row.get(source) or "").strip():
        return f"dependent picklist requires a value in controlling field {controller}"
    return None


def _required_field_errors(
    row: dict,
    required_fields: dict[str, dict],
    target_to_source: dict[str, str],
) -> list[tuple[str, str]]:
    errors = []
    for target, _field in required_fields.items():
        source = target_to_source.get(target)
        if not source:
            errors.append((target, "required target field is not mapped"))
        elif not str(row.get(source) or "").strip():
            errors.append((target, "required value is blank"))
    return errors


def clean_csv_against_schema(
    source_path: str | Path,
    clean_path: str | Path,
    rejected_path: str | Path,
    file_config: dict,
    described_fields: list[dict],
    record_type_picklists: dict[str, dict[str, set[str]]] | None = None,
    default_record_type_id: str = "",
    reference_key_prefixes: dict[str, set[str]] | None = None,
    valid_record_type_ids: set[str] | None = None,
    progress_callback=None,
    progress_every_rows: int = 5000,
    row_limit: int | None = None,
    enabled_checks: set[str] | None = None,
) -> dict:
    """Write schema-valid rows to clean CSV and invalid rows to rejected CSV.

    ``progress_callback`` receives dicts with ``phase``, ``rows_processed``,
    optional ``total_rows``, and during schema cleaning ``clean_count`` /
    ``rejected_count``.
    """
    salesforce_id_length = str(file_config.get("_salesforce_id_length") or "15_or_18")

    def emit(payload: dict) -> None:
        if not progress_callback:
            return
        try:
            progress_callback(payload)
        except Exception:
            # Progress must never fail the cleaning job.
            pass
    enabled = lambda name: enabled_checks is None or name in enabled_checks
    metadata = {
        str(field.get("name") or "").casefold(): field
        for field in described_fields
        if field.get("name")
    }
    mapped = {}
    target_to_source = {}
    for mapping in file_config.get("mappings") or []:
        if mapping.get("ignored") or not mapping.get("target"):
            continue
        target = str(mapping["target"]).strip()
        if "." in target:
            continue
        field = metadata.get(target.casefold())
        if field:
            policy_field = dict(field)
            policy_field["_value_policy"] = str(mapping.get("value_policy") or "")
            mapped[str(mapping.get("source") or "")] = (target, policy_field)
            target_to_source[target] = str(mapping.get("source") or "")
    required_fields = {
        str(field["name"]): field
        for field in described_fields
        if field.get("name")
        and field.get("createable")
        and not field.get("nillable", True)
        and not field.get("defaultedOnCreate", False)
        and not field.get("calculated", False)
        and not field.get("autoNumber", False)
    }

    key_sources = {"Id"}
    external_id = str(file_config.get("external_id") or "").strip()
    operation = str(file_config.get("operation") or "upsert").strip().lower()
    external_id_sources = set()
    for mapping in file_config.get("mappings") or []:
        if mapping.get("ignored"):
            continue
        target_key = str(mapping.get("target") or "").strip()
        if external_id and target_key.casefold() == external_id.casefold():
            source_key = str(mapping.get("source") or "").strip()
            if source_key:
                key_sources.add(source_key)
                external_id_sources.add(source_key)
        elif not external_id and target_key.casefold() == "id":
            source_key = str(mapping.get("source") or "").strip()
            if source_key:
                external_id_sources.add(source_key)
    # Keep cardinality-heavy duplicate tracking on disk.  A Counter retains every
    # distinct migration key and can exhaust memory for multi-million-row files.
    duplicate_db = tempfile.NamedTemporaryFile(prefix="sf_etl_keys_", suffix=".sqlite3", delete=False)
    duplicate_db.close()
    key_connection = sqlite3.connect(duplicate_db.name)
    key_connection.execute(
        "CREATE TABLE key_counts (source TEXT NOT NULL, value TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(source, value))"
    )
    total_rows = 0
    emit({"phase": "duplicate_scan", "rows_processed": 0, "total_rows": None})
    with open_csv(source_path) as count_handle:
        pending = []
        for count_row in csv.DictReader(count_handle):
            if row_limit is not None and total_rows >= row_limit:
                break
            total_rows += 1
            for key_source in key_sources if enabled("duplicate_keys") else ():
                key_value = str(count_row.get(key_source) or "").strip()
                if key_source and key_value:
                    pending.append((key_source, key_value))
            if len(pending) >= 10_000:
                key_connection.executemany(
                    "INSERT INTO key_counts(source, value, count) VALUES (?, ?, 1) "
                    "ON CONFLICT(source, value) DO UPDATE SET count = count + 1",
                    pending,
                )
                key_connection.commit()
                pending.clear()
            if progress_every_rows > 0 and total_rows % progress_every_rows == 0:
                emit({
                    "phase": "duplicate_scan",
                    "rows_processed": total_rows,
                    "total_rows": None,
                })
        if pending:
            key_connection.executemany(
                "INSERT INTO key_counts(source, value, count) VALUES (?, ?, 1) "
                "ON CONFLICT(source, value) DO UPDATE SET count = count + 1",
                pending,
            )
            key_connection.commit()
    emit({
        "phase": "duplicate_scan",
        "rows_processed": total_rows,
        "total_rows": total_rows,
    })

    # Keep only values that are genuinely duplicated in memory.  The previous
    # implementation issued a SQLite SELECT for every mapped cell on every row,
    # even though duplicate tracking applies only to Id/external-ID columns.
    # Duplicate values are normally a tiny subset of the input, so this removes
    # millions of database round trips without retaining all unique keys.
    duplicate_keys = {
        (str(source), str(value))
        for source, value in key_connection.execute(
            "SELECT source, value FROM key_counts WHERE count > 1"
        )
    }

    def is_duplicate(source_name: str, value: str) -> bool:
        if not value or source_name not in key_sources:
            return False
        return (source_name, value) in duplicate_keys

    record_type_source = _record_type_source_column(file_config)
    record_type_mappings = {
        str(source).strip(): str(target).strip()
        for source, target in (file_config.get("record_type_mappings") or {}).items()
        if str(source).strip() and str(target).strip()
    }
    clean_count = rejected_count = changed_count = 0
    field_counts: dict[str, int] = {}
    examples: list[dict] = []
    with open_csv(source_path) as source:
        reader = csv.DictReader(source)
        headers = list(reader.fieldnames or [])
        # Rejected artifacts are user-facing migration reports.  Keep only
        # active mapped values and label them with canonical target API names;
        # the load-ready clean artifact intentionally retains source headers so
        # the staging adapter can apply the saved source-to-target mappings.
        rejected_headers = [target for target, _field in mapped.values()]
        with Path(clean_path).open("w", encoding="utf-8-sig", newline="") as clean_handle, \
                Path(rejected_path).open("w", encoding="utf-8-sig", newline="") as rejected_handle:
            clean_writer = csv.DictWriter(clean_handle, fieldnames=headers)
            rejected_writer = csv.DictWriter(
                rejected_handle,
                fieldnames=[*rejected_headers, "__source_row", "__validation_errors"],
            )
            clean_writer.writeheader()
            rejected_writer.writeheader()
            emit({
                "phase": "schema_clean",
                "rows_processed": 0,
                "total_rows": total_rows,
                "clean_count": 0,
                "rejected_count": 0,
            })
            for row_number, row in enumerate(reader, start=2):
                if row_limit is not None and row_number - 1 > row_limit:
                    break
                row_errors = []
                changed = False
                if None in row:
                    row_errors.append("CSV: row has more values than the header")
                if operation in {"update", "upsert"} and external_id_sources:
                    for key_source in external_id_sources:
                        key_value = str(row.get(key_source) or "").strip()
                        if not key_value:
                            row_errors.append(f"{key_source}: migration key is blank for {operation}")
                        elif re.fullmatch(r"[+-]?\d+(?:\.\d+)?[eE][+-]?\d+", key_value):
                            row_errors.append(f"{key_source}: migration key appears to be scientific notation")
                source_record_type = str(row.get(record_type_source) or "").strip()
                target_record_type = record_type_mappings.get(source_record_type, "")
                if not target_record_type:
                    target_record_type = (
                        str(default_record_type_id or "").strip()
                        if not source_record_type
                        else ""
                    )
                record_type_error = ""
                if source_record_type and not target_record_type:
                    record_type_error = (
                        f"target Record Type DeveloperName '{source_record_type}' "
                        "does not exist or is unavailable to the migration user"
                    )
                elif (
                    target_record_type
                    and valid_record_type_ids is not None
                    and target_record_type not in valid_record_type_ids
                ):
                    record_type_error = (
                        f"resolved target Record Type {target_record_type} is unavailable "
                        "to the migration user"
                    )
                if record_type_error:
                    field_counts["RecordTypeId"] = field_counts.get("RecordTypeId", 0) + 1
                    row_errors.append(f"RecordTypeId: {record_type_error}")
                    if len(examples) < 100:
                        examples.append({
                            "row": row_number,
                            "source": record_type_source,
                            "target": "RecordTypeId",
                            "value": source_record_type[:100],
                            "message": record_type_error,
                        })
                record_type_fields = (record_type_picklists or {}).get(
                    target_record_type, {}
                )
                for target, message in (_required_field_errors(
                    row, required_fields, target_to_source
                ) if enabled("required_fields") else []):
                    field_counts[target] = field_counts.get(target, 0) + 1
                    row_errors.append(f"{target}: {message}")
                    if len(examples) < 100:
                        examples.append({
                            "row": row_number,
                            "source": target_to_source.get(target, ""),
                            "target": target,
                            "value": str(
                                row.get(target_to_source.get(target, ""), "")
                            )[:100],
                            "message": message,
                        })
                for source_name, (target, field) in mapped.items():
                    # Record Type source values may be DeveloperNames. Their resolved
                    # target IDs were validated above, so do not apply generic SF-ID
                    # validation to the original source text.
                    if target.casefold() == "recordtypeid":
                        continue
                    original = str(row.get(source_name) or "")
                    normalized = _safe_normalize(original, field)
                    if normalized != original:
                        row[source_name] = normalized
                        changed = True
                    message = None
                    if (
                        normalized
                        and str(field.get("type") or "").lower() in {"reference", "id"}
                        and re.fullmatch(r"[+-]?\d+(?:\.\d+)?[eE][+-]?\d+", normalized)
                    ):
                        message = "Salesforce identifier appears to be scientific notation"
                    if not message:
                        message = validate_value(normalized, field, enabled_checks=enabled_checks, salesforce_id_length=salesforce_id_length)
                    if not message and is_duplicate(source_name, normalized):
                        message = f"duplicate in-file key value for {source_name}"
                    allowed_prefixes = (reference_key_prefixes or {}).get(target, set())
                    if (
                        not message
                        and normalized
                        and str(field.get("type") or "").lower() in {"reference", "id"}
                        and allowed_prefixes
                        and normalized[:3] not in allowed_prefixes
                    ):
                        message = (
                            "Salesforce ID prefix does not match referenced object "
                            f"({', '.join(sorted(allowed_prefixes))})"
                        )
                    if not message and enabled("dependent_picklists"):
                        message = _dependent_picklist_controller_error(
                            normalized, field, target_to_source, row
                        )
                    # Salesforce accepts values outside the configured value set
                    # for unrestricted picklists. UI API returns the values shown
                    # for a Record Type, but that list is not a DML allow-list
                    # unless restrictedPicklist is true.
                    if (
                        not message
                        and enabled("record_type_picklists")
                        and _enforce_record_type_picklist(field)
                    ):
                        controller_name = str(field.get("controllerName") or "").strip()
                        controller_source = target_to_source.get(controller_name, "")
                        controller_field = metadata.get(controller_name.casefold(), {})
                        controller_value = _safe_normalize(
                            str(row.get(controller_source) or ""), controller_field
                        )
                        message = _record_type_picklist_error(
                            normalized,
                            target,
                            target_record_type,
                            record_type_fields,
                            controller_value=controller_value,
                            field_type=str(field.get("type") or ""),
                        )
                    if message:
                        field_counts[target] = field_counts.get(target, 0) + 1
                        row_errors.append(f"{target}: {message}")
                        if len(examples) < 100:
                            examples.append({
                                "row": row_number,
                                "source": source_name,
                                "target": target,
                                "value": original[:100],
                                "message": message,
                            })
                if row_errors:
                    rejected_count += 1
                    rejected_writer.writerow({
                        **{
                            target: row.get(source_name, "")
                            for source_name, (target, _field) in mapped.items()
                        },
                        "__source_row": row_number,
                        "__validation_errors": " | ".join(row_errors),
                    })
                else:
                    clean_count += 1
                    changed_count += int(changed)
                    clean_writer.writerow(row)
                rows_processed = row_number - 1
                if progress_every_rows > 0 and rows_processed % progress_every_rows == 0:
                    emit({
                        "phase": "schema_clean",
                        "rows_processed": rows_processed,
                        "total_rows": total_rows,
                        "clean_count": clean_count,
                        "rejected_count": rejected_count,
                    })
    emit({
        "phase": "schema_clean",
        "rows_processed": total_rows,
        "total_rows": total_rows,
        "clean_count": clean_count,
        "rejected_count": rejected_count,
    })
    try:
        key_connection.commit()
    except Exception:
        pass
    try:
        key_connection.close()
    except Exception:
        pass
    # Windows/OneDrive can keep a brief lock on the temp SQLite file after close.
    for attempt in range(5):
        try:
            Path(duplicate_db.name).unlink(missing_ok=True)
            break
        except PermissionError:
            if attempt == 4:
                break
            time_module.sleep(0.2 * (attempt + 1))
    return {
        "clean_count": clean_count,
        "rejected_count": rejected_count,
        "normalized_count": changed_count,
        "rows_scanned": total_rows,
        "sample_only": row_limit is not None,
        "sample_size": row_limit,
        "field_counts": field_counts,
        "examples": examples,
    }
