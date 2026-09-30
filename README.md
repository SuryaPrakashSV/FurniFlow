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
    access = {r["container_id"].strip(): r for r in csv.DictReader(f)}

targets = {
    cid: r["name"] for cid, r in access.items()
    if r["name"].strip() in {"BAU", "B518 Momentum", "B487DC26"}
    and r["akash_access"] == "RETRIEVED"
}
assert len(targets) == 3, "Expected three target containers"

folders = [
    "parentfolderid", "childfolderid", "grandchildfolderid",
    "babyfolderid", "grandbabyfolderid"
]
tasks = [
    "parenttaskid", "childtaskid", "grandchildtaskid",
    "babytaskid", "grandbabytaskid", "greatgrandbabytaskid"
]
columns = ["id"] + folders + tasks

def load(path):
    counts = Counter()
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [
            "".join(c for c in h.lower() if c.isalnum())
            for h in reader.fieldnames
        ]
        for r in reader:
            counts[(
                r["key"].strip(),
                Decimal(r["effortallocationtotaleffort"].strip()),
                tuple(r[c].strip() for c in columns)
            )] += 1
    return counts

print("Reading earlier completed exports...", flush=True)
s = load(root/"snowflake_snapshot.csv")
a = load(root/"output/local_validation/20260930T213320_958948Z/wrike_local_full.csv")

for cid, name in sorted(targets.items()):
    print("\nCONTAINER:", name, cid)
    for label, own, other in [("Snowflake", s, a), ("Akash", a, s)]:
        other_efforts = {(k[0], k[1]) for k in other}
        groups = defaultdict(lambda: [0, Decimal(0), set()])
        for key, count in own.items():
            entity, effort, path = key
            if path[0] != cid or key in other:
                continue
            if (entity, effort) not in other_efforts:
                continue
            levels = tuple(
                field + ("=ENTITY" if value == entity else "=ANCESTOR")
                for field, value in zip(tasks, path[6:]) if value
            )
            group = groups[(path[1:6], levels)]
            group[0] += count
            group[1] += effort * count
            group[2].add(entity)

        print(label, "unmatched hierarchy patterns:", len(groups))
        for (folder_path, levels), (count, effort, entities) in sorted(
            groups.items(), key=lambda item: abs(item[1][1]), reverse=True
        )[:5]:
            print("  Rows:", count, "| Raw effort:", effort)
            print("  Folders:", "; ".join(
                field + "=" + value + " [" +
                access.get(value, {}).get("name", "no saved name") + "]"
                for field, value in zip(folders, folder_path) if value
            ) or "(none)")
            print("  Task levels:", ", ".join(levels) or "(none)")
            print("  Example entity:", sorted(entities)[0])
PY
