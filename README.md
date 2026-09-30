python3 - <<'PY'
from pathlib import Path
import hashlib

root = Path.home() / "Desktop/missing-projects/paired_token_runs_20260930"

def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

for name in (
    "Wrike_Data.py",
    "Wrike_Data_local_validation.py",
    "compare_wrike_raw_rows.py",
    "snowflake_snapshot.csv",
):
    a, b = root / "surya" / name, root / "akash" / name
    if not a.is_file() or not b.is_file():
        print(name, ": FILE MISSING")
    else:
        print(name, ": MATCH" if sha256(a) == sha256(b) else ": DIFFERENT")
PY
