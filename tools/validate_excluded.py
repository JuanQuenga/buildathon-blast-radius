"""Validate refinements offline: run, on the patched pin, exactly the tests a
cruder sound rule would have selected but impact.select excluded
(``Analysis.excluded``). Any failure is a would-be miss of the refinement.

    python3 tools/validate_excluded.py EXCLUDED.json CASEDIR --src PIN_COPY --python VENV_PY

EXCLUDED.json: {case_id: [node ids]} (see docs/impact.md).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("excluded")
    ap.add_argument("casedir")
    ap.add_argument("--src", required=True)
    ap.add_argument("--python", required=True)
    ap.add_argument("-n", default="6")
    a = ap.parse_args()
    ex = json.loads(Path(a.excluded).read_text())
    bad = {}
    for cid, ids in ex.items():
        patch = Path(a.casedir) / "commits" / f"{cid}.patch"
        with tempfile.TemporaryDirectory(prefix="brv-") as tmp:
            work = Path(tmp) / "w"
            shutil.copytree(a.src, work, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", ".git"))
            r = subprocess.run(["git", "apply", "--unsafe-paths", "-p1", str(patch.resolve())],
                               cwd=work, capture_output=True, text=True)
            if r.returncode:
                print(f"{cid}: apply failed {r.stderr[:200]}", flush=True)
                continue
            listf = Path(tmp) / "ids.txt"
            listf.write_text("\n".join(ids))
            junit = Path(tmp) / "j.xml"
            cmd = [a.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", "--timeout=300",
                   "--continue-on-collection-errors", f"--junitxml={junit}", "-n", a.n,
                   *ids]
            env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1",
                       PYTHONPATH=str(work))
            r = subprocess.run(cmd, cwd=work, capture_output=True, text=True, env=env)
            tail = r.stdout.strip().splitlines()[-1:] if r.stdout else []
            status = "OK" if r.returncode == 0 else f"rc={r.returncode}"
            if r.returncode not in (0, 5):
                fails = [l for l in r.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))]
                bad[cid] = fails[:20]
            print(f"{cid}: {len(ids)} excluded tests -> {status} {tail}", flush=True)
            for l in bad.get(cid, [])[:5]:
                print("    ", l[:200], flush=True)
    print(f"\n{len(ex)} cases, {len(bad)} with failures among excluded tests: {sorted(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
