"""Record identity/equality agreement from a clean pinned pytest run."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
checkout = Path(sys.argv[1]).resolve()
assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip() == PIN
assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout)
with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ, BR_IDENTITY_OUT=tmp, SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="1",
               OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join(map(str, [ROOT / "tools/identity_trace", checkout])),
               PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"])
    subprocess.run([sys.executable, "-m", "pytest", "tests", "-p", "no:cacheprovider", "-q"],
                   cwd=checkout, env=env, check=True)
    started = {p.stem for p in Path(tmp).glob("*.start")}
    reports = list(Path(tmp).glob("*.json"))
    if not started or started != {p.stem for p in reports}:
        raise RuntimeError("incomplete identity recording")
    agreed, unknown = set(), set()
    for report in reports:
        for site, safe in json.loads(report.read_text()):
            (agreed if safe else unknown).add(site)
    sites = sorted(agreed - unknown)
    (ROOT / "selector/maps/identity.json").write_text(json.dumps(sites, indent=2) + "\n")
