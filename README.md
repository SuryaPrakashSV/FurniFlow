python3 - <<'PY'
import csv
from pathlib import Path
from collections import Counter

csv.field_size_limit(100000000)
root = Path.home() / "Desktop/missing-projects"

with (root / "output/raw_effort_review_20260930/container_differences.csv").open(
    encoding="utf-8-sig", newline=""
) as f:
    matches = [r for r in csv.DictReader(f)
               if r["snapshot_names"] == "FY26 Earth Day"]
assert len(matches) == 1, "Expected one Earth Day container"
cid = matches[0]["container_id"]

files = {
    "Snowflake": root / "snowflake_snapshot.csv",
    "Akash": root / "output/local_validation/20260930T213320_958948Z/wrike_local_full.csv"
}

for label, path in files.items():
    counts = Counter()
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [
            "".join(c for c in h.lower() if c.isalnum())
            for h in reader.fieldnames
        ]
        required = {"id", "parentfolderid", "parentfoldertitle"}
        assert required <= set(reader.fieldnames), "Missing required columns"
        for r in reader:
            if r["id"].strip() == cid:
                counts[(r["parentfolderid"], r["parentfoldertitle"])] += 1
    print("\n" + label + " — parent folders:")
    for (folder_id, title), count in counts.most_common():
        print(folder_id, title, "Rows:", count, sep=" | ")
PY
