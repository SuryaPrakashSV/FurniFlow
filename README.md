python3 - <<'PY'
from pathlib import Path
from collections import defaultdict
from decimal import Decimal
import csv

csv.field_size_limit(100_000_000)
home = Path.home()
root = home / "Desktop/missing-projects/paired_token_runs_20260930"
pair = root / "surya_vs_akash_comparison"
access = home / "Desktop/wrike-local-baseline/output/project_access_today/20260930T161721_018375Z/project_access_review.csv"

def read(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

evidence = {}
for r in read(access):
    cid = r["container_id"]
    if cid in evidence:
        raise SystemExit("STOP: duplicate access evidence for " + cid)
    evidence[cid] = r

shared = {
    r["container_id"]
    for r in read(pair / "container_differences.csv")
    if r["classification"] == "PRESENT_BOTH_DIFFERENT_ROWS"
}

folder_fields = (
    "id", "parent_folder_id", "child_folder_id",
    "grandchild_folder_id", "baby_folder_id", "grandbaby_folder_id"
)
groups = defaultdict(lambda: Decimal(0))

for r in read(pair / "raw_row_differences.csv"):
    if r["container_id"] not in shared:
        continue
    matches = set()
    for field in folder_fields:
        cid = r.get(field, "")
        e = evidence.get(cid, {})
        if e.get("akash_access", "").strip().upper() == "NOT_FOUND":
            matches.add((cid, e.get("name", "")))
    label = "; ".join(
        f"{name} [{cid}]" for cid, name in sorted(matches)
    ) or "No earlier NOT_FOUND container matched this row's path"
    groups[label] += Decimal(r["delta_minutes"])

print("\nSHARED CONTAINERS: net differences by earlier access evidence")
for label, minutes in sorted(groups.items(), key=lambda x: -abs(x[1])):
    print(f"{minutes / 60:,.2f} hours | {label}")
print("Shared total hours:", f"{sum(groups.values(), Decimal(0)) / 60:,.2f}")

p = root / "surya/output/raw_token_comparison/20260930T224420_012710Z/comparison/task_differences.csv"
rows = [r for r in read(p) if Decimal(r["delta_minutes"]) != 0]
rows.sort(key=lambda r: -abs(Decimal(r["delta_minutes"])))

print("\nSNOWFLAKE MINUS SURYA: entities with nonzero effort differences")
print("Number of entities:", len(rows))
for r in rows[:20]:
    name = r.get("snapshot_names") or r.get("akash_names") or ""
    print(r["entity_key"], "|", r["classification"], "|",
          f'{Decimal(r["delta_minutes"]) / 60:,.2f} hours', "|", name)
print("Total hours:", f'{sum((Decimal(r["delta_minutes"]) for r in rows), Decimal(0)) / 60:,.2f}')
if len(rows) > 20:
    print("Showing only the largest 20; total includes all entities.")
PY
