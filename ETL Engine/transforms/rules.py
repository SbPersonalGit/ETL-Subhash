"""
Built-in Transform Rules.

Each rule is a callable that takes a Polars DataFrame and returns
a modified Polars DataFrame. Rules are stateless and composable.

Supported rule types (configured in migration YAML):
  - field_rename     : rename a source field to a different target field name
  - value_map        : translate picklist/string values using a lookup map
  - null_default     : fill null values with a static default
  - static_value     : set a field to a fixed value for all records
  - conditional      : if field == X then set target_field = Y else Z
  - trim             : strip leading/trailing whitespace from a string field
  - uppercase        : convert string field to uppercase
  - lowercase        : convert string field to lowercase
  - type_cast        : cast field to a target type (string, integer, float, boolean, date)
  - text_to_decimal  : parse formatted text as numeric with null/zero fallback
  - boolean_convert  : convert 1/0 to true/false or true/false to 1/0
  - concat           : concatenate multiple fields into one
  - split            : split a field into multiple fields by a delimiter
  - anonymize_email  : deterministically replace an email address with a masked value
  - anonymize_phone  : deterministically replace phone digits while preserving separators
  - id_remap         : replace source SF ID with target SF ID (handled by transformer, registered here)
"""

import hashlib
import itertools
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
import polars as pl
from typing import Any


RuleConfig = dict[str, Any]


def apply_rule(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """Dispatch a single rule config to the appropriate handler."""
    rule_type = rule.get("type")
    handlers = {
        "field_rename": _field_rename,
        "value_map": _value_map,
        "null_default": _null_default,
        "static_value": _static_value,
        "conditional": _conditional,
        "trim": _trim,
        "uppercase": _uppercase,
        "lowercase": _lowercase,
        "type_cast": _type_cast,
        "text_to_decimal": _text_to_decimal,
        "boolean_convert": _boolean_convert,
        "date_convert": _date_convert,
        "concat": _concat,
        "split": _split,
        "anonymize_email": _anonymize_email,
        "anonymize_phone": _anonymize_phone,
    }
    handler = handlers.get(rule_type)
    if handler is None:
        raise ValueError(f"Unknown transform rule type: '{rule_type}'")
    return handler(df, rule)


def apply_rules(df: pl.DataFrame, rules: list[RuleConfig]) -> pl.DataFrame:
    """Apply a list of rules sequentially to a DataFrame."""
    for rule in rules:
        df = apply_rule(df, rule)
    return df


# ------------------------------------------------------------------ #
#  Rule implementations                                               #
# ------------------------------------------------------------------ #

def _field_rename(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """
    Rename source_field to target_field.
    config: {type: field_rename, source: OldName, target: NewName}
    """
    src = rule["source"]
    tgt = rule["target"]
    if src in df.columns:
        # Cross-org migrations intentionally copy source Id into a target
        # external-ID field. If the incoming dataset already has that target
        # column, source Id is authoritative and replaces it.
        if tgt != src and tgt in df.columns:
            df = df.drop(tgt)
        df = df.rename({src: tgt})
    return df


def _value_map(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """
    Map values in a field using a lookup dict.
    Unmapped values are left unchanged unless default is provided.
    config: {type: value_map, field: Industry, map: {Tech: Technology}, default: Other}
    """
    field = rule["field"]
    mapping = rule["map"]
    default = rule.get("default")

    if field not in df.columns:
        return df

    expr = pl.col(field)
    for src_val, tgt_val in mapping.items():
        expr = pl.when(pl.col(field) == src_val).then(pl.lit(tgt_val)).otherwise(expr)

    if default is not None:
        expr = pl.when(pl.col(field).is_in(list(mapping.keys()))).then(expr).otherwise(pl.lit(default))

    return df.with_columns(expr.alias(field))


def _null_default(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """
    Fill null values with a default.
    config: {type: null_default, field: Status__c, default: Active}
    """
    field = rule["field"]
    # A partially configured UI rule must not abort the entire object/pipeline.
    # With no default value there is no transformation to apply.
    if "default" not in rule:
        return df
    default = rule["default"]
    if field in df.columns:
        df = df.with_columns(pl.col(field).fill_null(pl.lit(str(default))).alias(field))
    return df


def _static_value(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """
    Set all rows of a field to a fixed value (overwrite or create).
    config: {type: static_value, field: Migration_Batch__c, value: "2025-Q1"}
    """
    field = rule["field"]
    value = rule["value"]
    return df.with_columns(pl.lit(str(value)).alias(field))


def _conditional(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """
    Conditional field assignment.
    config:
      type: conditional
      source_field: Status
      condition_value: Closed
      target_field: IsActive__c
      then_value: "false"
      else_value: "true"
    """
    src = rule["source_field"]
    cond_val = rule["condition_value"]
    tgt = rule["target_field"]
    then_val = rule["then_value"]
    else_val = rule["else_value"]

    if src not in df.columns:
        return df

    df = df.with_columns(
        pl.when(pl.col(src) == cond_val)
        .then(pl.lit(str(then_val)))
        .otherwise(pl.lit(str(else_val)))
        .alias(tgt)
    )
    return df


def _trim(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    field = rule["field"]
    if field in df.columns:
        df = df.with_columns(pl.col(field).str.strip_chars().alias(field))
    return df


def _uppercase(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    field = rule["field"]
    if field in df.columns:
        df = df.with_columns(pl.col(field).str.to_uppercase().alias(field))
    return df


def _lowercase(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    field = rule["field"]
    if field in df.columns:
        df = df.with_columns(pl.col(field).str.to_lowercase().alias(field))
    return df


def _type_cast(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """
    Cast a field to a target type.
    config: {type: type_cast, field: Amount, to: float}
    Supported: string, integer, float, boolean, date, datetime
    """
    field = rule["field"]
    to_type = rule["to"]
    type_map = {
        "string": pl.Utf8,
        "integer": pl.Int64,
        "float": pl.Float64,
        "boolean": pl.Boolean,
        "date": pl.Date,
        "datetime": pl.Datetime,
    }
    polars_type = type_map.get(to_type)
    if polars_type and field in df.columns:
        df = df.with_columns(pl.col(field).cast(polars_type).alias(field))
    return df


def _parse_decimal_value(
    value: Any, invalid_value: str, decimal_places: int | None
) -> float | None:
    fallback = 0.0 if invalid_value == "zero" else None
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    negative_parentheses = text.startswith("(") and text.endswith(")")
    if negative_parentheses:
        text = text[1:-1]
    normalized = re.sub(r"[,\s$€£₹]", "", text)
    try:
        parsed = Decimal(normalized)
        if not parsed.is_finite():
            return fallback
        if negative_parentheses:
            parsed = -parsed
        if decimal_places is not None:
            quantum = Decimal(1).scaleb(-decimal_places)
            parsed = parsed.quantize(quantum)
        return float(parsed)
    except (InvalidOperation, ValueError):
        return fallback


def _text_to_decimal(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """
    Parse formatted text into a decimal-compatible numeric value.
    config:
      {type: text_to_decimal, field: AnnualRevenue,
       invalid_value: zero, decimal_places: 2}
    """
    field = rule["field"]
    if field not in df.columns:
        return df
    invalid_value = str(rule.get("invalid_value") or "null").strip().lower()
    if invalid_value not in {"null", "zero"}:
        raise ValueError("text_to_decimal invalid_value must be 'null' or 'zero'")
    raw_places = rule.get("decimal_places")
    decimal_places = None if raw_places in (None, "") else int(raw_places)
    if decimal_places is not None and not 0 <= decimal_places <= 18:
        raise ValueError("text_to_decimal decimal_places must be between 0 and 18")
    return df.with_columns(
        pl.col(field)
        .map_elements(
            lambda value: _parse_decimal_value(value, invalid_value, decimal_places),
            return_dtype=pl.Float64,
            skip_nulls=False,
        )
        .alias(field)
    )


def _normalized_boolean(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true"}:
        return True
    if text in {"0", "false"}:
        return False
    return None


def _date_convert(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """Convert a selected source date format to Salesforce YYYY-MM-DD."""
    field = str(rule.get("field") or "").strip()
    if not field or field not in df.columns:
        return df
    source_format = str(rule.get("source_format") or "mdy2").strip().lower()
    formats = {
        "mdy2": "%m/%d/%y", "mdy4": "%m/%d/%Y",
        "dmy2": "%d/%m/%y", "dmy4": "%d/%m/%Y",
        "dmy2_dash": "%d-%m-%y", "dmy4_dash": "%d-%m-%Y", "iso": "%Y-%m-%d",
    }
    date_format = formats.get(source_format)
    if not date_format:
        raise ValueError("date_convert source_format is unsupported")
    invalid_value = str(rule.get("invalid_value") or "retain").strip().lower()
    if invalid_value not in {"retain", "null"}:
        raise ValueError("date_convert invalid_value must be 'retain' or 'null'")

    def convert(value):
        if value is None or not str(value).strip():
            return None
        text = str(value).strip()
        # Excel/CSV exports commonly append a midnight time to values destined
        # for Salesforce Date fields. Removing midnight is non-lossy; retain a
        # non-midnight value so the configured invalid-value policy still
        # controls it instead of silently discarding meaningful time data.
        text = re.sub(r"\s+0{1,2}:00(?::00(?:\.0+)?)?\s*$", "", text)
        try:
            return datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            return None if invalid_value == "null" else text

    return df.with_columns(
        pl.col(field).map_elements(convert, return_dtype=pl.Utf8).alias(field)
    )


def _boolean_convert(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """
    Convert common Boolean representations in either direction.
    config:
      {type: boolean_convert, field: Active__c, direction: number_to_boolean}
      {type: boolean_convert, field: Active__c, direction: boolean_to_number}
    Invalid values and nulls become null.
    """
    field = rule["field"]
    if field not in df.columns:
        return df
    direction = str(rule.get("direction") or "number_to_boolean").strip().lower()
    if direction not in {"number_to_boolean", "boolean_to_number"}:
        raise ValueError(
            "boolean_convert direction must be 'number_to_boolean' or 'boolean_to_number'"
        )
    if direction == "number_to_boolean":
        return df.with_columns(
            pl.col(field)
            .map_elements(
                _normalized_boolean,
                return_dtype=pl.Boolean,
                skip_nulls=False,
            )
            .alias(field)
        )
    return df.with_columns(
        pl.col(field)
        .map_elements(
            lambda value: (
                None
                if _normalized_boolean(value) is None
                else int(_normalized_boolean(value))
            ),
            return_dtype=pl.Int64,
            skip_nulls=False,
        )
        .alias(field)
    )


def _concat(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """
    Concatenate multiple fields into one.
    config: {type: concat, fields: [FirstName, LastName], separator: " ", target: FullName}
    """
    fields = rule["fields"]
    separator = rule.get("separator", "")
    target = rule["target"]
    present = [f for f in fields if f in df.columns]
    if not present:
        return df
    expr = pl.concat_str([pl.col(f) for f in present], separator=separator)
    return df.with_columns(expr.alias(target))


def _split(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """
    Split a field by delimiter into multiple target fields.
    config: {type: split, field: FullName, delimiter: " ", targets: [FirstName, LastName]}
    """
    field = rule["field"]
    delimiter = rule.get("delimiter", " ")
    targets = rule["targets"]
    if field not in df.columns:
        return df
    for i, tgt in enumerate(targets):
        df = df.with_columns(
            pl.col(field).str.split(delimiter).list.get(i).alias(tgt)
        )
    return df


def _split_email_parts(text: str) -> tuple[str, str]:
    if "@" not in text:
        return text, ""
    local_part, domain = text.split("@", 1)
    return local_part, domain


def _masked_email(value: Any, rule: RuleConfig) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    mode = (rule.get("mode") or "hash").strip().lower() or "hash"
    local_part, original_domain = _split_email_parts(text)

    if mode == "append_suffix":
        suffix = str(rule.get("suffix") or "mask")
        if not original_domain:
            return f"{local_part}{suffix}"
        return f"{local_part}{suffix}@{original_domain}"

    if mode == "replace_domain":
        domain = (rule.get("domain") or "mask.com").strip() or "mask.com"
        if not local_part:
            return f"maskedemail@{domain}"
        return f"{local_part}@{domain}"

    if mode == "fixed_local":
        fixed_local = (rule.get("local_part") or "maskedemail").strip() or "maskedemail"
        if not original_domain:
            return fixed_local
        return f"{fixed_local}@{original_domain}"

    if mode == "fixed_full":
        fixed_local = (rule.get("local_part") or "maskedemail").strip() or "maskedemail"
        domain = (rule.get("domain") or "mask.com").strip() or "mask.com"
        return f"{fixed_local}@{domain}"

    domain = (rule.get("domain") or "anonymized.invalid").strip() or "anonymized.invalid"
    digest = hashlib.sha1(text.lower().encode("utf-8")).hexdigest()[:12]
    return f"user_{digest}@{domain}"


def _anonymize_email(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """
    Replace an email with a deterministic masked value.
    config examples:
      {type: anonymize_email, field: Email, mode: hash, domain: masked.example}
      {type: anonymize_email, field: Email, mode: append_suffix, suffix: mask}
      {type: anonymize_email, field: Email, mode: replace_domain, domain: mask.com}
      {type: anonymize_email, field: Email, mode: fixed_local, local_part: maskedemail}
      {type: anonymize_email, field: Email, mode: fixed_full, local_part: maskedemail, domain: mask.com}
    """
    field = rule["field"]
    if field not in df.columns:
        return df
    return df.with_columns(
        pl.col(field)
        .map_elements(lambda value: _masked_email(value, rule), return_dtype=pl.Utf8)
        .alias(field)
    )


def _masked_phone(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text

    digits = re.findall(r"\d", text)
    if not digits:
        return "0000000000"

    digest_bytes = hashlib.sha256(text.encode("utf-8")).digest()
    digit_stream = "".join(str(byte % 10) for byte in digest_bytes)
    replacement_digits = list(itertools.islice(itertools.cycle(digit_stream), len(digits)))
    digit_iter = iter(replacement_digits)
    masked_chars: list[str] = []
    for char in text:
        if char.isdigit():
            next_digit = next(digit_iter)
            masked_chars.append("9" if next_digit == "0" else next_digit)
        else:
            masked_chars.append(char)
    return "".join(masked_chars)


def _anonymize_phone(df: pl.DataFrame, rule: RuleConfig) -> pl.DataFrame:
    """
    Replace phone digits with deterministic masked digits while preserving formatting.
    config: {type: anonymize_phone, field: Phone}
    """
    field = rule["field"]
    if field not in df.columns:
        return df
    return df.with_columns(
        pl.col(field)
        .map_elements(_masked_phone, return_dtype=pl.Utf8)
        .alias(field)
    )
