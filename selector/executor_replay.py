"""Replay patch-local executor changes with the pinned full-data plan inputs."""
import functools
from pathlib import Path
import pickle

ROOT = Path("/app/pin-cache")


@functools.cache
def full_table(name):
    if not name.isidentifier():
        raise ValueError("invalid table")
    return pickle.loads((ROOT / "tables" / name).read_bytes())


def prepared_query(i, selection, tables):
    """Restore an unaffected execution prefix at its original fixture position."""
    if not selection or i not in selection.get("cached", ()):
        return None
    from staged import execution_inputs
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import ensure_tables

    plans, prefixes = execution_inputs()
    if i not in plans:
        return None
    regions = selection["executor_regions"]
    if any(lo <= line <= hi for path, line in prefixes[i] for lo, hi in regions.get(path, ())):
        return None
    plan, names = pickle.loads(plans[i])
    engine = PythonExecutor(tables=ensure_tables({name: tables[name] for name in names}))
    return engine, plan
