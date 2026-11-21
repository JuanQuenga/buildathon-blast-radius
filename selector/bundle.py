"""Run all tests on a single occupied worker, stopping at the first failure."""
import importlib
import json
import sys
import unittest
from pathlib import Path


def main():
    sys.path.insert(0, str(Path.cwd()))
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

        def addSuccess(self, case):
            super().addSuccess(case)
            self.passed.append(nodes[case.id()])

    result = Results()
    result.failfast = True
    suite.run(result)
    if (not ids or result.skipped or result.expectedFailures or result.errors
            or result.unexpectedSuccesses or not result.testsRun):
        raise ValueError("incomplete worker")
    if result.failures:
        if not result.passed:
            raise ValueError("no passing tests")
        Path(".blast-passed.json").write_text(json.dumps(result.passed))
    elif result.testsRun != len(ids):
        raise ValueError("incomplete worker")


if __name__ == "__main__":
    main()
