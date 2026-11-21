"""Exercise a cross-query state change against the real TPC-DS test and replay."""
import difflib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from selector import heavy, verify


def main():
    path = "sqlglot/executor/python.py"
    before = (Path(verify.PRISTINE) / path).read_text()
    anchor = "        ctes, scope = self._ctes, self._outer_scope"
    # This transition occurs in fixture order but disappears when queries
    # are sorted by their measured cost. The actual pytest oracle checks it.
    state = '''        if outer_scope is None:
            width = len(self.tables.mapping)
            if getattr(PythonExecutor, "previous_width", 0) == 2 and width == 7:
                raise ValueError("unexpected schema transition")
            PythonExecutor.previous_width = width
'''
    if before.count(anchor) != 1:
        raise ValueError("pin changed")
    after = before.replace(anchor, state + anchor)
    patch = f"diff --git a/{path} b/{path}\n" + "".join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True), "a/" + path, "b/" + path))
    mode, paths, changes, cases = heavy.stage_edit(patch, "ds")
    if mode != "slice":
        raise ValueError("executor slice was not exercised")
    with tempfile.TemporaryDirectory() as overlay:
        verify.build_overlay(overlay, changes)
        receipt = heavy.replay(mode, [json.dumps(cases)], overlay, time.monotonic() + 100, paths)
        if receipt != "failed" or heavy.replay.phase != "execute":
            raise ValueError(f"unexpected replay receipt {receipt}, {heavy.replay.phase}")
        oracle = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
             "tests/test_executor.py::TestExecutor::test_execute_tpcds"],
            cwd=overlay, env=dict(os.environ, PYTHONPATH=overlay), capture_output=True, timeout=125)
        if oracle.returncode != 1 or b"unexpected schema transition" not in oracle.stdout:
            raise ValueError("real test did not report the expected failure")
        print("ordered replay and real pytest both reported the cross-query failure")


if __name__ == "__main__":
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    main()
