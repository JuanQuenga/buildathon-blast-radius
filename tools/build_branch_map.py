"""Record observed branch outcomes, one complete heavy test per process tree."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
pin = Path(sys.argv[1]).resolve()
assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=pin, text=True).strip() == "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=pin)
tests = ["tests/test_executor.py::TestExecutor::test_execute_tpcds",
         "tests/test_parser.py::TestParser::test_max_nodes"]
profiles = {}
for test in tests:
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, BR_BRANCH_OUT=tmp, PYTHON_CPU_COUNT="1", SKIP_INTEGRATION="0",
                   PYTHONDONTWRITEBYTECODE="1", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                   PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"],
                   PYTHONPATH=os.pathsep.join(map(str, [root / "tools/branch_trace", pin])))
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test],
                       cwd=pin, env=env, check=True)
        profiles[test] = {p.stem: int(p.suffix[1:]) for p in sorted(Path(tmp).iterdir())
                          if not p.with_suffix(".1" if p.suffix == ".0" else ".0").exists()}
(root / "selector/maps/branches.json").write_text(json.dumps(profiles, sort_keys=True))
