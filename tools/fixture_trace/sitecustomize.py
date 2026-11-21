"""Record direct fixture reads in the parent and spawned test workers."""
import atexit
import json
import os
from pathlib import Path
import sys

reads = set()
loading = None
node = None
root = "/app/sqlglot/tests/fixtures/"


def audit(event, args):
    if event != "open" or not isinstance(args[0], str):
        return
    name = args[0]
    if name.startswith(root) and name.endswith(".sql") and name != loading:
        owner = node or os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0] or "*"
        reads.add((owner, name[len(root):]))


sys.addaudithook(audit)
atexit.register(lambda: Path("/out", f"raw-{os.getpid()}.json").write_text(json.dumps(sorted(reads))))
