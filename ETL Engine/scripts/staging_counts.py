from __future__ import annotations

from pathlib import Path


def main() -> int:
    try:
        import duckdb  # type: ignore
    except Exception as exc:
        print(f"duckdb import failed: {exc}")
        return 2

    db_path = Path("state/staging.duckdb")
    print(f"db_path={db_path}")
    print(f"db_exists={db_path.exists()}")
    if not db_path.exists():
        return 1

    # Try read-only first: staging DB is often held open by a running pipeline.
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        opened_mode = "read_only"
    except Exception:
        con = duckdb.connect(str(db_path))
        opened_mode = "read_write"
    print(f"duckdb_open_mode={opened_mode}")
    try:
        tables = [
            row[0]
            for row in con.execute(
                "select table_name from information_schema.tables where table_schema='main'"
            ).fetchall()
        ]
        raw_tables = [t for t in tables if t.startswith("raw_")]

        counts: list[tuple[str, int]] = []
        for t in raw_tables:
            # DuckDB identifiers may need quoting; use double quotes.
            cnt = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            counts.append((t, int(cnt)))

        counts.sort(key=lambda pair: pair[1], reverse=True)
        total = sum(c for _, c in counts)

        print(f"raw_tables={len(raw_tables)}")
        print(f"raw_total_rows={total}")
        print("top10=")
        for t, c in counts[:10]:
            print(f"- {t}: {c}")
    finally:
        con.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

