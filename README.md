python3 - <<'PY'
import csv
from pathlib import Path
from collections import Counter, defaultdict
from decimal import Decimal

root = Path.home() / "Desktop"
files = sorted({
    p
    for folder in [root/"wrike-local-baseline", root/"missing-projects"]
    for p in folder.rglob("project_access_review.csv")
})
assert len(files) == 1, "Expected exactly one access-review file"

def read(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        return list(reader)

access_rows = read(files[0])
access = {}
for r in access_rows:
    cid = r["container_id"].strip()
    assert cid not in access, "Duplicate access-review ID: " + cid
    access[cid] = r

diffs = read(
    root/"missing-projects/output/raw_effort_review_20260930/container_differences.csv"
)
groups = defaultdict(lambda: [0, Decimal(0)])
for r in diffs:
    evidence = access.get(r["container_id"].strip())
    status = evidence["akash_access"] if evidence else "NO_SAVED_CHECK"
    group = groups[(r["classification"], status)]
    group[0] += 1
    group[1] += Decimal(r["delta_minutes"])

print("DIFFERENCES MATCHED TO SAVED ACCESS RESULTS")
for (classification, status), (count, effort) in sorted(groups.items()):
    print(classification, status, "Containers:", count,
          "Net raw effort:", effort, sep=" | ")

print("\nTOTAL NET RAW EFFORT:",
      sum((v[1] for v in groups.values()), Decimal(0)))

print("\nACCESS OBSERVATION RANGE:")
times = sorted(r["akash_observed_at"] for r in access_rows
               if r["akash_observed_at"])
print(times[0] if times else "Unknown",
      "to", times[-1] if times else "Unknown")

print("\nSAVED BAU EVIDENCE:")
bau = [r for r in access_rows if r["name"].strip().casefold() == "bau"]
if not bau:
    print("No exact BAU name found in saved access review")
for r in bau:
    for field in ["container_id", "name", "akash_access",
                  "akash_http_status", "akash_observed_at",
                  "akash_scope", "akash_evidence_method"]:
        print(field + ":", r.get(field, "COLUMN ABSENT"))
PY
