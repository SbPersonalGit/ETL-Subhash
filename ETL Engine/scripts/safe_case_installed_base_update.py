from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests


FIELD = "GE_OG_CIR_Unit_Serial_number__c"
API = "v68.0"


def sf_session(alias: str):
    executable = shutil.which("sf.cmd") or shutil.which("sf") or "sf.cmd"
    completed = subprocess.run(
        [executable, "org", "display", "--target-org", alias, "--json"],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(completed.stdout)
    result = payload["result"]
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {result['accessToken']}"
    return session, result["instanceUrl"].rstrip("/")


def query(session, base: str, soql: str) -> list[dict]:
    response = session.get(f"{base}/services/data/{API}/query", params={"q": soql}, timeout=(15, 120))
    response.raise_for_status()
    data = response.json()
    records = data.get("records", [])
    while not data.get("done", True):
        response = session.get(base + data["nextRecordsUrl"], timeout=(15, 120))
        response.raise_for_status(); data = response.json(); records.extend(data.get("records", []))
    return records


def chunks(values, size=100):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def quoted(values):
    return ",".join("'" + value.replace("'", "\\'") + "'" for value in values)


def full_cases(session, base: str, legacy_ids: list[str]) -> list[dict]:
    records = []
    for batch in chunks(legacy_ids, 100):
        records.extend(query(session, base, f"SELECT FIELDS(ALL) FROM Case WHERE Legacy_ID__c IN ({quoted(batch)}) LIMIT 200"))
    return records


def flat(record: dict) -> dict:
    return {key: (json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value)
            for key, value in record.items() if key != "attributes"}


def write_csv(path: Path, rows: list[dict], headers: list[str] | None = None):
    headers = headers or sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def prepare(args):
    source = Path(args.input)
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    if any(run_dir.iterdir()):
        raise ValueError(f"Run directory is not empty: {run_dir}")
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 500 or len({r["Case_Id"] for r in rows}) != 500:
        raise ValueError("Expected exactly 500 rows and 500 unique Case_Id values")
    session, base = sf_session(args.org)
    fallback = query(session, base, f"SELECT Id, Name FROM GE_Installed_Base__c WHERE Id = '{args.fallback_id}'")
    if len(fallback) != 1:
        raise ValueError("Fallback Installed Base does not resolve uniquely")
    cases = full_cases(session, base, [r["Case_Id"] for r in rows])
    case_map = {str(r.get("Legacy_ID__c") or ""): r for r in cases}
    if len(case_map) != 500:
        raise ValueError(f"Only {len(case_map)} of 500 target Cases resolved")
    found_ids = sorted({r[FIELD] for r in rows if r["MatchStatus"] == "FOUND"})
    installed = []
    for batch in chunks(found_ids):
        installed.extend(query(session, base, f"SELECT Id, Legacy_ID__c FROM GE_Installed_Base__c WHERE Legacy_ID__c IN ({quoted(batch)})"))
    installed_map = {r["Legacy_ID__c"]: r["Id"] for r in installed}
    if len(installed_map) != len(found_ids):
        raise ValueError(f"Only {len(installed_map)} of {len(found_ids)} Installed Base references resolved")
    plan = []
    rollback = []
    for row in rows:
        case = case_map[row["Case_Id"]]
        desired = args.fallback_id if row["MatchStatus"] == "MISSING" else installed_map[row[FIELD]]
        current = str(case.get(FIELD) or "")
        if current:
            raise ValueError(f"Safety stop: target Case {case['Id']} already has {FIELD}={current}")
        plan.append({"Id": case["Id"], FIELD: desired, "Legacy_Case_Id": row["Case_Id"],
                     "Source_Installed_Base_Id": row[FIELD], "MatchStatus": row["MatchStatus"]})
        rollback.append({"Id": case["Id"], FIELD: current})
    write_csv(run_dir / "case_full_backup_before.csv", [flat(r) for r in cases])
    write_csv(run_dir / "case_installed_base_update.csv", plan,
              ["Id", FIELD, "Legacy_Case_Id", "Source_Installed_Base_Id", "MatchStatus"])
    write_csv(run_dir / "case_installed_base_rollback.csv", rollback, ["Id", FIELD])
    (run_dir / "source_input.csv").write_bytes(source.read_bytes())
    manifest = {"status": "PREPARED", "created_at": datetime.now(timezone.utc).isoformat(), "org": args.org,
                "input_rows": 500, "target_cases_backed_up": 500, "full_backup_columns": len(flat(cases[0])),
                "found_rows": 494, "fallback_rows": 6, "fallback_id": args.fallback_id, "field": FIELD}
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


def execute(args):
    run_dir = Path(args.run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] != "PREPARED":
        raise ValueError(f"Run is not prepared: {manifest['status']}")
    with (run_dir / "case_installed_base_update.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        plan = list(csv.DictReader(handle))
    session, base = sf_session(args.org)
    results = []
    for batch in chunks(plan, 25):
        payload = {"allOrNone": False, "records": [{"attributes": {"type": "Case"}, "Id": r["Id"], FIELD: r[FIELD]} for r in batch]}
        response = session.patch(f"{base}/services/data/{API}/composite/sobjects", json=payload, timeout=(15, 120))
        response.raise_for_status()
        results.extend(response.json())
    result_rows = [{"Id": plan[i]["Id"], "success": item.get("success"),
                    "errors": json.dumps(item.get("errors", []), ensure_ascii=False)} for i, item in enumerate(results)]
    write_csv(run_dir / "update_results.csv", result_rows, ["Id", "success", "errors"])
    after = full_cases(session, base, [r["Legacy_Case_Id"] for r in plan])
    after_map = {r["Id"]: r for r in after}
    validation = []
    for row, result in zip(plan, results):
        actual = str(after_map.get(row["Id"], {}).get(FIELD) or "")
        validation.append({"Id": row["Id"], "Legacy_Case_Id": row["Legacy_Case_Id"], "expected": row[FIELD],
                           "actual": actual, "status": "MATCH" if actual == row[FIELD] else "MISMATCH",
                           "dml_success": result.get("success")})
    write_csv(run_dir / "case_full_backup_after.csv", [flat(r) for r in after])
    write_csv(run_dir / "post_validation.csv", validation)
    counts = Counter(v["status"] for v in validation)
    successes = sum(bool(r.get("success")) for r in results)
    manifest.update({"status": "COMPLETED" if successes == 500 and counts["MATCH"] == 500 else "PARTIAL_FAILED",
                     "completed_at": datetime.now(timezone.utc).isoformat(), "dml_success": successes,
                     "dml_failed": 500 - successes, "post_validation_matches": counts["MATCH"],
                     "post_validation_mismatches": counts["MISMATCH"]})
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


def artifacts(args):
    run_dir = Path(args.run_dir)
    with (run_dir / "case_installed_base_update.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        plan = list(csv.DictReader(handle))
    with (run_dir / "update_results.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        results = list(csv.DictReader(handle))
    with (run_dir / "post_validation.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        validation = {row["Id"]: row for row in csv.DictReader(handle)}
    combined = []
    messages = Counter()
    for planned, result in zip(plan, results):
        errors = json.loads(result.get("errors") or "[]")
        for error in errors:
            messages[str(error.get("message") or "Unknown Salesforce error")] += 1
        combined.append({
            **planned,
            "DML_Success": result.get("success"),
            "Post_Validation": validation.get(planned["Id"], {}).get("status", "UNKNOWN"),
            "Salesforce_Errors": " | ".join(str(error.get("message") or "") for error in errors),
        })
    headers = ["Id", "Legacy_Case_Id", FIELD, "Source_Installed_Base_Id", "MatchStatus",
               "DML_Success", "Post_Validation", "Salesforce_Errors"]
    success = [row for row in combined if str(row["DML_Success"]).lower() == "true"]
    failure = [row for row in combined if str(row["DML_Success"]).lower() != "true"]
    write_csv(run_dir / "case_installed_base_success.csv", success, headers)
    write_csv(run_dir / "case_installed_base_failure.csv", failure, headers)
    write_csv(run_dir / "failure_category_summary.csv",
              [{"Affected rows": count, "Salesforce error": message} for message, count in messages.most_common()],
              ["Affected rows", "Salesforce error"])
    print(json.dumps({"success_rows": len(success), "failure_rows": len(failure),
                      "distinct_error_messages": len(messages)}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "execute", "artifacts"])
    parser.add_argument("--input", default=r"C:\Users\puneetmalhotra\Downloads\Case_InstalledBase.csv")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--org", default="Waygate_UAT")
    parser.add_argument("--fallback-id", default="a5oS9000000EvPpIAK")
    arguments = parser.parse_args()
    {"prepare": prepare, "execute": execute, "artifacts": artifacts}[arguments.mode](arguments)
