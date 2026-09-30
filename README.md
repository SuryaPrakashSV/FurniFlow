python3 - <<'PY'
import csv
from pathlib import Path
from collections import Counter
from decimal import Decimal

csv.field_size_limit(100000000)
root = Path.home() / "Desktop/missing-projects"
review = root / "output/raw_effort_review_20260930/container_differences.csv"
with review.open(encoding="utf-8-sig", newline="") as f:
    matches = [r for r in csv.DictReader(f) if r["snapshot_names"] == "FY26 Earth Day"]
assert len(matches) == 1, "Expected exactly one Earth Day container"
cid = matches[0]["container_id"]

def read_counts(path):
    counts = Counter()
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]
        for r in reader:
            if r["id"].strip() == cid:
                counts[(r["key"].strip(), Decimal(r["effortallocation_totaleffort"]))] += 1
    return counts

s = read_counts(root / "snowflake_snapshot.csv")
a = read_counts(root / "output/local_validation/20260930T213320_958948Z/wrike_local_full.csv")
print("Container:", cid)
print("Rows, Snowflake / Akash:", sum(s.values()), "/", sum(a.values()))
print("Distinct entity IDs, Snowflake / Akash:", len({k[0] for k in s}), "/", len({k[0] for k in a}))
print("Same entity-and-effort combinations:", s.keys() == a.keys())
print("Every combination occurs exactly twice as often in Snowflake:", bool(a) and s.keys() == a.keys() and all(s[k] == 2*a[k] for k in a))
PY
