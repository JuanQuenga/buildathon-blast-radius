"""Record and replay optimizer rules with self-contained inputs from the pin."""
import collections
import functools
import gzip
import hashlib
import inspect
import json
import os
from pathlib import Path
import pickle
import sys
import time

ROOT = Path(os.environ.get("BLAST_APP_DIR", "/app")) / "rules"


def fingerprint(expr):
    return hashlib.sha256(json.dumps(expr.dump(), separators=(",", ":")).encode()).digest()


def record():
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:1])
    sys.path.insert(0, str(Path.cwd()))
    from tests import test_executor as tests
    import sqlglot.executor as executor
    from sqlglot.optimizer.optimizer import RULES
    prefix = str(Path.cwd()) + "/"
    optimizer = prefix + "sqlglot/optimizer/"
    outside = collections.defaultdict(set)
    current = outside
    records = []
    ROOT.mkdir()
    def line(code, lineno):
        if code.co_filename.startswith(optimizer):
            current[code.co_filename[len(prefix):]].add(lineno)
        return sys.monitoring.DISABLE

    def wrap(rule):
        @functools.wraps(rule)
        def observe(expr, **kwargs):
            nonlocal current
            if current is not outside:
                return rule(expr, **kwargs)
            try:
                inputs = pickle.dumps((expr, kwargs))
            except (TypeError, pickle.PickleError, AttributeError):
                return rule(expr, **kwargs)
            current = collections.defaultdict(set)
            sys.monitoring.restart_events()
            try:
                result = rule(expr, **kwargs)
                hits = {p: sorted(lines) for p, lines in current.items()}
            finally:
                current = outside
                sys.monitoring.restart_events()
            if hits:
                index = len(records)
                records.append(hits)
                with gzip.open(ROOT / f"{index}.gz", "wb", compresslevel=1) as stream:
                    pickle.dump((rule.__module__, rule.__name__, inputs, fingerprint(expr),
                                 fingerprint(result), result is expr,
                                 hashlib.sha256(pickle.dumps(kwargs)).digest()), stream)
            return result
        observe.__signature__ = inspect.signature(rule)
        return observe

    default = executor.optimize
    rules = tuple(map(wrap, RULES))
    def optimize(*args, **kwargs):
        return default(*args, rules=rules, **kwargs)
    executor.optimize = optimize
    sys.monitoring.use_tool_id(4, "optimizer rule inputs")
    sys.monitoring.register_callback(4, sys.monitoring.events.LINE, line)
    sys.monitoring.set_events(4, sys.monitoring.events.LINE)
    test = tests.TestExecutor("test_execute_tpcds")
    test.setUpClass()
    try:
        tests.initializer(tests.TPCDS_SCHEMA, test.tpcds_tables)
        for i, (meta, sql, _) in enumerate(test.tpcds_sqls):
            expr = pickle.loads(pickle.dumps(tests.parse_one(sql)))
            table = tests.mp_execute(expr, meta)
            if table is not None:
                test.subtestHelper(i, pickle.loads(pickle.dumps(table)), tpch=False)
    finally:
        test.tearDownClass()
    sys.monitoring.set_events(4, 0)
    if len(test.tpcds_sqls) != 99 or not records:
        raise ValueError("incomplete rule corpus")
    with gzip.open(ROOT / "index.gz", "wt") as stream:
        json.dump([records, {p: sorted(lines) for p, lines in outside.items()}], stream)

    costs = []
    for i in range(len(records)):
        start = time.process_time()
        try:
            replay([i])
        except Exception:
            costs.append(None)
        else:
            costs.append(time.process_time() - start)
    (ROOT / "costs.json").write_text(json.dumps(costs))


@functools.cache
def costs():
    return json.loads((ROOT / "costs.json").read_text())


def estimate(indices):
    return 0.10 + sum(costs()[i] for i in indices)


@functools.cache
def index():
    with gzip.open(ROOT / "index.gz") as stream:
        return json.load(stream)


def select(regions):
    from .query_slice import indices
    if not all(p.startswith("sqlglot/optimizer/") for p in regions):
        return None
    selected = indices("rules", regions)
    return selected if selected is not None and all(costs()[i] is not None for i in selected) else None


def _query_exec_costs():
    costs_path = ROOT.parent / "ds-costs.json.gz"
    if not costs_path.is_file():
        return {}
    with gzip.open(costs_path, "rt", encoding="utf-8") as stream:
        rows = json.load(stream)
    out = {}
    for k, v in rows.items():
        if k.startswith("ds:") and k[3:].isdigit():
            ph = v.get("phases", {})
            out[int(k[3:])] = ph.get("optimize", 0.0) + ph.get("plan", 0.0) + ph.get("execute", 0.0)
    return out


def _divergent_exec_cost(divergent):
    qcosts = _query_exec_costs()
    return sum(qcosts.get(q, 0.0) for q in divergent)


def replay(indices, verify_query=None):
    started = time.process_time()
    divergent = set()
    queries = json.loads((Path(__file__).parent / "maps/rule-queries.json").read_text()) if verify_query else None
    import importlib
    sys.path.insert(0, str(Path.cwd()))
    # Pass 1: replay all selected optimizer rules first (~0.15-0.30s) to discover
    # the exact set of divergent TPC-DS queries before committing to execution.
    for i in indices:
        if verify_query and queries[i] in divergent:
            continue
        with gzip.open(ROOT / f"{i}.gz", "rb") as stream:
            module, name, inputs, after, expected, identical, kwargs_digest = pickle.load(stream)
        expr, kwargs = pickle.loads(inputs)
        try:
            result = getattr(importlib.import_module(module), name)(expr, **kwargs)
        except Exception:
            if verify_query is None:
                raise
            divergent.add(queries[i])
            continue
        output = fingerprint(result)
        if ((result is expr) != identical or output != expected
                or (output if result is expr else fingerprint(expr)) != after
                or hashlib.sha256(pickle.dumps(kwargs)).digest() != kwargs_digest):
            if verify_query is None:
                raise ValueError("rule result changed")
            divergent.add(queries[i])
    # Pass 2: verify divergent queries in original fixture order (0..98) using
    # plan-first checking in result_replay so intermediate rule AST changes that
    # canonicalize to identical plans skip execution, while genuinely broken
    # optimizer edits abort mid-planning once changed plans exceed the CPU budget.
    if verify_query is not None and divergent:
        ordered = sorted(divergent)
        old_start = os.environ.get("BLAST_PROBE_START_CPU")
        os.environ["BLAST_PROBE_START_CPU"] = str(started)
        try:
            if getattr(verify_query, "_accepts_batch", False):
                verify_query(ordered)
            else:
                for q in ordered:
                    verify_query(q)
        finally:
            if old_start is None:
                os.environ.pop("BLAST_PROBE_START_CPU", None)
            else:
                os.environ["BLAST_PROBE_START_CPU"] = old_start
    return sorted(divergent)


if __name__ == "__main__":
    if sys.argv[1] == "record":
        record()
    else:
        indices = json.loads(sys.argv[2])
        if len(sys.argv) < 4:
            replay(indices)
        else:
            sys.path.insert(0, str(Path(__file__).parent))
            from phase import mark
            from staged import rule_query_check

            def check_queries(qs):
                mark("divergent")
                rule_query_check()(qs)

            check_queries._accepts_batch = True
            replay(indices, verify_query=check_queries)
