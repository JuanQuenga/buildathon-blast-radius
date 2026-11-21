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
operands = {}
for test in tests:
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, BR_BRANCH_OUT=tmp, PYTHON_CPU_COUNT="1", SKIP_INTEGRATION="0",
                   PYTHONDONTWRITEBYTECODE="1", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                   PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"],
                   PYTHONPATH=os.pathsep.join(map(str, [root / "tools/branch_trace", pin])))
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test],
                       cwd=pin, env=env, check=True)
        profiles[test] = {p.stem: int(p.suffix[1:]) for p in sorted(Path(tmp).iterdir())
                          if p.suffix in (".0", ".1") and not p.with_suffix(".1" if p.suffix == ".0" else ".0").exists()}
        started = {f.name.split("-")[1] for f in Path(tmp).glob("started-*")}
        saved = {f.stem.split("-")[1] for f in Path(tmp).glob("operands-*.json")}
        if not started or started != saved:
            raise RuntimeError(f"{test}: incomplete process profiles {started ^ saved}")
        joined = {}
        for file in Path(tmp).glob("operands-*.json"):
            for site, items in json.loads(file.read_text()).items():
                previous = joined.setdefault(site, set())
                if previous is None or items is None:
                    joined[site] = None
                else:
                    previous.update(tuple(pair) for pair in items)
                    if len(previous) > 64:
                        joined[site] = None
        operands[test] = {site: [value for _, value in sorted(items)]
                          for site, items in joined.items() if items}
(root / "selector/maps/operands.json").write_text(json.dumps(operands, sort_keys=True))
(root / "selector/maps/branches.json").write_text(json.dumps(profiles, sort_keys=True))
