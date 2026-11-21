"""Run inside the selector image with tools mounted and stdout redirected to the map."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from selector import impact, token_options

an = impact.Analysis()
bits = 0
for path, line in token_options.discover_sites("NUMERIC_ESCAPES"):
    bits |= an.lines_bits(path, line, line)
result = {}
for i, test in enumerate(an.idx["tests"]):
    if not bits & (1 << i):
        continue
    with tempfile.TemporaryDirectory() as work:
        env = dict(os.environ, BR_TOKEN_TRACE=work, PYTHON_CPU_COUNT="1",
                   PYTHONPATH=str(Path(__file__).parent / "token_trace") + ":/app/sqlglot")
        run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test],
                             cwd="/app/sqlglot", env=env, capture_output=True, timeout=120)
        files = sorted(Path(work).glob("*.json"))
        if run.returncode or {p.stem for p in files} != {p.stem for p in Path(work).glob("*.start")}:
            print("incomplete:", test, file=sys.stderr)
            continue
        observations = set()
        installed = set()
        for p in files:
            modules, hits = json.loads(p.read_text())
            installed.update(modules)
            observations.update(map(tuple, hits))
        if len(installed) == 2 and observations:
            result[test] = sorted(observations)
print(json.dumps(result, sort_keys=True))
