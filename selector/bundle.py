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

    # Group contiguous tests by module path so fail-fast `bundle` mode runs cheap
    # tier-0 dialect/unit modules BEFORE ever importing heavy modules like
    # tests/test_optimizer.py (duckdb/pandas/numpy) or tests/test_executor.py!
    import itertools
    groups = [
        (mod_path, list(grp))
        for mod_path, grp in itertools.groupby(ids, key=lambda n: n.split("::", 1)[0])
    ]

    nodes = {}

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
    exec_stubbed = False
    opt_stubbed = False
    for path, grp_nodes in groups:
        module = importlib.import_module(path[:-3].replace("/", "."))
        if skip_exec_setup and not exec_stubbed and path == "tests/test_executor.py":
            tcls = getattr(module, "TestExecutor", None)
            if tcls is not None:
                tcls.setUpClass = classmethod(lambda c: None)
                tcls.tearDownClass = classmethod(lambda c: None)
                exec_stubbed = True
        if skip_opt_duckdb and not opt_stubbed and path == "tests/test_optimizer.py":
            ocls = getattr(module, "TestOptimizer", None)
            if ocls is not None:
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
                opt_stubbed = True
        suite = unittest.TestSuite()
        for node in grp_nodes:
            _, cls, method = node.split("::")
            case = getattr(module, cls)(method)
            if not isinstance(case, unittest.TestCase):
                raise TypeError(node)
            suite.addTest(case)
            nodes[case.id()] = node
        suite.run(result)
        if result.failfast and (
            result.shouldStop
            or result.failures
            or result.errors
            or result.subtest_failed
            or result.skipped
            or result.expectedFailures
            or result.unexpectedSuccesses
            or result.stray
        ):
            break
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
