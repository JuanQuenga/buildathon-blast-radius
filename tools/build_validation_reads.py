"""Collect value-sensitive validation and parser dispatch reads on the SQLGlot pin."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ, BR_VALIDATION=tmp, PYTHON_CPU_COUNT="1",
               PYTHONPATH=str(Path(__file__).parent / "validation_trace") + ":/app/sqlglot")
    root = Path("/app/sqlglot/tests")
    for path in sorted(root.iterdir()):
        if path.name.startswith("test_") and path.suffix == ".py" or path.is_dir() and any(path.rglob("test_*.py")):
            proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(path)],
                           cwd=root.parent, env=env, stdout=sys.stderr)
            if proc.returncode not in (0, 5):
                proc.check_returncode()
    files = list(Path(tmp).glob("*.json"))
    assert files and {p.stem for p in files} == {p.stem for p in Path(tmp).glob("*.start")}
    installed, show, required = set(), set(), {}
    for f in files:
        hooks, keys, classes = json.loads(f.read_text())
        installed.update(hooks)
        show.update(keys)
        for cls, fields in classes.items():
            required.setdefault(cls, set()).update(fields)
    assert installed == {"error_messages", "_find_parser"} and show and required
    print(json.dumps([sorted(show), {k: sorted(v) for k, v in sorted(required.items())}]))
