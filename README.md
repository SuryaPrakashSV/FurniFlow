python3 - <<'PY'
import csv
from pathlib import Path
from collections import Counter, defaultdict
from decimal import Decimal, getcontext

getcontext().prec = 60
csv.field_size_limit(100000000)
root = Path.home() / "Desktop/missing-projects"

path_columns = [
    "id",
    "parentfolderid", "childfolderid", "grandchildfolderid",
    "babyfolderid", "grandbabyfolderid",
    "parenttaskid", "childtaskid", "grandchildtaskid",
    "babytaskid", "grandbabytaskid", "greatgrandbabytaskid"
]

def load(path):
    counts = Counter()
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = [
            "".join(c for c in h.lower() if c.isalnum())
            for h in reader.fieldnames
        ]
        assert len(headers) == len(set(headers)), "Ambiguous column names"
        reader.fieldnames = headers
        required = set(path_columns + ["key", "effortallocationtotaleffort"])
        assert required <= set(headers), f"Missing columns: {required-set(headers)}"

        for line, r in enumerate(reader, 2):
            assert None not in r and all(v is not None for v in r.values()), (
                f"Malformed CSV record near line {line}"
            )
            entity = r["key"].strip()
            effort = Decimal(r["effortallocationtotaleffort"].strip())
            assert effort.is_finite(), f"Invalid effort near line {line}"
            hierarchy = tuple(r[c].strip() for c in path_columns)
            counts[(entity, effort, hierarchy)] += 1
    return counts

print("Reading Snowflake...", flush=True)
s = load(root / "snowflake_snapshot.csv")
print("Reading Akash export...", flush=True)
a = load(root / "output/local_validation/20260930T213320_958948Z/wrike_local_full.csv")

s_total = sum((k[1]*n for k, n in s.items()), Decimal(0))
a_total = sum((k[1]*n for k, n in a.items()), Decimal(0))
assert (sum(s.values()), s_total) == (213654, Decimal("1802389425")), (
    "Snowflake input differs from the reviewed baseline"
)
assert (sum(a.values()), a_total) == (170774, Decimal("1621073293")), (
    "Akash input differs from the reviewed baseline"
)

labels = [
    "Missing entity key",
    "Entity ID absent from other export",
    "Entity present, effort value absent from other export",
    "Same entity and effort, hierarchy path absent",
    "Same entity, effort and path, occurrence count differs"
]
totals = defaultdict(lambda: [0, 0, Decimal(0), Decimal(0)])

def classify(own, other, side):
    other_ids = {k[0] for k in other}
    other_efforts = {(k[0], k[1]) for k in other}
    for key, count in own.items():
        extra = count - other.get(key, 0)
        if extra <= 0:
            continue
        entity, effort, hierarchy = key
        if not entity:
            category = labels[0]
        elif entity not in other_ids:
            category = labels[1]
        elif (entity, effort) not in other_efforts:
            category = labels[2]
        elif key not in other:
            category = labels[3]
        else:
            category = labels[4]
        totals[category][side] += extra
        totals[category][side + 2] += effort * extra

classify(s, a, 0)
classify(a, s, 1)

print("\nCounts below are unmatched ROW OCCURRENCES, not unique tasks.")
bridge = Decimal(0)
for label in labels:
    sr, ar, se, ae = totals[label]
    net = se - ae
    bridge += net
    print(label)
    print(f"  Rows SF/Akash: {sr}/{ar}; raw effort SF/Akash: {se}/{ae}; NET: {net}")

difference = s_total - a_total
print("\nRaw total difference:", difference)
print("Sum of category differences:", bridge)
print("Residual:", difference - bridge)
assert bridge == difference, "Category totals do not reconcile"
PY
