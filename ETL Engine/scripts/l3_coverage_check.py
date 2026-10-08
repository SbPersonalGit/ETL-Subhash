import json
import re
from collections import Counter
from pathlib import Path


def main() -> int:
    template_path = Path("config/templates/Itinerary-_Child.json")
    log_path = Path("logs/ui_pipeline_run.log")

    tpl = json.loads(template_path.read_text(encoding="utf-8"))
    nodes = (tpl.get("template") or {}).get("nodes") or []

    l3_tpl = {n.get("id"): n for n in nodes if n.get("depth") == 3 and n.get("id")}

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    seen = {
        m.group(2): m.group(1)
        for m in re.finditer(r"\[([^\]]+)\]\[([^\]]+)\]\[L3\]", log_text)
    }

    covered = [nid for nid in l3_tpl if nid in seen]
    pending = [nid for nid in l3_tpl if nid not in seen]

    print(f"TEMPLATE_L3={len(l3_tpl)}")
    print(f"LOG_L3_SEEN={len(seen)}")
    print(f"COVERED={len(covered)}")
    print(f"PENDING={len(pending)}")

    print("\nPENDING_SAMPLE (up to 25):")
    for nid in pending[:25]:
        n = l3_tpl[nid]
        print(
            f"- {nid}: object={n.get('object')} parent={n.get('parentId')} via={n.get('via_field')}"
        )

    print("\nCOVERED_BY_OBJECT_TOP (top 20):")
    for obj, cnt in Counter(l3_tpl[nid].get("object") for nid in covered).most_common(20):
        print(f"- {obj}: {cnt}")

    print("\nPENDING_BY_OBJECT_TOP (top 20):")
    for obj, cnt in Counter(l3_tpl[nid].get("object") for nid in pending).most_common(20):
        print(f"- {obj}: {cnt}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

