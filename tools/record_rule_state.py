"""Identify rule calls after which the per-query schema caches have no consumers."""
import functools
import gzip
import inspect
import json
import pickle
import sys
from pathlib import Path

sys.path[:0] = ["/app/sqlglot", "/app/selector"]
from sqlglot import executor
from sqlglot.optimizer.optimizer import RULES
from tests import test_executor as tests
from rule_corpus import ROOT, fingerprint, persistent_kwargs
from result_replay import table_digest

accepted = {}
index = 0


def wrap(rule):
    @functools.wraps(rule)
    def observe(expr, **kw):
        global index
        current = index
        index += 1
        with gzip.open(ROOT / f"{current}.gz", "rb") as stream:
            mod, name, _, after, expected, same, _ = pickle.load(stream)
        assert (mod, name) == (rule.__module__, rule.__name__)
        tail = RULES[RULES.index(rule) + 1:]
        terminal = ("schema" in kw and all("schema" not in inspect.getfullargspec(fn).args for fn in tail))
        before = persistent_kwargs(kw) if terminal else None
        result = rule(expr, **kw)
        if "schema" not in kw:
            return result
        assert (result is expr) == same and fingerprint(result) == expected and fingerprint(expr) == after
        if terminal and persistent_kwargs(kw) == before:
            accepted[current] = True
        return result
    observe.__signature__ = inspect.signature(rule)
    return observe


optimize = executor.optimize
rules = tuple(wrap(fn) for fn in RULES)
executor.optimize = lambda *args, **kw: optimize(*args, rules=rules, **kw)
queries, tables = pickle.loads(Path("/app/staged-tables.pkl").read_bytes())
expected = json.loads(Path("/app/staged.json").read_text())["ds"]
tests.initializer(tests.TPCDS_SCHEMA, tables)
for i, (meta, sql, _) in enumerate(queries):
    table = tests.mp_execute(pickle.loads(pickle.dumps(tests.parse_one(sql))), meta)
    if table is not None:
        assert table_digest(table) == expected[i][-1]
assert index == 57 * len(RULES)
Path(sys.argv[1]).write_text(json.dumps(sorted(accepted)))
print(index, len(accepted))
