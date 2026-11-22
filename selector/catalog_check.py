"""Record missing catalog nodes after a complete patched pytest collection."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd()))
import pytest


class Catalog:
    collected = None

    def __init__(self):
        self.errors = set()

    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.add(report.nodeid)

    def pytest_collection_finish(self, session):
        self.collected = {item.nodeid for item in session.items}


if __name__ == "__main__":
    import sqlglot

    if not Path(sqlglot.__file__).is_relative_to(Path.cwd()):
        raise RuntimeError("wrong package")
    catalog = Catalog()
    tests = json.loads((Path(os.environ.get("BLAST_DATA", "/data")) / "tests.json").read_text())["tests"]
    status = pytest.main(["--collect-only", "--continue-on-collection-errors",
                          "-qq", "-p", "no:cacheprovider", "tests"], plugins=[catalog])
    files = {row["file"] for row in tests}
    if (catalog.collected is None or catalog.errors - files
            or status != (pytest.ExitCode.TESTS_FAILED if catalog.errors else pytest.ExitCode.OK)):
        raise RuntimeError("incomplete collection")
    missing = [row["number"] for row in tests
               if row["file"] in catalog.errors or row["id"] not in catalog.collected]
    Path(".blast-missing.json").write_text(json.dumps(missing))
