"""Check patched, unexecuted TPC-DS fixture inputs at the actual worker boundary."""
import json
import os
from pathlib import Path
import pickle
import sys

def main():
    sys.path.insert(0, os.getcwd())
    from sqlglot import parse_one
    from tests.helpers import load_sql_fixture_pairs
    rows = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    with Path("/app/pin-cache/query-fixtures.pkl").open("rb") as stream:
        baseline = pickle.load(stream)
    if len(rows) != len(baseline) or len(rows) != 99:
        raise ValueError("fixture structure changed")
    for (meta, sql, _), (prior_meta, source, _) in zip(rows, baseline):
        if meta != prior_meta:
            raise ValueError("worker metadata changed")
        if sql == source:
            continue
        # The original worker immediately returns None without reading the
        # expression for these rows. Its parent still parses and sends it.
        if meta.get("execute"):
            raise ValueError("full result comparison required")
        pickle.loads(pickle.dumps((parse_one(sql), meta)))


if __name__ == "__main__":
    main()
