"""Record full-fixture DuckDB results from the pinned test outside image builds."""
import gzip
import os
from pathlib import Path
import pickle
import sys


def record(destination):
    # DuckDB uses the visible CPU set when sizing its worker pool.
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    sys.path.insert(0, "/app/sqlglot")
    from tests.test_executor import TestExecutor

    test = TestExecutor("test_execute_tpcds")
    test.setUpClass()
    try:
        frames = {i: test.cached_execute(sql, tpch=False)
                  for i, (meta, sql, _) in enumerate(test.tpcds_sqls) if meta.get("execute")}
        if len(frames) != 57:
            raise ValueError("incomplete TPC-DS reference")
        destination.write_bytes(gzip.compress(pickle.dumps(frames, protocol=4), mtime=0))
    finally:
        test.tearDownClass()


if __name__ == "__main__":
    record(Path(sys.argv[1]))
