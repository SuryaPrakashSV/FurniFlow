python3 - <<'PY'
from pathlib import Path
from collections import defaultdict
from decimal import Decimal
import csv

p = Path.home() / "Desktop/missing-projects/paired_token_runs_20260930/closeout_evidence_20261001T010531_871277Z/remaining_path_rows.csv"
csv.field_size_limit(100_000_000)

fields = (
    "parent_folder_id", "child_folder_id", "grandchild_folder_id",
    "baby_folder_id", "grandbaby_folder_id"
)
groups = defaultdict(lambda: Decimal(0))

with p.open(encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        path = tuple(r.get(k, "") for k in fields)
        key = (r["container_id"], r["container_name"], path)
        groups[key] += Decimal(r["delta_minutes"])

for (cid, name, path), minutes in sorted(
    groups.items(), key=lambda x: -abs(x[1])
):
    if minutes:
        print(f"\n{name} | {cid} | {minutes / 60:,.2f} hours")
        for field, value in zip(fields, path):
            if value:
                print(f"  {field}: {value}")

print("\nTOTAL:", f"{sum(groups.values(), Decimal(0)) / 60:,.2f}", "hours")
PY
