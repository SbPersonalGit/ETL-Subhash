"""
Stager — DuckDB-backed staging layer.
All extracted data lands here before transformation and load.
Processed in chunks — never loads full dataset into memory.
"""

import os
import csv
import duckdb
import polars as pl
import pyarrow as pa
from pathlib import Path

STATE_DIR = Path(__file__).parent.parent / "state"
DEFAULT_DB_PATH = STATE_DIR / "staging.duckdb"
CHUNK_SIZE = 50_000


class Stager:
    """
    Manages a DuckDB database used as local staging storage.
    Each Salesforce object gets its own table: raw_{ObjectName} and xfm_{ObjectName}.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        override_path = (os.environ.get("ETL_STAGING_DB_PATH") or "").strip()
        self.db_path = str(override_path or db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(self.db_path)

    def close(self):
        self._conn.close()

    # ------------------------------------------------------------------ #
    #  Raw staging (as extracted from source)                            #
    # ------------------------------------------------------------------ #

    def raw_table(self, object_name: str) -> str:
        return f"raw_{object_name}"

    def xfm_table(self, object_name: str) -> str:
        return f"xfm_{object_name}"

    def table_exists(self, table_name: str) -> bool:
        result = self._conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [table_name],
        ).fetchone()
        return result[0] > 0

    def drop_table(self, object_name: str, stage: str = "raw"):
        table = self.raw_table(object_name) if stage == "raw" else self.xfm_table(object_name)
        self._conn.execute(f'DROP TABLE IF EXISTS "{table}"')

    def write_raw_chunk(self, object_name: str, df: pl.DataFrame):
        """
        Append a Polars DataFrame chunk into the raw staging table.
        Creates the table on first write (schema inferred from df).
        """
        table = self.raw_table(object_name)
        arrow = df.to_arrow()
        if not self.table_exists(table):
            self._conn.execute(f'CREATE TABLE "{table}" AS SELECT * FROM arrow')
        else:
            self._conn.execute(f'INSERT INTO "{table}" SELECT * FROM arrow')

    def write_xfm_chunk(self, object_name: str, df: pl.DataFrame):
        """Write a transformed chunk into the xfm staging table."""
        table = self.xfm_table(object_name)
        arrow = df.to_arrow()
        if not self.table_exists(table):
            self._conn.execute(f'CREATE TABLE "{table}" AS SELECT * FROM arrow')
        else:
            self._conn.execute(f'INSERT INTO "{table}" SELECT * FROM arrow')

    def load_csv_to_stage(self, object_name: str, csv_path: str | Path, stage: str = "xfm"):
        """Replace a staged table from a CSV artifact on disk."""
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"CSV not found: {path}")

        self.drop_table(object_name, stage)
        from core.flat_file import iter_csv_batches, open_csv

        with open_csv(path) as handle:
            header = next(csv.reader(handle), [])
        schema_overrides = {
            column_name: pl.Utf8
            for column_name in header
            if column_name
        }
        for batch in iter_csv_batches(path, schema_overrides, CHUNK_SIZE):
            if stage == "raw":
                self.write_raw_chunk(object_name, batch)
            else:
                self.write_xfm_chunk(object_name, batch)

    def read_raw_chunks(self, object_name: str, chunk_size: int = CHUNK_SIZE):
        """Yield Polars DataFrames in chunks from the raw staging table."""
        yield from self._read_chunks(self.raw_table(object_name), chunk_size)

    def read_xfm_chunks(self, object_name: str, chunk_size: int = CHUNK_SIZE):
        """Yield Polars DataFrames in chunks from the transformed staging table."""
        yield from self._read_chunks(self.xfm_table(object_name), chunk_size)

    def _read_chunks(self, table: str, chunk_size: int):
        total = self.get_count_by_table(table)
        offset = 0
        while offset < total:
            arrow = self._conn.execute(
                f'SELECT * FROM "{table}" LIMIT ? OFFSET ?', [chunk_size, offset]
            ).arrow()
            yield pl.from_arrow(arrow)
            offset += chunk_size

    def get_raw_count(self, object_name: str) -> int:
        return self.get_count_by_table(self.raw_table(object_name))

    def get_xfm_count(self, object_name: str) -> int:
        return self.get_count_by_table(self.xfm_table(object_name))

    def get_count_by_table(self, table: str) -> int:
        if not self.table_exists(table):
            return 0
        result = self._conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()
        return result[0]

    def get_column_names(self, object_name: str, stage: str = "raw") -> list[str]:
        table = self.raw_table(object_name) if stage == "raw" else self.xfm_table(object_name)
        result = self._conn.execute(f'DESCRIBE "{table}"').fetchall()
        return [row[0] for row in result]

    def execute_sql(self, sql: str, params: list = None) -> pl.DataFrame:
        """Run arbitrary SQL against the staging DB — for advanced transforms."""
        raw = self._conn.execute(sql, params or []).arrow()
        if raw is None:
            return pl.DataFrame()
        # DuckDB may return a Table or a RecordBatchReader depending on
        # version; normalize so Polars always receives a Table.
        if isinstance(raw, pa.RecordBatchReader):
            raw = raw.read_all()
        if raw.num_columns == 0:
            return pl.DataFrame()
        try:
            return pl.from_arrow(raw)
        except ValueError as exc:
            # Empty / degenerate Arrow payloads can fail conversion.
            if "RecordBatch" in str(exc) or "schema" in str(exc).lower():
                return pl.DataFrame()
            raise

    def get_distinct_values(self, object_name: str, column_name: str, stage: str = "raw") -> list[str]:
        table = self.raw_table(object_name) if stage == "raw" else self.xfm_table(object_name)
        if not self.table_exists(table):
            return []
        rows = self._conn.execute(
            f'SELECT DISTINCT "{column_name}" FROM "{table}" WHERE "{column_name}" IS NOT NULL'
        ).fetchall()
        return [row[0] for row in rows if row and row[0] is not None]

    def find_duplicate_keys(
        self,
        object_name: str,
        key_columns: list[str],
        stage: str = "raw",
        limit: int = 20,
    ) -> list[dict]:
        """Find duplicate nonblank keys with DuckDB, without loading keys into memory."""
        table = self.raw_table(object_name) if stage == "raw" else self.xfm_table(object_name)
        if not self.table_exists(table) or not key_columns:
            return []
        available = set(self.get_column_names(object_name, stage))
        if any(column not in available for column in key_columns):
            return []

        def quote(identifier: str) -> str:
            return '"' + identifier.replace('"', '""') + '"'

        keys = ", ".join(quote(column) for column in key_columns)
        populated = " AND ".join(
            f"NULLIF(TRIM(CAST({quote(column)} AS VARCHAR)), '') IS NOT NULL"
            for column in key_columns
        )
        rows = self._conn.execute(
            f'''SELECT {keys}, COUNT(*) AS duplicate_count
                FROM {quote(table)}
                WHERE {populated}
                GROUP BY {keys}
                HAVING COUNT(*) > 1
                ORDER BY duplicate_count DESC
                LIMIT ?''',
            [max(1, int(limit))],
        ).fetchall()
        return [
            {
                **dict(zip(key_columns, row[:len(key_columns)])),
                "duplicate_count": row[len(key_columns)],
            }
            for row in rows
        ]

    def clear_object(self, object_name: str):
        """Drop both raw and xfm tables for a fresh re-extract."""
        self.drop_table(object_name, "raw")
        self.drop_table(object_name, "xfm")

    def clear_all(self):
        """Drop every staged raw/xfm table from the local DuckDB file."""
        tables = self._conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        for (table_name,) in tables:
            if table_name.startswith("raw_") or table_name.startswith("xfm_"):
                self._conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')

    def deduplicate_object(self, object_name: str, stage: str = "raw", key_column: str = "Id"):
        table = self.raw_table(object_name) if stage == "raw" else self.xfm_table(object_name)
        if not self.table_exists(table):
            return

        columns = self.get_column_names(object_name, stage)
        if key_column not in columns:
            return

        quoted_columns = ", ".join(f'"{col}"' for col in columns)
        self._conn.execute(
            f'''
            CREATE OR REPLACE TABLE "{table}" AS
            SELECT {quoted_columns}
            FROM (
                SELECT {quoted_columns},
                       ROW_NUMBER() OVER (PARTITION BY "{key_column}") AS "__rn"
                FROM "{table}"
            )
            WHERE "__rn" = 1
            '''
        )

    def list_staged_objects(self) -> dict[str, dict]:
        """Return all staged objects with their row counts."""
        tables = self._conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        result = {}
        for (tname,) in tables:
            if tname.startswith("raw_") or tname.startswith("xfm_"):
                prefix, obj = tname.split("_", 1)
                if obj not in result:
                    result[obj] = {}
                result[obj][prefix] = self.get_count_by_table(tname)
        return result
