"""Run the traced test suite of a pinned sqlglot checkout in parallel chunks.

    python tools/run_trace.py --src CHECKOUT --python VENV/bin/python \
        --catalog tests.json --out TRACE_DIR [--jobs 12]

Each chunk is one pytest process (one test file, <= --chunk node ids) loading
tools/trace_plugin.py; each test is forked so results are order independent.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--python", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=12)
    ap.add_argument("--chunk", type=int, default=25)
    ap.add_argument("--only", default="")
    a = ap.parse_args()

    cat = json.loads(Path(a.catalog).read_text())
    ids = [t["id"] for t in cat["tests"]] + list(cat.get("skipped", []))
    if a.only:
        ids = [i for i in ids if a.only in i]
    by_file: dict[str, list[str]] = {}
    for i in ids:
        by_file.setdefault(i.split("::")[0], []).append(i)
    secs = {t["id"]: t["seconds"] for t in cat["tests"]}
    chunks = []
    for f, lst in by_file.items():
        for k in range(0, len(lst), a.chunk):
            part = lst[k:k + a.chunk]
            chunks.append((sum(secs.get(i, 0) for i in part), part))
    chunks.sort(key=lambda c: -c[0])
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    logs = out / "_logs"
    logs.mkdir(exist_ok=True)
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1",
               TRACE_OUT=str(out), TRACE_ROOT=str(Path(a.src).resolve()),
               PYTHONPATH=str(HERE))

    def run(c):
        n, (_, part) = c
        t0 = time.time()
        with open(logs / f"chunk{n:04d}.log", "w") as lf:
            r = subprocess.run([a.python, "-m", "pytest", "-q", "-p", "trace_plugin",
                                "-p", "no:cacheprovider", "-p", "no:randomly", *part],
                               cwd=a.src, env=env, stdout=lf, stderr=subprocess.STDOUT)
        return n, r.returncode, time.time() - t0, part[0]

    t0 = time.time()
    with ThreadPoolExecutor(a.jobs) as ex:
        for n, rc, dt, first in ex.map(run, enumerate(chunks)):
            print(f"chunk {n} rc={rc} {dt:.1f}s {first}", file=sys.stderr, flush=True)
    print(f"done {len(chunks)} chunks in {time.time() - t0:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
