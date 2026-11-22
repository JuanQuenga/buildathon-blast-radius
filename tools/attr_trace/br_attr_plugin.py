"""pytest plugin for offline class-attribute and default-argument tracing.

Exposes the active test nodeid on ``builtins._br_test`` during the call phase
only, and inlines ``ProcessPoolExecutor`` so worker-pool tests (such as
``TestOptimizer.check_file`` and ``TestExecutor.test_execute_tpcds``) attribute
every observation to the exact calling test.
"""
import builtins
import concurrent.futures as cf
import concurrent.futures.process as cfp
import pickle

import pytest


class _InlinePool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
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


def pytest_configure(config):
    cf.ProcessPoolExecutor = _InlinePool
    cfp.ProcessPoolExecutor = _InlinePool


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    builtins._br_test = item.nodeid
    try:
        yield
    finally:
        builtins._br_test = None
