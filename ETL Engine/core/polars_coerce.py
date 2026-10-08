"""
Helpers for normalizing Polars frames before CSV/DuckDB staging.

Salesforce REST query results use nested objects for compound fields (Address,
Location, etc.). Polars represents those as Struct; casting them directly to
Utf8 fails — serialize to JSON text first.
"""

from __future__ import annotations

import json

import polars as pl


def _json_cell(v: object) -> str | None:
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, default=str)


def coerce_dataframe_columns_to_utf8(df: pl.DataFrame) -> pl.DataFrame:
    """
    Cast every column to Utf8 for consistent staging and string-based transforms.

    Struct columns (compound Salesforce fields) become JSON strings.
    Other types use cast(..., strict=False) so odd scalars degrade to null instead of failing.
    """
    if df.is_empty():
        return df

    exprs: list[pl.Expr] = []
    for name, dtype in df.schema.items():
        col = pl.col(name)
        if isinstance(dtype, pl.Struct):
            exprs.append(
                col.map_elements(_json_cell, return_dtype=pl.Utf8).alias(name)
            )
        elif dtype == pl.Object:
            exprs.append(
                col.map_elements(
                    lambda v: None if v is None else str(v),
                    return_dtype=pl.Utf8,
                ).alias(name)
            )
        else:
            exprs.append(col.cast(pl.Utf8, strict=False).alias(name))

    return df.with_columns(exprs)
