"""pytest plugin: expose the running test's node id to the equivalence observer during the call phase only."""
import builtins

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    builtins._br_test = item.nodeid
    try:
        yield
    finally:
        flush = getattr(builtins, "_br_bool_flush", None)
        if flush is not None:
            flush()
        builtins._br_test = None
