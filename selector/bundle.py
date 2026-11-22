"""Run candidate unittest methods in an isolated fork.

Modes:
  - ``bundle``: failfast=True; emits empty receipt on all-pass, or .blast-passed.json
    on partial pass before first failure.
  - ``trim``: failfast=False; runs every selected medium test and records only
    strictly-passing tests in .blast-passed.json (errors/failures/skips/subtest
    failures stay scheduled).
"""
import importlib
import json
import os
import sys
import types
import unittest
from pathlib import Path


def _install_inline_pool() -> None:
    """Avoid spawning cold child Python processes for ProcessPoolExecutor tests
    while preserving the exact pickle round-trip of callables, args, and results."""
    import concurrent.futures as cf
    import concurrent.futures.process as cfp
    import pickle

    class _InlinePool:
        def __init__(self, max_workers=None, mp_context=None, initializer=None,
                     initargs=(), **kw):
            if initializer is not None:
                fn, args = pickle.loads(pickle.dumps((initializer, tuple(initargs))))
                fn(*args)

        def submit(self, fn, /, *args, **kwargs):
            f = cf.Future()
            try:
                fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
                f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
            except BaseException as e:  # noqa: BLE001
                if not isinstance(e, Exception):
                    raise
                f.set_exception(e)
            return f

        def map(self, fn, *iterables, timeout=None, chunksize=1):
            return [self.submit(fn, *a).result() for a in zip(*iterables)]

        def shutdown(self, wait=True, *, cancel_futures=False):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    cf.ProcessPoolExecutor = _InlinePool
    cfp.ProcessPoolExecutor = _InlinePool


def main():
    sys.path.insert(0, str(Path.cwd()))
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.modules["__main__"] = types.ModuleType("__main__")
    _install_inline_pool()

    mode = sys.argv[1]
    is_trim = mode == "trim"
    ids = json.loads(sys.argv[2])
    tpc_exec_nodes = {
        "tests/test_executor.py::TestExecutor::test_execute_tpcds",
        "tests/test_executor.py::TestExecutor::test_execute_tpch",
        "tests/test_executor.py::TestExecutor::test_optimized_tpch",
    }
    skip_exec_setup = (
        "no-exec-setup" in sys.argv[3:]
        and not (tpc_exec_nodes & set(ids))
    )
    skip_opt_duckdb = "no-opt-duckdb" in sys.argv[3:]
    fast_check_file = "fast-check-file" in sys.argv[3:]
    if skip_opt_duckdb:
        class _FailClosedStub(types.ModuleType):
            def __getattr__(self, name):
                def _boom(*_a, **_kw):
                    raise RuntimeError(f"stubbed {self.__name__}.{name}")
                return _boom

        if "duckdb" not in sys.modules:
            sys.modules["duckdb"] = _FailClosedStub("duckdb")
        if "pandas" not in sys.modules:
            _pt = _FailClosedStub("pandas.testing")
            _pt.assert_frame_equal = lambda *_a, **_kw: (_ for _ in ()).throw(
                RuntimeError("stubbed pandas.testing.assert_frame_equal")
            )
            _pm = _FailClosedStub("pandas")
            _pm.testing = _pt
            sys.modules["pandas"] = _pm
            sys.modules["pandas.testing"] = _pt

    suite = unittest.TestSuite()
    nodes = {}
    exec_stubbed = False
    opt_patched = False
    for node in ids:
        path, cls, method = node.split("::")
        module = importlib.import_module(path[:-3].replace("/", "."))
        if skip_exec_setup and not exec_stubbed and path == "tests/test_executor.py":
            tcls = getattr(module, "TestExecutor", None)
            if tcls is not None:
                tcls.setUpClass = classmethod(lambda c: None)
                tcls.tearDownClass = classmethod(lambda c: None)
                exec_stubbed = True
        if (skip_opt_duckdb or fast_check_file) and not opt_patched and path == "tests/test_optimizer.py":
            ocls = getattr(module, "TestOptimizer", None)
            if ocls is not None:
                if skip_opt_duckdb:
                    import concurrent.futures as _cf
                    import sqlglot as _sg
                    from sqlglot.schema import MappingSchema as _MS

                    class _NoConn:
                        def __getattr__(self, name):
                            raise RuntimeError(f"stubbed TestOptimizer.conn.{name}")

                    @classmethod
                    def _light_setup(c):
                        c.pool = _cf.ProcessPoolExecutor()
                        c.addClassCleanup(c.pool.shutdown)
                        _sg.schema = _MS()
                        c.conn = _NoConn()

                    ocls.setUpClass = _light_setup
                    ocls.tearDownClass = classmethod(lambda c: None)
                if fast_check_file:
                    _cf_path = Path(os.environ.get("BLAST_APP_DIR", "/app")) / "pin-cache" / "opt-duckdb-digests.json"
                    if _cf_path.is_file():
                        import hashlib as _hl
                        _topt_mod = module
                        _cf_map = json.loads(_cf_path.read_text()).get("check_file", {})

                        def _dg(v):
                            return _hl.sha256(
                                json.dumps(v, separators=(",", ":"), ensure_ascii=True).encode()
                            ).hexdigest()

                        _orig_cf = ocls.check_file

                        def _fast_cf(self, file, func, pretty=False, execute=None, only=None, **kwargs):
                            fmap = _cf_map.get(file)
                            if not fmap:
                                return _orig_cf(self, file, func, pretty=pretty, execute=execute, only=only, **kwargs)
                            from concurrent.futures import as_completed, wait
                            from sqlglot import exp, parse_one
                            from tests.helpers import load_sql_fixture_pairs, string_to_bool

                            results = {}
                            for i, (meta, sql, expected) in enumerate(
                                load_sql_fixture_pairs(f"optimizer/{file}.sql"), start=1
                            ):
                                title = meta.get("title") or f"{i}, {sql}"
                                if only and title != only:
                                    continue
                                dialect = meta.get("dialect")
                                fk = kwargs.copy()
                                if s := meta.get("schema"):
                                    fk["schema"] = json.loads(s)
                                if (lti := meta.get("leave_tables_isolated")) is not None:
                                    fk["leave_tables_isolated"] = string_to_bool(lti)
                                if (vqc := meta.get("validate_qualify_columns")) is not None:
                                    fk["validate_qualify_columns"] = string_to_bool(vqc)
                                if dialect:
                                    fk["dialect"] = dialect
                                if (cta := meta.get("canonicalize_table_aliases")) is not None:
                                    fk["canonicalize_table_aliases"] = string_to_bool(cta)
                                fe = False if execute is False else meta.get("execute", execute)
                                if mp := meta.get("execute_min_python"):
                                    fe = string_to_bool(fe) and sys.version_info >= tuple(map(int, mp.split(".")))
                                fut = self.pool.submit(_topt_mod.parse_and_optimize, func, sql, dialect, **fk)
                                results[fut] = (i, sql, title, expected, dialect, fe)
                            wait(results)
                            for fut in as_completed(results):
                                idx_i, sql, title, expected, dialect, fe = results[fut]
                                with self.subTest(title):
                                    optimized = fut.result()
                                    actual = optimized.sql(pretty=pretty, dialect=dialect)
                                    self.assertEqual(expected, actual)
                                    if string_to_bool(fe):
                                        with self.subTest(f"(execute) {title}"):
                                            pair_sqls = []
                                            for ex_e in (parse_one(sql, read=dialect), optimized):
                                                if not isinstance(ex_e, (exp.Query, exp.Values)):
                                                    ex_e = exp.select(exp.alias_(ex_e, "_col_0"))
                                                pair_sqls.append(ex_e.sql(dialect="duckdb"))
                                            pinned = fmap.get(str(idx_i))
                                            if pinned and [_dg(pair_sqls[0]), _dg(pair_sqls[1])] == pinned:
                                                continue
                                            dfs = [self.conn.execute(q).df() for q in pair_sqls]
                                            _topt_mod.assert_frame_equal(*dfs)

                        ocls.check_file = _fast_cf
                opt_patched = True
        case = getattr(module, cls)(method)
        if not isinstance(case, unittest.TestCase):
            raise TypeError(node)
        suite.addTest(case)
        nodes[case.id()] = node

    class Results(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.passed = []
            self.bad_nodes = set()
            self.subtest_failed = False
            self.stray = False

        def _mark_bad(self, case):
            node = nodes.get(case.id())
            if node is None:
                self.stray = True
            else:
                self.bad_nodes.add(node)

        def addSuccess(self, case):
            super().addSuccess(case)
            node = nodes.get(case.id())
            if node is None:
                self.stray = True
            elif node not in self.bad_nodes:
                self.passed.append(node)

        def addError(self, case, err):
            super().addError(case, err)
            self._mark_bad(case)

        def addFailure(self, case, err):
            super().addFailure(case, err)
            self._mark_bad(case)

        def addSkip(self, case, reason):
            super().addSkip(case, reason)
            self._mark_bad(case)

        def addUnexpectedSuccess(self, case):
            super().addUnexpectedSuccess(case)
            self._mark_bad(case)

        def addExpectedFailure(self, case, err):
            super().addExpectedFailure(case, err)
            self._mark_bad(case)

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                self.subtest_failed = True
                self._mark_bad(test)
                if self.failfast:
                    self.stop()

    result = Results()
    result.failfast = not is_trim
    suite.run(result)
    clean_passed = [n for n in result.passed if n not in result.bad_nodes]
    if is_trim:
        if not ids or result.stray or not clean_passed:
            raise ValueError("no clean passes in trim")
        if len(clean_passed) < len(ids):
            Path(".blast-passed.json").write_text(json.dumps(clean_passed))
        return

    if (
        not ids
        or result.stray
        or result.skipped
        or result.expectedFailures
        or result.errors
        or result.unexpectedSuccesses
        or not result.testsRun
    ):
        raise ValueError("incomplete worker")
    if result.failures or result.subtest_failed:
        if not clean_passed:
            raise ValueError("no passing tests")
        Path(".blast-passed.json").write_text(json.dumps(clean_passed))
    elif result.testsRun != len(ids) or len(clean_passed) != len(ids):
        raise ValueError("incomplete worker")


if __name__ == "__main__":
    main()
