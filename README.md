python3 - <<'PY'
from pathlib import Path
import subprocess
import sys

root = Path.home() / "Desktop/missing-projects/paired_token_runs_20260930"
exports = {}

for person in ("surya", "akash"):
    files = list((root / person / "output/local_validation").glob("*/wrike_local_full.csv"))
    if len(files) != 1:
        raise SystemExit(f"STOP: Found {len(files)} exports for {person}; need exactly one.")
    exports[person] = files[0]

out = root / "surya_vs_akash_comparison"
if out.exists():
    raise SystemExit(f"STOP: Output already exists: {out}")

print("Reference = Surya; comparison = Akash", flush=True)
subprocess.run([
    sys.executable,
    str(root / "akash/compare_wrike_raw_rows.py"),
    "--snapshot", str(exports["surya"]),
    "--akash", str(exports["akash"]),
    "--output", str(out),
], check=True)
PY
