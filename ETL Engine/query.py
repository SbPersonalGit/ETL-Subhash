"""
Quick DuckDB query tool for SF ETL staging database.

Usage:
    python query.py "SHOW TABLES"
    python query.py "SELECT * FROM raw_PricebookEntry LIMIT 10"
    python query.py "SELECT count(*) FROM raw_Pricebook2"
"""

import sys
import duckdb
from pathlib import Path

DB_PATH = Path(__file__).parent / "state" / "staging.duckdb"

if not DB_PATH.exists():
    print(f"No staging database found at {DB_PATH}")
    print("Run 'python cli.py extract' first.")
    sys.exit(1)

if len(sys.argv) < 2:
    print("Usage: python query.py \"<SQL>\"")
    print("Examples:")
    print('  python query.py "SHOW TABLES"')
    print('  python query.py "SELECT * FROM raw_PricebookEntry LIMIT 10"')
    sys.exit(0)

sql = " ".join(sys.argv[1:])

con = duckdb.connect(str(DB_PATH), read_only=True)
try:
    result = con.execute(sql).df()
    print(result.to_string(index=False))
    print(f"\n({len(result)} rows)")
finally:
    con.close()
