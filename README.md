python3 - <<'PY'
from pathlib import Path
import shutil

root = Path.home() / "Desktop/missing-projects"
names = [
    "Wrike_Data.py",
    "Wrike_Data_local_validation.py",
    "run_akash_raw_comparison.py",
    "compare_wrike_raw_rows.py",
    "snowflake_snapshot.csv"
]
for name in names:
    assert (root/name).is_file(), "Missing file: " + str(root/name)

destination = root / "paired_token_runs_20260930"
destination.mkdir(exist_ok=False)

for label in ["surya", "akash"]:
    folder = destination / label
    folder.mkdir()
    for name in names:
        shutil.copy2(root/name, folder/name)
    print("Prepared:", folder)
PY
