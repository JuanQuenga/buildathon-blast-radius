"""Entrypoint: read /data, print one JSON line per patch on stdout.

Safety contract: whatever happens in impact selection for a patch (exception,
timeout, missing maps, global deadline), that patch gets the full-suite plan.
Every line is validated before anything is printed.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from .schedule import MAX_WORKERS, duration_ms, schedule

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
# Whole-batch budget for *selection* (practice limit 180 s, final 270 s).
GLOBAL_BUDGET_S = float(os.environ.get("BLAST_BUDGET_S", "60"))
PER_PATCH_S = float(os.environ.get("BLAST_PER_PATCH_S", "5"))
STDOUT_CAP = 2_000_000


def log(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


class _Timeout(Exception):
    pass


def _alarm(signum, frame):  # pragma: no cover
    raise _Timeout()


def _load_impact():
    try:
        from . import impact  # noqa: WPS433
        return impact.select
    except Exception as e:  # missing module, broken maps, ...
        log(f"impact unavailable ({type(e).__name__}: {e}); full suite everywhere")
        return None


def validate_line(obj, valid_tests: set[int], seen_commits: set[str]) -> None:
    assert isinstance(obj, dict) and set(obj) == {"commit", "workers"}
    c = obj["commit"]
    assert isinstance(c, str) and c not in seen_commits
    ws = obj["workers"]
    assert isinstance(ws, list)
    nonempty = 0
    seen: set[int] = set()
    for w in ws:
        assert isinstance(w, list)
        if w:
            nonempty += 1
        for t in w:
            assert type(t) is int and t in valid_tests and t not in seen
            seen.add(t)
    assert nonempty <= MAX_WORKERS


def main() -> int:
    t0 = time.monotonic()
    catalog = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    dur = {int(t["number"]): duration_ms(float(t["seconds"])) for t in catalog}
    valid = set(dur)
    full = [w for w in schedule(valid, set(), dur) if w]
    full_line_workers = [sorted(w) for w in full]

    patches = sorted((DATA / "commits").glob("*.patch"))
    select = _load_impact() if patches else None
    use_alarm = hasattr(signal, "SIGALRM")
    if use_alarm:
        signal.signal(signal.SIGALRM, _alarm)

    out_lines: list[str] = []
    seen_commits: set[str] = set()
    n_full = 0
    for p in patches:
        commit = p.stem
        workers = full_line_workers
        why = "full"
        elapsed = time.monotonic() - t0
        if select is not None and elapsed < GLOBAL_BUDGET_S:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                budget = max(0.05, min(PER_PATCH_S, GLOBAL_BUDGET_S - elapsed))
                if use_alarm:
                    signal.setitimer(signal.ITIMER_REAL, budget)
                try:
                    res = select(text, catalog)
                finally:
                    if use_alarm:
                        signal.setitimer(signal.ITIMER_REAL, 0)
                if not res.get("full"):
                    req = {int(t) for t in res.get("required", ())}
                    pl = {int(t) for t in res.get("plausible", ())}
                    if not req <= valid:
                        raise ValueError("unknown test numbers in required")
                    ws = [sorted(w) for w in schedule(req, pl & valid, dur) if w]
                    covered = {t for w in ws for t in w}
                    if not req <= covered:
                        raise ValueError("schedule dropped a required test")
                    workers = ws
                    why = f"req={len(req)} plaus={len(pl)} sel={len(covered)}"
                reasons = res.get("reasons")
                if reasons:
                    log(f"  {commit}: {str(reasons)[:300]}")
            except BaseException as e:  # noqa: BLE001 - includes _Timeout
                if isinstance(e, (KeyboardInterrupt, SystemExit)) and not isinstance(e, _Timeout):
                    raise
                workers = full_line_workers
                why = f"fallback ({type(e).__name__}: {str(e)[:120]})"
        if workers is full_line_workers:
            n_full += 1
        obj = {"commit": commit, "workers": workers}
        try:
            validate_line(obj, valid, seen_commits)
        except AssertionError:
            obj = {"commit": commit, "workers": full_line_workers}
            why = "fallback (invalid plan)"
            n_full += 1
        seen_commits.add(commit)
        out_lines.append(json.dumps(obj, separators=(",", ":")))
        log(f"{commit}: {why}")

    data = ("\n".join(out_lines) + "\n") if out_lines else ""
    if len(data.encode()) > STDOUT_CAP:  # cannot happen with 1.6k tests, but be safe
        log("stdout too large; this should be impossible")
    sys.stdout.write(data)
    sys.stdout.flush()
    log(f"{len(patches)} patches, {n_full} full-suite, {time.monotonic() - t0:.2f}s wall, "
        f"cpu {time.process_time():.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
