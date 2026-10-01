python3 - <<'PY'
from pathlib import Path
from collections import defaultdict
from decimal import Decimal
import csv

p = Path.home() / "Desktop/missing-projects/paired_token_runs_20260930/surya_vs_akash_comparison/container_differences.csv"
groups = defaultdict(lambda: [0, Decimal(0)])

with p.open(encoding="utf-8-sig", newline="") as f:
    for row in csv.DictReader(f):
        group = groups[row["classification"]]
        group[0] += 1
        group[1] += Decimal(row["delta_minutes"])

for label, (count, minutes) in sorted(groups.items()):
    label = label.replace("SNAPSHOT", "SURYA")
    print(f"{label}: {count} containers | difference: {minutes / 60:,.2f} hours")

total = sum((v[1] for v in groups.values()), Decimal(0))
print(f"TOTAL DIFFERENCE: {total / 60:,.2f} hours")
PY
