"""Match each pinned optimizer recording to the TPC-DS query that invoked it."""
import functools
import gzip
import hashlib
import inspect
import json
import os
from pathlib import Path
import pickle
import sys

sys.path.insert(0, "/app/sqlglot")
from selector import rule_corpus
from sqlglot import executor
from sqlglot.optimizer.optimizer import RULES
from tests import test_executor as tests


def main():
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:1])
    records, _ = rule_corpus.index()
    mapping = []
    nested = False

    def wrap(rule):
        @functools.wraps(rule)
        def observe(expr, **kwargs):
            nonlocal nested
            if nested:
                return rule(expr, **kwargs)
            nested = True
            try:
                try:
                    inputs = pickle.dumps((expr, kwargs))
                except (TypeError, pickle.PickleError, AttributeError):
                    return rule(expr, **kwargs)
                result = rule(expr, **kwargs)
                actual = (rule.__module__, rule.__name__, inputs, rule_corpus.fingerprint(expr),
                          rule_corpus.fingerprint(result), result is expr,
                          hashlib.sha256(pickle.dumps(kwargs)).digest())
                with gzip.open(rule_corpus.ROOT / f"{len(mapping)}.gz", "rb") as stream:
                    if pickle.load(stream) != actual:
                        raise ValueError(f"pin recording mismatch at {len(mapping)}")
                mapping.append(query)
                return result
            finally:
                nested = False
        observe.__signature__ = inspect.signature(rule)
        return observe

    optimize = executor.optimize
    rules = tuple(map(wrap, RULES))
    executor.optimize = lambda *a, **kw: optimize(*a, rules=rules, **kw)
    test = tests.TestExecutor("test_execute_tpcds")
    test.setUpClass()
    try:
        tests.initializer(tests.TPCDS_SCHEMA, test.tpcds_tables)
        for query, (meta, sql, _) in enumerate(test.tpcds_sqls):
            expr = pickle.loads(pickle.dumps(tests.parse_one(sql)))
            table = tests.mp_execute(expr, meta)
            if table is not None:
                test.subtestHelper(query, pickle.loads(pickle.dumps(table)), tpch=False)
    finally:
        test.tearDownClass()
    assert len(mapping) == len(records)
    print(json.dumps(mapping))


if __name__ == "__main__":
    main()
