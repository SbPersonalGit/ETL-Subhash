"""Salesforce ID helpers.

18-character IDs encode uppercase/lowercase of the first 15 characters in a
3-character checksum. Bulk API 2.0 failedResults often lowercases those IDs in
error text, which breaks case-sensitive External ID matching and makes failed
CSVs hard to reconcile.
"""

from __future__ import annotations

import re

_CHECKSUM_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
_SF_ID_18 = re.compile(r"^[a-zA-Z0-9]{18}$")
_SF_ID_TOKEN = re.compile(r"\b[a-zA-Z0-9]{18}\b")


def encode_salesforce_id_checksum(id15: str) -> str:
    body = str(id15 or "")
    if len(body) != 15:
        raise ValueError("Salesforce ID body must be 15 characters")
    suffix: list[str] = []
    for block in range(3):
        flags = 0
        for bit in range(5):
            char = body[block * 5 + bit]
            if "A" <= char <= "Z":
                flags |= 1 << bit
        suffix.append(_CHECKSUM_ALPHABET[flags])
    return body + "".join(suffix)


def restore_salesforce_id_casing(value: str) -> str:
    text = str(value or "").strip()
    if not _SF_ID_18.fullmatch(text):
        return text
    body = text[:15]
    checksum = text[15:].upper()
    restored: list[str] = []
    for block, check_char in enumerate(checksum):
        try:
            flags = _CHECKSUM_ALPHABET.index(check_char)
        except ValueError:
            return text
        for bit in range(5):
            char = body[block * 5 + bit]
            restored.append(char.upper() if flags & (1 << bit) else char.lower())
    restored_id = "".join(restored) + checksum
    try:
        if encode_salesforce_id_checksum(restored_id[:15]) != restored_id:
            return text
    except ValueError:
        return text
    return restored_id


def restore_salesforce_ids_in_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return text
    return _SF_ID_TOKEN.sub(lambda match: restore_salesforce_id_casing(match.group(0)), text)


def restore_salesforce_ids_in_frame(df):
    """Restore 18-character Salesforce ID casing in Bulk API result frames."""
    import polars as pl

    if df is None or df.is_empty():
        return df
    updates = []
    for name in df.columns:
        if df.schema.get(name) not in (pl.Utf8, pl.String):
            continue
        values = df.get_column(name).to_list()
        if name == "sf__Error":
            restored_errors = [restore_salesforce_ids_in_text(value) for value in values]
            if restored_errors != values:
                updates.append(pl.Series(name, restored_errors, dtype=pl.Utf8))
            continue
        restored_values = []
        changed = False
        for value in values:
            if isinstance(value, str) and len(value) == 18:
                restored = restore_salesforce_id_casing(value)
                changed = changed or restored != value
                restored_values.append(restored)
            else:
                restored_values.append(value)
        if changed:
            updates.append(pl.Series(name, restored_values, dtype=pl.Utf8))
    return df.with_columns(updates) if updates else df
