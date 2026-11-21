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
    for node in ids:
        path, cls, method = node.split("::")
        module = importlib.import_module(path[:-3].replace("/", "."))
        case = getattr(module, cls)(method)
        if not isinstance(case, unittest.TestCase):
            raise TypeError(node)
        suite.addTest(case)
    result = unittest.TestResult()
    result.failfast = True
    suite.run(result)
    if (not ids or result.testsRun != len(ids) or result.skipped or result.expectedFailures
            or not result.wasSuccessful()):
        raise ValueError("incomplete or failing worker")


if __name__ == "__main__":
    main()
