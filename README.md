python3 - <<'PY'
from pathlib import Path
from collections import defaultdict
from decimal import Decimal
import csv

home = Path.home()
diff = home / "Desktop/missing-projects/paired_token_runs_20260930/surya_vs_akash_comparison/container_differences.csv"
access = home / "Desktop/wrike-local-baseline/output/project_access_today/20260930T161721_018375Z/project_access_review.csv"

with access.open(encoding="utf-8-sig", newline="") as f:
    evidence = {}
    for r in csv.DictReader(f):
        key = r["container_id"]
        if key in evidence:
            raise SystemExit(f"STOP: Duplicate access evidence for {key}")
        evidence[key] = r

groups = defaultdict(lambda: [0, Decimal(0)])
with diff.open(encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        if r["classification"] != "SNAPSHOT_ONLY":
            continue
        e = evidence.get(r["container_id"], {})
        label = (
            e.get("current_type") or "TYPE UNVERIFIED",
            e.get("akash_access") or "NO SAVED CHECK",
        )
        groups[label][0] += 1
        groups[label][1] += Decimal(r["delta_minutes"])

for (kind, status), (count, minutes) in sorted(groups.items()):
    print(f"{kind} | {status} | {count} containers | {minutes / 60:,.2f} hours")
PY
