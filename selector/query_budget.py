"""Bound a single execution using measured CPU on the full pinned fixture."""
from contextlib import contextmanager
import functools
import gzip
import json
import signal
import time


@functools.cache
def execution_costs():
    with gzip.open("/app/ds-costs.json.gz") as stream:
        rows = json.load(stream)
    return [rows[f"ds:{i}"]["phases"].get("execute", 0) for i in range(99)]


@contextmanager
def query_budget(i):
    remaining, _ = signal.getitimer(signal.ITIMER_PROF)
    limit = 2 + 12 * execution_costs()[i]
    if remaining <= limit:
        yield
        return
    start = time.process_time()
    signal.setitimer(signal.ITIMER_PROF, limit)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_PROF, max(0.001, remaining - (time.process_time() - start)))
