"""Emit clean sObject + COUNT(Id) WHERE Legacy_ID__c != null results."""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "output" / "legacy_id_object_counts.csv"
OUT = ROOT / "output" / "legacy_id_soql_counts_updated.csv"


def main() -> None:
    rows = list(csv.DictReader(SRC.open(encoding="utf-8")))
    ok = [r for r in rows if r["Status"] == "OK"]
    ok.sort(key=lambda r: (-int(r["LegacyIdPopulatedCount"]), r["Object"]))

    with OUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sObject", "RecordCount"])
        for row in ok:
            writer.writerow([row["Object"], int(row["LegacyIdPopulatedCount"])])

    print(f"Wrote {len(ok)} rows to {OUT}")
    print("SOQL used per object: SELECT COUNT(Id) FROM <sObject> WHERE Legacy_ID__c != null")
    print()
    for row in ok:
        print(f"{row['Object']}\t{row['LegacyIdPopulatedCount']}")


if __name__ == "__main__":
    main()
