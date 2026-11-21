"""Optional soft address-space allowance for one explicitly named test call."""
import os
from pathlib import Path
import resource

import pytest


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    if item.nodeid != os.environ["BR_LIMIT_TEST_NODE"]:
        return (yield)
    old = resource.getrlimit(resource.RLIMIT_AS)
    pages = int(Path("/proc/self/statm").read_text().split()[0])
    limit = pages * os.sysconf("SC_PAGE_SIZE") + (int(os.environ["BR_LIMIT_AS_MIB"]) << 20)
    if old[0] != resource.RLIM_INFINITY:
        limit = min(limit, old[0])
    resource.setrlimit(resource.RLIMIT_AS, (limit, old[1]))
    old_path = os.environ["PYTHONPATH"]
    if os.environ.get("BR_CHILD_AS_MIB"):
        child_site = Path(__file__).parent / "memory_child"
        os.environ["PYTHONPATH"] = str(child_site) + os.pathsep + old_path
    try:
        return (yield)
    finally:
        os.environ["PYTHONPATH"] = old_path
        resource.setrlimit(resource.RLIMIT_AS, old)
