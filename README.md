python3 - <<'PY'
import csv
from pathlib import Path
from collections import Counter
from decimal import Decimal

csv.field_size_limit(100000000)
root = Path.home() / "Desktop/missing-projects"

with (root / "output/raw_effort_review_20260930/container_differences.csv").open(
    encoding="utf-8-sig", newline=""
) as f:
    matches = [
        r for r in csv.DictReader(f)
        if r["snapshot_names"] == "FY26 Earth Day"
    ]
assert len(matches) == 1, "Expected one Earth Day container"
cid = matches[0]["container_id"]

def load(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]
        headers = reader.fieldnames
        rows = [
            r for r in reader
            if r["id"].strip() == cid
        ]
    return headers, rows

sh, s = load(root / "snowflake_snapshot.csv")
ah, a = load(
    root / "output/local_validation/20260930T213320_958948Z/wrike_local_full.csv"
)

def value(row, column):
    text = (row[column] or "").strip()
    if column == "effortallocation_totaleffort":
        return str(Decimal(text).normalize())
    return text

def counts(rows, columns):
    return Counter(
        tuple(value(r, c) for c in columns)
        for r in rows
    )

def doubled(sc, ac):
    return bool(ac) and sc.keys() == ac.keys() and all(
        sc[k] == 2 * ac[k] for k in ac
    )

excluded = {"data_refresh", "snapshot_exported_at"}
common = sorted((set(sh) & set(ah)) - excluded)
base = ["key", "effortallocation_totaleffort"]

print("Container:", cid)
print("Snowflake-only columns:", sorted(set(sh) - set(ah)))
print("Akash-only columns:", sorted(set(ah) - set(sh)))
print("Excluded timestamp columns:", sorted(excluded))
print("Full common rows occur exactly twice:",
      doubled(counts(s, common), counts(a, common)))

different = []
for column in common:
    columns = list(dict.fromkeys(base + [column]))
    if not doubled(counts(s, columns), counts(a, columns)):
        different.append(column)

print("Fields that do NOT follow the exact doubling pattern:")
for column in different:
    print(" -", column)
print("Number of such fields:", len(different))
PY
