"""pytest plugin: expose the running test's node id to the equivalence observer during the call phase only."""
import builtins

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    builtins._br_test = item.nodeid
    try:
        yield
    finally:
        builtins._br_test = None
