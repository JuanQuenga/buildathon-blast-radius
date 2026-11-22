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
    suite = unittest.TestSuite()
    nodes = {}
    for node in ids:
        path, cls, method = node.split("::")
        module = importlib.import_module(path[:-3].replace("/", "."))
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
