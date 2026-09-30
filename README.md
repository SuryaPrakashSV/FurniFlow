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
    cid for cid, r in access.items()
    if r["name"].strip() in {"BAU", "B518 Momentum", "B487DC26"}
    and r["akash_access"] == "RETRIEVED"
}
assert len(targets) == 3, "Expected three target containers"

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
            counts[(
                r["key"].strip(),
                Decimal(r["effortallocationtotaleffort"].strip()),
                tuple(r[c].strip() for c in columns)
            )] += 1
    return counts

print("Reading saved CSVs...", flush=True)
s = load(root/"snowflake_snapshot.csv")
a = load(root/"output/local_validation/20260930T213320_958948Z/wrike_local_full.csv")

selected = set()
for own, other in [(s, a), (a, s)]:
    other_efforts = {(k[0], k[1]) for k in other}
    for entity, effort, path in own:
        key = (entity, effort, path)
        if path[0] in targets and key not in other:
            if (entity, effort) in other_efforts:
                selected.add((entity, effort))

locations = [defaultdict(set), defaultdict(set)]
for side, counts in enumerate([s, a]):
    for entity, effort, path in counts:
        if (entity, effort) in selected:
            locations[side][(entity, effort)].add(path[0])

groups = defaultdict(list)
for entity_effort in selected:
    pair = (
        tuple(sorted(locations[0][entity_effort])),
        tuple(sorted(locations[1][entity_effort]))
    )
    groups[pair].append(entity_effort)

def describe(ids):
    return "; ".join(
        (cid or "(blank ID)") + " [" +
        access.get(cid, {}).get("name", "no saved name") + "]"
        for cid in ids
    )

print("\nSelected entity-and-effort combinations:", len(selected))
print("Container patterns:", len(groups))
for (sf_ids, ak_ids), entities in sorted(
    groups.items(), key=lambda item: len(item[1]), reverse=True
)[:12]:
    print("\nCombinations:", len(entities))
    print("Snowflake:", describe(sf_ids))
    print("Akash:    ", describe(ak_ids))
    print("Example entity IDs:",
          ", ".join(sorted({e[0] for e in entities})[:3]))
PY
