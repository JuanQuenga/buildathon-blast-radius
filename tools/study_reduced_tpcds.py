"""Run the reduced-data experiment in a pinned selector container.

Pass the optional reference produced by record_executor_inputs.py and a
directory containing patches alone. Emit verdicts for host-side comparison
with independent labels. These results never change a worker plan.
"""
import argparse
import gzip
import json
import os
from pathlib import Path
import pickle
import resource
import subprocess
import sys
import tempfile


def worker(reference):
    sys.path[:0] = [os.getcwd(), "/app/selector"]
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute
    from tests.helpers import TPCDS_SCHEMA
    from result_replay import table_digest
    from staged import digest

    with gzip.open(reference, "rb") as stream:
        small, reduced = pickle.load(stream)
    queries = pickle.loads(Path("/app/pin-cache/query-fixtures.pkl").read_bytes())
    pinned = json.loads(Path("/app/staged.json").read_text())["ds"]
    indices = {i for i, (meta, _, _) in enumerate(queries) if meta.get("execute")}
    if len(indices) != 57 or set(reduced) != indices:
        raise ValueError("incomplete pin recording")
    for i, (meta, sql, _) in enumerate(queries):
        expr = pickle.loads(pickle.dumps(parse_one(sql)))
        if not meta.get("execute"):
            continue
        if digest(transpile(sql, write="duckdb")) != pinned[i][4]:
            return 4
        tables = {t.name: small[t.name] for t in find_tables(expr)}
        result = execute(expr, schema=TPCDS_SCHEMA, tables=tables)
        if table_digest(pickle.loads(pickle.dumps(result))) != reduced[i]:
            return 3
    return 0


def limits():
    resource.setrlimit(resource.RLIMIT_AS, (1600 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (14, 15))


def replay(path, reference):
    sys.path.insert(0, "/app")
    from selector import heavy, verify
    from selector.patch import parse

    patch = path.read_text()
    changes = verify.apply_patch(patch)
    if not changes or not all(heavy.eligible_file(fd, "ds") for fd in parse(patch)):
        return "ineligible"
    with tempfile.TemporaryDirectory() as overlay:
        verify.build_overlay(overlay, changes)
        try:
            proc = subprocess.run(
                [sys.executable, __file__, "--worker", str(reference)],
                cwd=overlay, preexec_fn=limits, timeout=35,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return "timeout"
    return {0: "match", 1: "execution-error", 3: "different", 4: "reference-changed"}.get(
        proc.returncode, "inconclusive")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("patches", nargs="?", type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        sys.exit(worker(args.reference))
    if args.patches is None:
        parser.error("supply a directory of patches")
    for item in sorted(args.patches.glob("*.patch")):
        try:
            verdict = replay(item, args.reference.resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            verdict = type(exc).__name__
        print(json.dumps([item.stem, verdict]), flush=True)
