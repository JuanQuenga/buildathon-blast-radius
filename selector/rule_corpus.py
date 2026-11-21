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

ROOT = Path("/app/rules")


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


def persistent_kwargs(kwargs):
    """The executor discards its per-query schema after its last optimizer consumer."""
    schema = kwargs.get("schema")
    if type(schema).__module__ != "sqlglot.schema" or type(schema).__name__ != "MappingSchema":
        raise ValueError("unexpected schema type")
    caches = {"_type_mapping_cache", "_normalized_table_cache", "_normalized_name_cache", "_find_cache"}
    return pickle.dumps(({k: v for k, v in kwargs.items() if k != "schema"},
                         {k: v for k, v in vars(schema).items() if k not in caches}))


@functools.cache
def terminal_schemas():
    return set(json.loads((Path(__file__).parent / "maps/terminal-schema.json").read_text()))


def record_at(i):
    with gzip.open(ROOT / f"{i}.gz", "rb") as stream:
        return pickle.load(stream)


def continue_rules(i, query, queries, result):
    """Run the real suffix until the pin state is recovered or a whole result is needed."""
    from sqlglot.optimizer.optimizer import RULES

    chain = [j for j, q in enumerate(queries) if q == query]
    if len(chain) != len(RULES):
        return None
    position = chain.index(i)
    for j, rule in zip(chain[position + 1:], RULES[position + 1:]):
        module, name, inputs, after, expected, same, kwargs_digest = record_at(j)
        if (rule.__module__, rule.__name__) != (module, name):
            return None
        _, kwargs = pickle.loads(inputs)
        expr = result
        result = rule(expr, **kwargs)
        if hashlib.sha256(pickle.dumps(kwargs)).digest() != kwargs_digest:
            return None
        output = fingerprint(result)
        if ((result is expr) == same and output == expected
                and (output if same else fingerprint(expr)) == after):
            return j
    from staged import check_optimized
    check_optimized(query, result)
    return chain[-1]


def replay(indices, verify_query=None):
    divergent = set()
    through = {}
    queries = json.loads((Path(__file__).parent / "maps/rule-queries.json").read_text()) if verify_query else None
    import importlib
    sys.path.insert(0, str(Path.cwd()))
    for i in indices:
        if verify_query and (queries[i] in divergent or i <= through.get(queries[i], -1)):
            continue
        module, name, inputs, after, expected, identical, kwargs_digest = record_at(i)
        expr, kwargs = pickle.loads(inputs)
        terminal = persistent_kwargs(kwargs) if verify_query and i in terminal_schemas() else None
        try:
            result = getattr(importlib.import_module(module), name)(expr, **kwargs)
        except Exception:
            if verify_query is None:
                raise
            verify_query(queries[i])
            divergent.add(queries[i])
            continue
        output = fingerprint(result)
        state_matches = (terminal is not None and persistent_kwargs(kwargs) == terminal
                         or hashlib.sha256(pickle.dumps(kwargs)).digest() == kwargs_digest)
        if ((result is expr) != identical or output != expected
                or (output if result is expr else fingerprint(expr)) != after
                or not state_matches):
            if verify_query is None:
                raise ValueError("rule result changed")
            if state_matches:
                try:
                    end = continue_rules(i, queries[i], queries, result)
                except Exception:
                    end = None
                if end is not None:
                    through[queries[i]] = end
                    continue
            verify_query(queries[i])
            divergent.add(queries[i])
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

            def check_query(query):
                mark("divergent")
                rule_query_check()(query)

            replay(indices, verify_query=check_query)
