"""Worker scheduling against the scorer's cost function.

cost(workers) = max(load(w) for nonempty w) + SETUP_MS * (#nonempty workers),
with loads in whole milliseconds (``duration_ms``).  An empty plan costs 0.

``schedule`` packs the *required* tests optimally-ish (LPT + local search,
exact branch-and-bound when small) over k = 1..8 workers, then fills the
slack below the makespan with *plausible* tests and finally with any other
catalog test ("free riders"): a test that fits under the makespan of an
already-open worker costs nothing and can only reduce the chance of a miss.
"""

from __future__ import annotations

import math
from typing import Iterable

SETUP_MS = 2000
MAX_WORKERS = 8


def duration_ms(seconds: float) -> int:
    """Same rounding as the official scorer (JS Math.round)."""
    return math.floor(seconds * 1000 + 0.5)


def plan_cost(workers: list[list[int]], dur: dict[int, int]) -> int:
    loads = [sum(dur[t] for t in w) for w in workers if w]
    if not loads:
        return 0
    return max(loads) + SETUP_MS * len(loads)


# --------------------------------------------------------------------------
# Packing a fixed set onto k workers, minimising the makespan.


def _lpt(items: list[tuple[int, int]], k: int) -> tuple[list[list[int]], list[int]]:
    """items: (ms, test) sorted by ms desc."""
    bins: list[list[int]] = [[] for _ in range(k)]
    loads = [0] * k
    for ms, t in items:
        i = min(range(k), key=loads.__getitem__)
        bins[i].append(t)
        loads[i] += ms
    return bins, loads


def _improve(bins: list[list[int]], loads: list[int], dur: dict[int, int], lb: int,
             max_rounds: int = 200) -> None:
    """Move/swap from the heaviest bin while it strictly lowers its load and
    does not create a new bin at least as heavy.  In place."""
    k = len(bins)
    for _ in range(max_rounds):
        hi = max(range(k), key=loads.__getitem__)
        top = loads[hi]
        if top <= lb:
            return
        best = None  # (new_max_of_pair, hi_idx, j, a, b)
        for j in range(k):
            if j == hi:
                continue
            for a in bins[hi]:
                da = dur[a]
                # move a -> j
                nm = max(top - da, loads[j] + da)
                if nm < top and (best is None or nm < best[0]):
                    best = (nm, j, a, None)
                for b in bins[j]:
                    db = dur[b]
                    if db >= da:
                        continue
                    nm = max(top - da + db, loads[j] + da - db)
                    if nm < top and (best is None or nm < best[0]):
                        best = (nm, j, a, b)
        if best is None:
            return
        _, j, a, b = best
        bins[hi].remove(a)
        bins[j].append(a)
        loads[hi] -= dur[a]
        loads[j] += dur[a]
        if b is not None:
            bins[j].remove(b)
            bins[hi].append(b)
            loads[j] -= dur[b]
            loads[hi] += dur[b]


def _exact(items: list[tuple[int, int]], k: int, ub: int, lb: int,
           node_budget: int = 15_000) -> tuple[list[list[int]], int] | None:
    """Branch and bound for makespan < ub.  Returns (bins, makespan) or None."""
    n = len(items)
    loads = [0] * k
    assign = [0] * n
    best: list = [ub, None]
    nodes = [0]
    suffix = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = suffix[i + 1] + items[i][0]

    def rec(i: int, cur_max: int) -> bool:
        nodes[0] += 1
        if nodes[0] > node_budget:
            return True  # abort
        if i == n:
            if cur_max < best[0]:
                best[0] = cur_max
                best[1] = list(assign)
            return best[0] <= lb
        ms = items[i][0]
        seen = set()
        for b in range(k):
            l = loads[b]
            if l in seen:
                continue  # symmetric
            seen.add(l)
            nl = l + ms
            if nl >= best[0]:
                continue
            # remaining work must fit somewhere below best
            if (sum(loads) + suffix[i]) > k * (best[0] - 1):
                return False
            loads[b] = nl
            assign[i] = b
            if rec(i + 1, max(cur_max, nl)):
                loads[b] = l
                return True
            loads[b] = l
        return False

    rec(0, 0)
    if best[1] is None:
        return None
    bins: list[list[int]] = [[] for _ in range(k)]
    for (ms, t), b in zip(items, best[1]):
        bins[b].append(t)
    return bins, best[0]


def pack(tests: Iterable[int], k: int, dur: dict[int, int]) -> tuple[list[list[int]], int]:
    """Pack tests onto exactly k (possibly some empty) bins; return (bins, makespan)."""
    items = sorted(((dur[t], t) for t in tests), key=lambda x: (-x[0], x[1]))
    if not items:
        return [[] for _ in range(k)], 0
    total = sum(ms for ms, _ in items)
    lb = max(items[0][0], -(-total // k))
    bins, loads = _lpt(items, k)
    if max(loads) > lb:
        _improve(bins, loads, dur, lb)
    ms = max(loads)
    # Exact search only when the possible gain is worth its CPU (1 ms CPU ~ 1 ms score).
    if ms - lb >= 20 and len(items) <= 40 and k > 1:
        res = _exact(items, k, ms, lb)
        if res is not None and res[1] < ms:
            bins, ms = res
    return bins, ms


# --------------------------------------------------------------------------


def _fill(bins: list[list[int]], cap: int, candidates: list[int],
          dur: dict[int, int]) -> None:
    """Greedily add candidates (in priority order) to open bins without any
    bin exceeding cap.  Best fit: tightest bin that still fits."""
    loads = [sum(dur[t] for t in b) for b in bins]
    for t in candidates:
        d = dur[t]
        best = -1
        best_room = None
        for i, l in enumerate(loads):
            room = cap - l - d
            if room >= 0 and (best_room is None or room < best_room):
                best, best_room = i, room
        if best >= 0:
            bins[best].append(t)
            loads[best] += d


def schedule(required: set[int], plausible: set[int], duration_ms: dict[int, int],
             *, free_riders: bool = True,
             plausible_order: list[int] | None = None) -> list[list[int]]:
    """Return nonempty workers covering all ``required`` tests at minimum cost,
    padded at zero extra cost with plausible tests then (if ``free_riders``)
    any other test in ``duration_ms``.

    If ``required`` is empty the plan is empty (cost 0), whatever ``plausible``
    holds: plausible tests are only ever taken for free.
    """
    dur = duration_ms
    req = [t for t in required if t in dur]
    if not req:
        return []
    n = len(req)
    if plausible_order is not None:
        pl = [t for t in plausible_order if t in dur and t not in required]
        seen = set(pl)
        pl += sorted((t for t in plausible if t in dur and t not in required and t not in seen),
                     key=lambda t: (dur[t], t))
    else:
        pl = sorted((t for t in plausible if t in dur and t not in required),
                    key=lambda t: (dur[t], t))
    pl_set = set(pl)
    rest = []
    if free_riders:
        rest = sorted((t for t in dur if t not in required and t not in pl_set),
                      key=lambda t: (dur[t], t))

    total = sum(dur[t] for t in req)
    longest = max(dur[t] for t in req)
    best = None  # (cost, -plausible_covered, -total_covered, k, bins)
    for k in range(1, min(MAX_WORKERS, n) + 1):
        if best is not None and max(longest, -(-total // k)) + SETUP_MS * k > best[0]:
            continue  # cannot match the best cost with k workers
        bins, ms = pack(req, k, dur)
        bins = [b for b in bins if b]
        cost = ms + SETUP_MS * len(bins)
        if best is not None and cost > best[0]:
            continue
        _fill(bins, ms, pl, dur)
        covered_pl = sum(len(b) for b in bins) - n
        if rest:
            _fill(bins, ms, rest, dur)
        selected_count = sum(len(b) for b in bins)
        key = (cost, -covered_pl, -selected_count)
        if best is None or key < best[0:3]:
            best = (cost, -covered_pl, -selected_count, k, bins)
    return best[4]
