"""Record SQLGlot line coverage on each side of the pinned test's pickle boundary."""
import gzip
import json
import os
from pathlib import Path
import pickle

from heavy_cases import Recorder


def record():
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    rec = Recorder(True)
    with rec.case("setup"):
        from tests import test_executor as tests
        test = tests.TestExecutor("test_execute_tpcds")
        test.setUpClass()
        tests.initializer(tests.TPCDS_SCHEMA, test.tpcds_tables)
    workers, combined = [], []
    for meta, sql, _ in test.tpcds_sqls:
        with rec.case("front"):
            payload = pickle.dumps((tests.mp_execute, tests.parse_one(sql), meta))
        with rec.case("worker"):
            fn, expr, args = pickle.loads(payload)
            result = pickle.dumps(fn(expr, args))
        with rec.case("reference"):
            if pickle.loads(result) is not None:
                tests.transpile(sql, write="duckdb")
        worker = rec.rows["worker"]["lines"]
        workers.append(worker)
        lines = {}
        for group in ("front", "worker", "reference"):
            for path, hit in rec.rows[group]["lines"].items():
                lines.setdefault(path, set()).update(hit)
        combined.append({p: sorted(hit) for p, hit in lines.items()})
    with rec.case("teardown"):
        test.tearDownClass()
    assert len(workers) == 99
    with gzip.open(Path("/app/query-lines.json.gz"), "wt") as stream:
        json.dump(dict(worker=workers, all=combined, setup=rec.rows["setup"]["lines"],
                       teardown=rec.rows["teardown"]["lines"]), stream, separators=(",", ":"))


if __name__ == "__main__":
    record()
