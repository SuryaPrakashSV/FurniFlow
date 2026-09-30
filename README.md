python3 - <<'PY'
import csv
from pathlib import Path
from collections import Counter, defaultdict
from decimal import Decimal

csv.field_size_limit(100000000)
desktop = Path.home() / "Desktop"
root = desktop / "missing-projects"

reviews = sorted({
    p for folder in [desktop/"wrike-local-baseline", root]
    for p in folder.rglob("project_access_review.csv")
})
assert len(reviews) == 1, "Expected one access-review file"

with reviews[0].open(encoding="utf-8-sig", newline="") as f:
    access = {}
    for r in csv.DictReader(f):
        cid = r["container_id"].strip()
        assert cid not in access, "Duplicate access-review ID"
        access[cid] = r

folders = [
    "parentfolderid", "childfolderid", "grandchildfolderid",
    "babyfolderid", "grandbabyfolderid"
]
tasks = [
    "parenttaskid", "childtaskid", "grandchildtaskid",
    "babytaskid", "grandbabytaskid", "greatgrandbabytaskid"
]
path_columns = ["id"] + folders + tasks

def load(path):
    counts = Counter()
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [
            "".join(c for c in h.lower() if c.isalnum())
            for h in reader.fieldnames
        ]
        required = set(path_columns + ["key", "effortallocationtotaleffort"])
        assert required <= set(reader.fieldnames), "Required columns missing"
        for r in reader:
            entity = r["key"].strip()
            effort = Decimal(r["effortallocationtotaleffort"].strip())
            hierarchy = tuple(r[c].strip() for c in path_columns)
            counts[(entity, effort, hierarchy)] += 1
    return counts

print("Reading saved CSVs...", flush=True)
s = load(root/"snowflake_snapshot.csv")
a = load(root/"output/local_validation/20260930T213320_958948Z/wrike_local_full.csv")

groups = defaultdict(lambda: [0, 0, Decimal(0), Decimal(0)])

for side, own, other in [(0, s, a), (1, a, s)]:
    shared_effort = {(k[0], k[1]) for k in other}
    for key, count in own.items():
        entity, effort, hierarchy = key
        if key in other or (entity, effort) not in shared_effort:
            continue

        # Only folder hierarchy columns; task IDs are not folder access checks.
        folder_ids = {x for x in hierarchy[1:6] if x}
        unavailable = tuple(sorted(
            x for x in folder_ids
            if x in access and access[x]["akash_access"] == "NOT_FOUND"
        ))
        if unavailable:
            label = "NOT_FOUND folder IDs: " + ", ".join(unavailable)
        elif any(x not in access for x in folder_ids):
            label = "No NOT_FOUND match; some folder IDs lack saved checks"
        elif folder_ids:
            label = "No NOT_FOUND match; all folder IDs have saved checks"
        else:
            label = "No folder hierarchy IDs"

        group = groups[label]
        group[side] += count
        group[side+2] += effort * count

net_total = Decimal(0)
print("\nMISSING HIERARCHY PATHS — SAVED FOLDER EVIDENCE")
for label, (sr, ar, se, ae) in sorted(
    groups.items(), key=lambda item: abs(item[1][2]-item[1][3]), reverse=True
):
    net_total += se-ae
    print(label)
    print(f"  Rows SF/Akash: {sr}/{ar}; NET raw effort: {se-ae}")

print("\nHierarchy net total:", net_total)
assert net_total == Decimal("85522514"), "Unexpected hierarchy total"
PY
