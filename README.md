python3 - <<'PY'
import ast
from pathlib import Path

p = Path.home() / "Desktop/missing-projects/Wrike_Data.py"
tree = ast.parse(p.read_text(encoding="utf-8-sig"))
functions = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
results = set()

for node in ast.walk(tree):
    reason = None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node.value.lower() in {
            "effortallocation", "totaleffort",
            "effortallocation_totaleffort"
        }:
            reason = "effort field"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "items":
            reason = "dictionary processing"
    if reason:
        owners = [f for f in functions
                  if f.lineno <= node.lineno <= f.end_lineno]
        owner = min(owners, key=lambda f: f.end_lineno-f.lineno,
                    default=None)
        name = owner.name if owner else "top-level code"
        results.add((node.lineno, name, reason))

for line, name, reason in sorted(results):
    print(f"Line {line}: {name} — {reason}")
PY
