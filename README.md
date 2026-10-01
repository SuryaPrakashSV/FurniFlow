python3 - <<'PY'
from pathlib import Path
from decimal import Decimal
import csv
import hashlib
import json

root = Path.home() / "Desktop/missing-projects"
evidence = root / "paired_token_runs_20260930/closeout_evidence_20261001T010531_871277Z"
manifest = json.loads((evidence / "evidence_manifest.json").read_text())
out = root / "FC.csv"

if out.exists():
    raise SystemExit(f"STOP: File already exists: {out}")

def verified_input(filename):
    matches = [
        Path(p) for p in manifest["inputs"]
        if Path(p).name == filename
    ]
    if len(matches) != 1:
        raise SystemExit(f"STOP: Expected one source for {filename}")
    p = matches[0]
    if hashlib.sha256(p.read_bytes()).hexdigest() != manifest["inputs"][str(p)]:
        raise SystemExit(f"STOP: Source changed: {p}")
    return p

def read(p):
    with p.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

csv.field_size_limit(100_000_000)
comparison = verified_input("container_differences.csv")
access_file = verified_input("project_access_review.csv")
access = {}
for r in read(access_file):
    if r["container_id"] in access:
        raise SystemExit("STOP: Duplicate access evidence")
    access[r["container_id"]] = r

fields = [
    "Project ID", "Project Name", "Effort Hours",
    "Effort Minutes", "Surya Export Rows", "Akash Export Rows",
    "Comparison Classification", "Project Type", "Project Type Evidence",
    "Akash Access Check Result", "Access Checked At",
    "Effort Basis", "Comparison Source", "Access Evidence Source"
]
rows = []
total_minutes = Decimal(0)
seen = set()

for r in read(comparison):
    a = access.get(r["container_id"], {})
    if r["classification"] != "SNAPSHOT_ONLY":
        continue
    if a.get("current_type", "").strip().lower() != "project":
        continue

    pid = r["container_id"]
    if pid in seen:
        raise SystemExit(f"STOP: Duplicate project ID: {pid}")
    seen.add(pid)

    minutes = Decimal(r["snapshot_numeric_minutes"])
    if int(r["akash_rows"]) != 0:
        raise SystemExit(f"STOP: Unexpected Akash rows for {pid}")
    if Decimal(r["delta_minutes"]) != minutes:
        raise SystemExit(f"STOP: Effort discrepancy for {pid}")

    total_minutes += minutes
    rows.append([
        pid,
        a.get("name") or r["snapshot_names"],
        format(minutes / 60, ".6f"),
        str(minutes),
        int(r["snapshot_rows"]),
        int(r["akash_rows"]),
        r["classification"],
        a["current_type"],
        a.get("type_evidence", ""),
        a.get("akash_access", ""),
        a.get("akash_observed_at", ""),
        "Surya raw rows grouped by direct container ID; repeats retained; minutes / 60",
        str(comparison),
        str(access_file)
    ])

if len(rows) != 56 or total_minutes != Decimal("104155128"):
    raise SystemExit(
        f"STOP: Unexpected result: {len(rows)} projects, "
        f"{total_minutes / 60:,.2f} hours"
    )

rows.sort(key=lambda r: r[1].casefold())
with out.open("x", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(fields)
    for row in rows:
        writer.writerow([
            "'" + v if isinstance(v, str)
            and v.startswith(("=", "+", "-", "@")) else v
            for v in row
        ])

print("CREATED:", out)
print("PROJECTS:", len(rows))
print("TOTAL RAW EFFORT HOURS:", f"{total_minutes / 60:,.2f}")
print("Earlier access results are evidence, not fresh permission checks.")
PY
