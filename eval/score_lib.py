"""Exact re-implementation of the official validator and score (SCORING.md / starter score.py).

Cross-checked against the starter's score.py by ``eval/run.py --selftest``.
"""

from __future__ import annotations

import json
import math
from typing import Any

WORKER_MS = 2_000
MISS_MS = 300_000
MAX_WORKERS = 8
STDOUT_CAP = 2_000_000  # "2 MB"; exact unit unpublished, we use MiB and also warn at 2,000,000


class _NotJson(Exception):
    pass


def _reject_constant(name):
    raise _NotJson(name)


def _parse_int(lit: str):
    return float(lit) if len(lit) > 4300 else int(lit)


def _parse(line: str):
    try:
        return json.loads(line, parse_constant=_reject_constant, parse_int=_parse_int)
    except (ValueError, RecursionError) as e:
        raise _NotJson(str(e)) from e


def _whole(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def duration_ms(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)


def _lines(text: str):
    if text == "":
        return []
    pieces = text.split("\n")
    if text.endswith("\n"):
        pieces.pop()
    return pieces


def evaluate(stdout: bytes, tests: list[dict], commits: list[str]) -> dict:
    """Validate a run.  Returns {"ok": False, "code", "line"} or
    {"ok": True, "plans": {commit: [[...], ...] (occupied workers only)}}."""
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "code": "not_utf8", "line": None}
    known_tests = {t["number"] for t in tests}
    known_commits = set(commits)
    seen = set()
    plans = {}
    for n, line in enumerate(_lines(text), 1):
        try:
            v = _parse(line)
        except _NotJson:
            return {"ok": False, "code": "not_json", "line": n}
        if not (isinstance(v, dict) and set(v.keys()) == {"commit", "workers"}
                and isinstance(v["commit"], str) and isinstance(v["workers"], list)):
            return {"ok": False, "code": "wrong_shape", "line": n}
        c = v["commit"]
        if c not in known_commits:
            return {"ok": False, "code": "unknown_commit", "line": n}
        if c in seen:
            return {"ok": False, "code": "repeated_commit", "line": n}
        seen.add(c)
        workers = []
        for w in v["workers"]:
            if not isinstance(w, list):
                return {"ok": False, "code": "wrong_shape", "line": n}
            ww = []
            for item in w:
                x = _whole(item)
                if x is None:
                    return {"ok": False, "code": "wrong_shape", "line": n}
                ww.append(x)
            workers.append(ww)
        flat = [x for w in workers for x in w]
        if any(x not in known_tests for x in flat):
            return {"ok": False, "code": "unknown_test", "line": n}
        if len(set(flat)) != len(flat):
            return {"ok": False, "code": "repeated_test", "line": n}
        occ = [w for w in workers if w]
        if len(occ) > MAX_WORKERS:
            return {"ok": False, "code": "too_many_workers", "line": n}
        plans[c] = occ
    if any(c not in seen for c in known_commits):
        return {"ok": False, "code": "missing_commit", "line": None}
    return {"ok": True, "plans": plans}


def commit_cost_ms(workers: list[list[int]], dur: dict[int, int]) -> int:
    if not workers:
        return 0
    return max(sum(dur[t] for t in w) for w in workers) + WORKER_MS * len(workers)


def board_score_s(total_us: int) -> float:
    return math.floor((total_us + 50_000) / 100_000) / 10


# ------------------------------------------------------------ reference plans


def lpt(tests: list[int], dur: dict[int, int], k: int) -> list[list[int]]:
    loads = [0] * k
    ws: list[list[int]] = [[] for _ in range(k)]
    for t in sorted(tests, key=lambda x: (-dur[x], x)):
        i = min(range(k), key=lambda j: (loads[j], j))
        ws[i].append(t)
        loads[i] += dur[t]
    return [w for w in ws if w]


def best_schedule(tests: list[int], dur: dict[int, int]) -> tuple[int, list[list[int]]]:
    """Best LPT plan over k=1..8 (cost_ms, workers).  Empty selection costs 0."""
    tests = list(tests)
    if not tests:
        return 0, []
    best = None
    for k in range(1, min(MAX_WORKERS, len(tests)) + 1):
        ws = lpt(tests, dur, k)
        c = commit_cost_ms(ws, dur)
        if best is None or c < best[0]:
            best = (c, ws)
    return best


def lower_bound(tests: list[int], dur: dict[int, int]) -> int:
    """Relax integral scheduling to an average-load and longest-job bound."""
    if not tests:
        return 0
    total = sum(dur[t] for t in tests)
    longest = max(dur[t] for t in tests)
    return min(WORKER_MS * k + max(longest, (total + k - 1) // k)
               for k in range(1, min(len(tests), MAX_WORKERS) + 1))
