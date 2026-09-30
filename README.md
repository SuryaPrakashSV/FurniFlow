python3 - <<'PY'
from pathlib import Path
import csv

folder = Path.home() / "Desktop/missing-projects/output/raw_effort_review_20260930"
for name in ("container_differences.csv", "task_differences.csv", "raw_row_differences.csv"):
    path = folder / name
    print(f"\n{name}")
    if not path.is_file():
        print("FILE NOT FOUND:", path)
        continue
    with path.open(encoding="utf-8-sig", newline="") as f:
        print(next(csv.reader(f), []))
PY
