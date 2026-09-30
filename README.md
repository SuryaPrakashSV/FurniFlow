cd "$HOME/Desktop/missing-projects"

python3 - <<'PY'
import csv
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

csv.field_size_limit(sys.maxsize)

path = Path("output/local_validation/20260930T213320_958948Z/wrike_local_full.csv")

with path.open(encoding="utf-8-sig", newline="") as f:
    reader = csv.reader(f)
    headers = next(reader)

    matches = [
        i for i, h in enumerate(headers)
        if h.strip().lower() == "effortallocation_totaleffort"
    ]

    if len(matches) != 1:
        raise SystemExit(
            f"Expected one effortAllocation_totalEffort column, found {len(matches)}"
        )

    index = matches[0]

    total = Decimal(0)
    rows = 0
    blanks = 0
    invalid = 0

    for row in reader:
        rows += 1

        value = row[index].strip()

        if value.lower() in {"", "null", "none", "nan", "<na>"}:
            blanks += 1
            continue

        try:
            total += Decimal(value)
        except InvalidOperation:
            invalid += 1

print("Column:", headers[index])
print("Column position:", index + 1)
print("Data rows:", rows)
print("Raw numeric sum, INCLUDING repeated rows:", total)
print("Blank/null values:", blanks)
print("Invalid values:", invalid)
PY
