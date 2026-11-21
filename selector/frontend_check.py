"""Check pinned frontend fingerprints without opening tables or running SQL."""
import json
import os
from pathlib import Path
import pickle
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(1, str(Path(__file__).parent))
from staged import digest


def check(indices):
    from sqlglot import parse_one, transpile
    inputs = json.loads(Path("/app/pin-cache/ds-inputs.json").read_text())
    expected = json.loads(Path("/app/staged.json").read_text())["ds"]
    for index in indices:
        sql, row = inputs[index], expected[index]
        expr = pickle.loads(pickle.dumps(parse_one(sql)))
        if digest(expr.dump()) != row[2]:
            raise ValueError("parsed or serialized expression changed")
        if len(row) > 5:
            if digest(transpile(sql, write="duckdb")) != row[4]:
                raise ValueError("reference SQL changed")



if __name__ == "__main__":
    check(json.loads(sys.argv[2]))
