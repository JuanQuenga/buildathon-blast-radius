"""Check every catalog node is present after a real patched pytest collection."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd()))
import pytest

class Catalog:
    matched = False

    def pytest_collection_finish(self, session):
        required = {row["id"] for row in json.loads(Path("/data/tests.json").read_text())["tests"]}
        self.matched = required <= {item.nodeid for item in session.items}

if __name__ == "__main__":
    catalog = Catalog()
    if pytest.main(["--collect-only", "-qq", "-p", "no:cacheprovider", "tests"], plugins=[catalog]) or not catalog.matched:
        raise RuntimeError("incomplete pytest collection")
