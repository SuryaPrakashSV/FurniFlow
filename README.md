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

columns = [
    "id", "parentfolderid", "childfolderid", "grandchildfolderid",
    "babyfolderid", "grandbabyfolderid",
    "parenttaskid", "childtaskid", "grandchildtaskid",
    "babytaskid", "grandbabytaskid", "greatgrandbabytaskid"
]

def load(path):
    counts = Counter()
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [
            "".join(c for c in h.lower() if c.isalnum())
            for h in reader.fieldnames
        ]
        for r in reader:
            key = (
                r["key"].strip(),
                Decimal(r["effortallocationtotaleffort"].strip()),
                tuple(r[c].strip() for c in columns)
            )
            counts[key] += 1
    return counts

print("Reading saved CSVs...", flush=True)
s = load(root/"snowflake_snapshot.csv")
a = load(root/"output/local_validation/20260930T213320_958948Z/wrike_local_full.csv")
groups = defaultdict(lambda: [0, 0, Decimal(0), Decimal(0), set()])
no_folders = []

for side, own, other in [(0, s, a), (1, a, s)]:
    other_efforts = {(k[0], k[1]) for k in other}
    for key, count in own.items():
        entity, effort, path = key
        if key in other or (entity, effort) not in other_efforts:
            continue
        folder_ids = {x for x in path[1:6] if x}
        if any(access.get(x, {}).get("akash_access") == "NOT_FOUND"
               for x in folder_ids):
            continue
        group = groups[path[0]]
        group[side] += count
        group[side+2] += effort * count
        group[4].add(entity)
        if not folder_ids:
            no_folders.append((side, entity, effort, count, path))

total = sum((g[2]-g[3] for g in groups.values()), Decimal(0))
assert total == Decimal("321480"), "Unexpected remaining total"

print("\nContainers represented:", len(groups))
for cid, g in sorted(
    groups.items(), key=lambda item: abs(item[1][2]-item[1][3]),
    reverse=True
)[:10]:
    name = access.get(cid, {}).get("name", "(no saved name)")
    print(cid, name, sep=" | ")
    print(f"  Rows SF/Akash: {g[0]}/{g[1]}; distinct entities: {len(g[4])}")
    print(f"  Raw effort SF/Akash: {g[2]}/{g[3]}; NET: {g[2]-g[3]}")

print("\nOccurrences without folder hierarchy IDs:")
for side, entity, effort, count, path in no_folders:
    print("Source:", "Snowflake" if side == 0 else "Akash")
    print("Entity:", entity, "| Effort:", effort, "| Occurrences:", count)
    print("Populated path fields:",
          {c: v for c, v in zip(columns, path) if v})

print("\nTotal remaining net raw effort:", total)
PY
