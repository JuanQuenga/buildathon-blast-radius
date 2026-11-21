"""Derive executable plan keys from the verified full-fixture pin recording."""
import gzip
import json
import pickle
import sys
from pathlib import Path

sys.path[:0] = ["/app/sqlglot", "/app/selector"]
from staged import execution_key

with gzip.open(sys.argv[1], "rb") as stream:
    plans, _ = pickle.load(stream)
keys = {}
for i, raw in plans.items():
    plan, _ = pickle.loads(raw)
    keys[i] = execution_key(plan)
Path(sys.argv[2]).write_text(json.dumps(keys, sort_keys=True))
