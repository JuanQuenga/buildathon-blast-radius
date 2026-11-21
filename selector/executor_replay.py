"""Replay patch-local executor changes with the pinned full-data plan inputs."""
import functools
import json
from pathlib import Path
import pickle

from phase import mark
from result_replay import table_digest

ROOT = Path("/app/pin-cache")


@functools.cache
def full_table(name):
    if not name.isidentifier():
        raise ValueError("invalid table")
    return pickle.loads((ROOT / "tables" / name).read_bytes())


def check(selection):
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import ensure_tables

    regions = selection["executor_regions"]
    from staged import execution_inputs
    plans, prefixes = execution_inputs()
    reference = json.loads(Path("/app/staged.json").read_text())["ds"]
    checked = set()
    for i in selection["active"]:
        if i not in selection["cached"] or i not in plans or any(lo <= line <= hi for path, line in prefixes[i]
                                  for lo, hi in regions.get(path, ())):
            continue
        mark("execute")
        plan, names = pickle.loads(plans[i])
        tables = ensure_tables({n: full_table(n) for n in names})
        result = PythonExecutor(tables=tables).execute(plan)
        if table_digest(pickle.loads(pickle.dumps(result))) != reference[i][-1]:
            raise ValueError("full fixture result changed")
        checked.add(i)
    return [i for i in selection["active"] if i not in checked]
