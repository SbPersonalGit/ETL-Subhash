"""
ID Mapper — persists source_id -> target_id mappings in DuckDB.

After each object is loaded to the target org, this mapper stores the
source-to-target ID relationship so that child objects can resolve their
parent lookup fields before being loaded.

Special case: User matching by Username (same person, different ID across orgs).
"""

import os
import duckdb
import polars as pl
from pathlib import Path

STATE_DIR = Path(__file__).parent.parent / "state"
DEFAULT_MAP_DB = STATE_DIR / "id_map.duckdb"


class IDMapper:
    """
    DuckDB-backed store for source_id → target_id mappings.
    One table per Salesforce object: idmap_{ObjectName}
    """

    def __init__(self, db_path: str | Path = DEFAULT_MAP_DB):
        override_path = (os.environ.get("ETL_ID_MAP_DB_PATH") or "").strip()
        self.db_path = str(override_path or db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(self.db_path)

    def close(self):
        self._conn.close()

    def import_deferred_tables(
        self,
        source_db_path: str | Path,
        object_names: set[str] | None = None,
    ) -> dict[str, int]:
        """Copy deferred-reference tables from a completed transform-stage run.

        A UI load-stage run has a new ID-map database but may reuse xfm CSVs
        produced by an earlier run. The deferred values are intentionally not
        present in those upload CSVs, so they must travel with the artifacts.
        """
        source_path = Path(source_db_path).resolve()
        if not source_path.is_file() or source_path == Path(self.db_path).resolve():
            return {}
        prefixes = {
            f"deferred_{str(name).strip()}_"
            for name in (object_names or set())
            if str(name).strip()
        }
        source = duckdb.connect(str(source_path), read_only=True)
        imported: dict[str, int] = {}
        try:
            table_names = [
                str(row[0])
                for row in source.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' ORDER BY table_name"
                ).fetchall()
            ]
            for table_name in table_names:
                if not table_name.startswith("deferred_"):
                    continue
                if prefixes and not any(table_name.startswith(prefix) for prefix in prefixes):
                    continue
                safe_name = table_name.replace('"', '""')
                arrow = source.execute(f'SELECT * FROM "{safe_name}"').arrow()
                self._conn.execute(
                    f'CREATE OR REPLACE TABLE "{safe_name}" AS SELECT * FROM arrow'
                )
                imported[table_name] = int(
                    self._conn.execute(f'SELECT count(*) FROM "{safe_name}"').fetchone()[0]
                )
        finally:
            source.close()
        return imported

    def _table(self, object_name: str) -> str:
        return f"idmap_{object_name}"

    def _ensure_table(self, object_name: str):
        table = self._table(object_name)
        self._conn.execute(f"""
            CREATE TABLE IF NOT EXISTS "{table}" (
                source_id VARCHAR PRIMARY KEY,
                target_id VARCHAR NOT NULL
            )
        """)

    # ------------------------------------------------------------------ #
    #  Write mappings                                                     #
    # ------------------------------------------------------------------ #

    def add_mapping(self, object_name: str, source_id: str, target_id: str):
        self._ensure_table(object_name)
        table = self._table(object_name)
        self._conn.execute(f"""
            INSERT OR REPLACE INTO "{table}" (source_id, target_id) VALUES (?, ?)
        """, [source_id, target_id])

    def bulk_add_mappings(self, object_name: str, mappings_df: pl.DataFrame):
        """
        Bulk insert mappings from a Polars DataFrame with columns:
          source_id, target_id
        """
        self._ensure_table(object_name)
        table = self._table(object_name)
        arrow = mappings_df.to_arrow()
        self._conn.execute(f"""
            INSERT OR REPLACE INTO "{table}" (source_id, target_id)
            SELECT source_id, target_id FROM arrow
        """)

    def remove_mappings(self, object_name: str, source_ids: list[str]):
        if not source_ids:
            return
        table = self._table(object_name)
        if not self._table_exists(table):
            return
        normalized = [str(source_id) for source_id in source_ids if source_id]
        if not normalized:
            return
        delete_df = pl.DataFrame({"source_id": normalized}).unique(subset=["source_id"])
        arrow = delete_df.to_arrow()
        self._conn.execute(f"""
            DELETE FROM "{table}"
            WHERE source_id IN (SELECT source_id FROM arrow)
        """)

    # ------------------------------------------------------------------ #
    #  Read mappings                                                      #
    # ------------------------------------------------------------------ #

    def get_target_id(self, object_name: str, source_id: str) -> str | None:
        table = self._table(object_name)
        if not self._table_exists(table):
            return None
        result = self._conn.execute(
            f'SELECT target_id FROM "{table}" WHERE source_id = ?', [source_id]
        ).fetchone()
        return result[0] if result else None

    def resolve_column(
        self, object_name: str, df: pl.DataFrame, source_col: str, target_col: str | None = None
    ) -> pl.DataFrame:
        """
        Resolve a column of source IDs to target IDs using the stored mapping.
        Replaces source_col in-place (or writes to target_col if provided).
        Unresolved IDs are left as null — will surface as load errors.
        """
        out_col = target_col or source_col
        table = self._table(object_name)

        if not self._table_exists(table):
            return df.with_columns(pl.lit(None).cast(pl.Utf8).alias(out_col))

        # Pull the mapping table into a Polars frame for a fast join
        arrow = self._conn.execute(
            f'SELECT source_id, target_id FROM "{table}"'
        ).arrow()
        mapping_df = pl.from_arrow(arrow).rename({"source_id": source_col, "target_id": "__resolved__"})

        df = df.join(mapping_df, on=source_col, how="left")

        if out_col == source_col:
            df = df.drop(source_col).rename({"__resolved__": out_col})
        else:
            df = df.rename({"__resolved__": out_col})

        return df

    # ------------------------------------------------------------------ #
    #  User matching — by Username instead of ID                         #
    # ------------------------------------------------------------------ #

    def build_user_map_by_username(
        self, source_users_df: pl.DataFrame, target_users_df: pl.DataFrame
    ):
        """
        Match source User records to target User records by Username.
        Populates the User idmap table.

        Both DataFrames must have columns: Id, Username
        """
        joined = source_users_df.select(["Id", "Username"]).join(
            target_users_df.select(["Id", "Username"]).rename({"Id": "target_id"}),
            on="Username",
            how="inner",
        ).rename({"Id": "source_id"}).select(["source_id", "target_id"])

        self.bulk_add_mappings("User", joined)
        return len(joined)

    # ------------------------------------------------------------------ #
    #  Cycle support: two-pass deferred ID resolution                     #
    # ------------------------------------------------------------------ #

    def save_deferred_column(
        self,
        object_name: str,
        field_name: str,
        source_record_id_col: pl.Series,
        deferred_lookup_source_id_col: pl.Series,
    ):
        """
        Store (source_record_id -> deferred_source_lookup_id) for two-pass cycle resolution.
        Table: deferred_{ObjectName}_{FieldName}
        """
        table = f"deferred_{object_name}_{field_name}"
        df = pl.DataFrame({
            "source_record_id": source_record_id_col,
            "deferred_source_id": deferred_lookup_source_id_col,
        }).filter(
            pl.col("source_record_id").is_not_null() & pl.col("deferred_source_id").is_not_null()
        )
        arrow = df.to_arrow()
        self._conn.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM arrow')

    def get_deferred_updates(
        self,
        object_name: str,
        field_name: str,
        referenced_object: str,
    ) -> pl.DataFrame | None:
        """
        Returns DataFrame with columns: target_record_id, resolved_target_id
        Used in Pass 2 of cycle resolution.
        """
        table = f"deferred_{object_name}_{field_name}"
        if not self._table_exists(table):
            return None

        source_map_table = self._table(object_name)
        referenced_map_table = self._table(referenced_object)
        if not self._table_exists(source_map_table) or not self._table_exists(referenced_map_table):
            return None

        arrow = self._conn.execute(f"""
            SELECT src.target_id AS target_record_id, ref.target_id AS resolved_target_id
            FROM "{table}" d
            JOIN "{source_map_table}" src ON src.source_id = d.source_record_id
            JOIN "{referenced_map_table}" ref ON ref.source_id = d.deferred_source_id
        """).arrow()
        schema = {"target_record_id": pl.Utf8, "resolved_target_id": pl.Utf8}
        try:
            return pl.from_arrow(arrow, schema=schema)
        except ValueError:
            return pl.DataFrame(schema=schema)

    def get_deferred_records(self, object_name: str, field_name: str) -> pl.DataFrame | None:
        """Return deferred source references with the loaded target record ID."""
        table = f"deferred_{object_name}_{field_name}"
        source_map_table = self._table(object_name)
        if not self._table_exists(table) or not self._table_exists(source_map_table):
            return None
        arrow = self._conn.execute(f"""
            SELECT src.target_id AS target_record_id,
                   CAST(d.deferred_source_id AS VARCHAR) AS deferred_source_id
            FROM "{table}" d
            JOIN "{source_map_table}" src ON src.source_id = d.source_record_id
        """).arrow()
        schema = {"target_record_id": pl.Utf8, "deferred_source_id": pl.Utf8}
        try:
            return pl.from_arrow(arrow, schema=schema)
        except ValueError:
            return pl.DataFrame(schema=schema)

    def deferred_count(self, object_name: str, field_name: str) -> int:
        table = f"deferred_{object_name}_{field_name}"
        if not self._table_exists(table):
            return 0
        return self._conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]

    # ------------------------------------------------------------------ #
    #  Utilities                                                          #
    # ------------------------------------------------------------------ #

    def has_mappings(self, object_name: str) -> bool:
        table = self._table(object_name)
        if not self._table_exists(table):
            return False
        result = self._conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()
        return bool(result and result[0] > 0)

    def _table_exists(self, table: str) -> bool:
        result = self._conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
        ).fetchone()
        return result[0] > 0

    def mapping_count(self, object_name: str) -> int:
        table = self._table(object_name)
        if not self._table_exists(table):
            return 0
        return self._conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]

    def clear_object(self, object_name: str):
        self._conn.execute(f'DROP TABLE IF EXISTS "{self._table(object_name)}"')
        deferred_tables = self._conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name LIKE ?",
            [f"deferred_{object_name}_%"],
        ).fetchall()
        for (table_name,) in deferred_tables:
            self._conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')

    def clear_all(self):
        """Drop every mapping/deferred table from the local DuckDB file."""
        tables = self._conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        for (table_name,) in tables:
            if table_name.startswith("idmap_") or table_name.startswith("deferred_"):
                self._conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
