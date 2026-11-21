"""Benchmark exact pytest methods, including subtest and all three phase reports."""
import json
import os
import sys
from pathlib import Path
import pytest

class Receipt:
    def __init__(self, expected):
        self.expected = set(expected)
        self.phases = {node: set() for node in expected}
        self.complete_collection = False
        self.valid = True

    def pytest_collection_finish(self, session):
        nodes = [item.nodeid for item in session.items]
        self.complete_collection = len(nodes) == len(self.expected) and set(nodes) == self.expected

    def pytest_runtest_logreport(self, report):
        if report.nodeid not in self.phases or not report.passed or hasattr(report, "wasxfail"):
            self.valid = False
        elif type(report).__name__ != "SubtestReport":
            self.phases[report.nodeid].add(report.when)

nodes = json.loads(sys.argv[2])
plugin = Receipt(nodes)
status = pytest.main(["-q", "-x", "-p", "no:cacheprovider", *nodes], plugins=[plugin])
if (status != pytest.ExitCode.OK or not plugin.valid or not plugin.complete_collection
        or any(phases != {"setup","call","teardown"} for phases in plugin.phases.values())):
    raise ValueError("missing exact pytest pass")
