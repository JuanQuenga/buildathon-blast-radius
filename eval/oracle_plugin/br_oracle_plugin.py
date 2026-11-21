"""pytest plugin used by eval/oracle.py to label a patched checkout.

Loaded with ``-p br_oracle_plugin`` (its directory is put on PYTHONPATH after
the checkout).  Streams one JSON object per relevant report to the file named
by ``BR_ORACLE_OUT`` so a crash mid-run still leaves partial results.

Records:
  {"t": "collect", "nodeid": ..., "failed": bool, "longrepr": ...}
  {"t": "test", "nodeid": ..., "when": ..., "outcome": ..., "sub": bool,
   "duration": float, "msg": ...}
  {"t": "end", "exitstatus": int}

A failing subtest report (pytest 9 ``SubtestReport``) carries the parent's
nodeid, so "any failed report for a nodeid" == "the test failed".
"""

from __future__ import annotations

import json
import os

_OUT = None


def _w(obj):
    global _OUT
    if _OUT is None:
        _OUT = open(os.environ["BR_ORACLE_OUT"], "a", buffering=1, encoding="utf-8")
    _OUT.write(json.dumps(obj) + "\n")


def _short(longrepr):
    if longrepr is None:
        return None
    s = str(longrepr)
    # keep the tail: the exception line is usually last
    return s[-600:]


def pytest_collectreport(report):
    if report.failed:
        _w({"t": "collect", "nodeid": report.nodeid, "failed": True, "msg": _short(report.longrepr)})


def pytest_runtest_logreport(report):
    sub = type(report).__name__ != "TestReport"
    rec = {
        "t": "test",
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "sub": sub,
        "duration": round(getattr(report, "duration", 0.0), 4),
    }
    if report.failed:
        detail = str(report.longrepr or "")
        rec["msg"] = detail[-600:]
        if "Timeout (>" in detail and "from pytest-timeout" in detail:
            rec["timeout"] = True
    # only stream what we need: call reports, and anything that failed/skipped
    if report.when == "call" or report.failed or report.skipped:
        _w(rec)


def pytest_sessionfinish(session, exitstatus):
    _w({"t": "end", "exitstatus": int(exitstatus)})
