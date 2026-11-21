"""Pinned subcases for profiling and replay. Run as a script in the overlay."""
import argparse
import ast
import collections
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from multiprocessing.reduction import ForkingPickler
import contextlib
import functools
import gzip
import inspect
import json
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
PREFIX = str(ROOT / "sqlglot") + "/"


class Recorder:
    def __init__(self, enabled, branches=False, trace=True):
        self.enabled = enabled
        self.rows = {}
        self.lines = collections.defaultdict(set)
        self.edges = {}
        self.arcs = collections.defaultdict(set)
        self.phases = collections.Counter()
        self.trace = enabled and trace
        if self.trace:
            sys.monitoring.use_tool_id(3, "subcase coverage")
            sys.monitoring.register_callback(3, sys.monitoring.events.LINE, self.line)
            sys.monitoring.register_callback(3, sys.monitoring.events.BRANCH, self.branch)
            sys.monitoring.set_events(3, sys.monitoring.events.LINE | (sys.monitoring.events.BRANCH if branches else 0))

    def line(self, code, number):
        if code.co_filename.startswith(PREFIX):
            self.lines[code.co_filename[len(str(ROOT)) + 1:]].add(number)
        return sys.monitoring.DISABLE

    @functools.lru_cache(maxsize=None)
    def positions(self, code):
        return list(code.co_positions())

    def branch(self, code, offset, destination):
        if not code.co_filename.startswith(PREFIX):
            return sys.monitoring.DISABLE
        positions = self.positions(code)
        src, dst = positions[offset // 2][0], positions[destination // 2][0]
        if src is not None and dst is not None:
            self.arcs[code.co_filename[len(str(ROOT)) + 1:]].add((src, dst))
        prior = self.edges.setdefault((code, offset), destination)
        if prior != destination:
            return sys.monitoring.DISABLE

    @contextlib.contextmanager
    def case(self, key):
        if self.enabled:
            self.edges.clear()
            self.arcs.clear()
            self.lines.clear()
            self.phases.clear()
            if self.trace:
                sys.monitoring.restart_events()
        start = time.process_time()
        yield
        if self.enabled:
            self.rows[key] = dict(cpu=time.process_time() - start, phases=dict(self.phases),
                                  arcs={p: sorted(v) for p, v in self.arcs.items()},
                                  lines={p: sorted(v) for p, v in self.lines.items()})

    def timed(self, owner, name, label):
        original = getattr(owner, name)
        @functools.wraps(original)
        def timed(*args, **kwargs):
            start = time.process_time()
            try:
                return original(*args, **kwargs)
            finally:
                self.phases[label] += time.process_time() - start
        setattr(owner, name, timed)


def initialize_worker(schema, tables, record, trace):
    global worker_recorder
    from tests import test_executor as tests
    worker_recorder = Recorder(record, branches=True, trace=trace)
    tests.initializer(schema, tables)
    if record:
        import sqlglot.executor as module
        from sqlglot.executor.python import PythonExecutor
        worker_recorder.timed(module, "optimize", "optimize")
        worker_recorder.timed(module.Plan, "__init__", "plan")
        worker_recorder.timed(PythonExecutor, "execute", "execute")


def profiled_execute(key, payload):
    with worker_recorder.case(key):
        fn, expr, meta = ForkingPickler.loads(payload)
        table = bytes(ForkingPickler.dumps(fn(expr, meta)))
    return table, worker_recorder.rows.pop(key, None)


def merge(first, second):
    first["cpu"] += second["cpu"]
    for phase, cost in second["phases"].items():
        first["phases"][phase] = first["phases"].get(phase, 0) + cost
    for path, arcs in second["arcs"].items():
        first["arcs"][path] = sorted(set(first["arcs"].get(path, ())).union(arcs))
    for path, lines in second["lines"].items():
        first["lines"][path] = sorted(set(first["lines"].get(path, ())).union(lines))


def executor(rec, selected):
    with rec.case("ds:setup"):
        from tests import test_executor as tests
        tests.TestExecutor.setUpClass()
        test = tests.TestExecutor("test_execute_tpcds")
    try:
        if rec.enabled:
            rec.timed(tests, "parse_one", "parse")
        if selected == set():
            return
        with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"),
                                 initializer=initialize_worker,
                                 initargs=(tests.TPCDS_SCHEMA, test.tpcds_tables, rec.enabled, rec.trace)) as pool:
            pending = []
            for i, (meta, sql, _) in enumerate(test.tpcds_sqls):
                key = f"ds:{i}"
                if selected is not None and key not in selected:
                    continue
                with rec.case(key):
                    expr = tests.parse_one(sql)
                    payload = bytes(ForkingPickler.dumps((tests.mp_execute, expr, meta)))
                    future = pool.submit(profiled_execute, key, payload)
                pending.append((i, key, future))
            for i, key, future in pending:
                parsed = rec.rows.pop(key, None)
                with rec.case(key):
                    payload, executed = future.result()
                    table = ForkingPickler.loads(payload)
                    if table is not None:
                        test.subtestHelper(i, table, tpch=False)
                if rec.enabled:
                    merge(rec.rows[key], parsed)
                    merge(rec.rows[key], executed)
    finally:
        with rec.case("ds:teardown"):
            tests.TestExecutor.tearDownClass()


def parser(rec, selected):
    with rec.case("parser:setup"):
        from tests import test_parser as tests
        test = tests.TestParser("test_max_nodes")
        fn = ast.parse(textwrap.dedent(inspect.getsource(test.test_max_nodes))).body[0]
        # Split only at assertion boundaries. Both large limit checks share sql.
        assert len(fn.body) == 15, ast.dump(fn)
        scope = dict(vars(tests), self=test)
    for i, (start, end) in enumerate(((0, 1), (1, 4), (4, 13), (13, 15))):
        key = f"parser:{i}"
        if selected is None or key in selected:
            with rec.case(key):
                try:
                    exec(compile(ast.Module(fn.body[start:end], []), "<pinned assertions>", "exec"), scope)
                except AssertionError as exc:
                    import os
                    if os.environ.get("BLAST_PRACTICE_DIAGNOSTIC") == "D4-parser":
                        import traceback
                        frames = traceback.extract_tb(exc.__traceback__)
                        if any("/unittest/" in frame.filename for frame in frames):
                            from phase import mark
                            mark("asserted")
                    raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group", choices=["ds", "parser", "collect"])
    ap.add_argument("--record")
    ap.add_argument("--time-only", action="store_true")
    ap.add_argument("cases", nargs="*")
    args = ap.parse_args()
    if args.group == "collect":
        import pytest
        if pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", "tests"]) != 0:
            raise RuntimeError("collection failed")
        return
    rec = Recorder(bool(args.record), branches=args.group == "ds", trace=not args.time_only)
    (executor if args.group == "ds" else parser)(rec, None if args.record else set(args.cases))
    if args.record:
        with gzip.open(args.record, "wt") as f:
            json.dump(rec.rows, f, separators=(",", ":"))


if __name__ == "__main__":
    main()
