"""Entrypoint: read /data, print one JSON line per patch on stdout.

Safety contract: whatever happens in impact selection for a patch (exception,
timeout, missing maps, global deadline), that patch gets the full-suite plan.
Optional runtime verification (``selector.verify``) can only remove tests it
has *seen pass* against the patched source; every ambiguity keeps the static
plan.  Every line is validated before anything is printed.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from .schedule import MAX_WORKERS, duration_ms, plan_cost, schedule

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
# Whole-batch budget for impact selection (practice limit 180 s, final 270 s).
GLOBAL_BUDGET_S = float(os.environ.get("BLAST_BUDGET_S", "60"))
PER_PATCH_S = float(os.environ.get("BLAST_PER_PATCH_S", "5"))
STDOUT_CAP = 2_000_000

# --- verification knobs (see docs/verification.md) ---
VERIFY = os.environ.get("BLAST_VERIFY", "1") != "0"
VERIFY_END_S = float(os.environ.get("BLAST_VERIFY_END_S", "130"))  # stop starting jobs after this
VERIFY_PAR = int(os.environ.get("BLAST_VERIFY_PAR", "2"))
VERIFY_MAX_TEST_S = float(os.environ.get("BLAST_VERIFY_MAX_TEST_S", "3.0"))
VERIFY_PLAUSIBLE_BUDGET_S = float(os.environ.get("BLAST_VERIFY_PLAUSIBLE_S", "1.0"))
VERIFY_P_FAIL = float(os.environ.get("BLAST_VERIFY_P_FAIL", "0.6"))
VERIFY_MIN_GAIN_S = float(os.environ.get("BLAST_VERIFY_MIN_GAIN_S", "0.3"))
CPU_PER_CAT_S = 0.9     # measured local CPU / catalog seconds ~0.75 (conservative)
BASE_CPU_S = 0.15       # fork + fresh sqlglot import + overlay
HEAVY_MODULES = ("tests/test_optimizer.py", "tests/test_executor.py")  # import duckdb/pandas
HEAVY_IMPORT_S = 0.15   # per child, after the parent pre-imported duckdb/pandas
PREWARM_S = 1.0


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


class Patch:
    __slots__ = ("commit", "text", "req", "pl", "workers", "why", "full")

    def __init__(self, commit: str):
        self.commit = commit
        self.text = ""
        self.req: set[int] = set()
        self.pl: set[int] = set()
        self.workers: list[list[int]] | None = None  # None = full suite
        self.why = "full"
        self.full = True


def _plan(req: set[int], pl: set[int], dur: dict[int, int]) -> list[list[int]]:
    ws = [sorted(w) for w in schedule(req, pl, dur) if w]
    covered = {t for w in ws for t in w}
    if not req <= covered:
        raise ValueError("schedule dropped a required test")
    return ws


def _impact_pass(patches, select, catalog, dur, valid, t0) -> None:
    use_alarm = hasattr(signal, "SIGALRM")
    if use_alarm:
        signal.signal(signal.SIGALRM, _alarm)
    for P in patches:
        elapsed = time.monotonic() - t0
        try:
            P.text = (DATA / "commits" / f"{P.commit}.patch").read_text(
                encoding="utf-8", errors="replace")
        except Exception as e:
            P.why = f"fallback (read: {e})"
            continue
        if select is None or elapsed >= GLOBAL_BUDGET_S:
            continue
        try:
            budget = max(0.05, min(PER_PATCH_S, GLOBAL_BUDGET_S - elapsed))
            if use_alarm:
                signal.setitimer(signal.ITIMER_REAL, budget)
            try:
                res = select(P.text, catalog)
            finally:
                if use_alarm:
                    signal.setitimer(signal.ITIMER_REAL, 0)
            if not res.get("full"):
                req = {int(t) for t in res.get("required", ())}
                pl = {int(t) for t in res.get("plausible", ())}
                if not req <= valid:
                    raise ValueError("unknown test numbers in required")
                pl &= valid
                P.workers = _plan(req, pl, dur)
                P.req, P.pl, P.full = req, pl - req, False
                P.why = f"req={len(req)} plaus={len(pl)} sel={sum(map(len, P.workers))}"
            reasons = res.get("reasons")
            if reasons:
                log(f"  {P.commit}: {str(reasons)[:300]}")
        except BaseException as e:  # noqa: BLE001 - includes _Timeout
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            P.workers, P.full = None, True
            P.why = f"fallback ({type(e).__name__}: {str(e)[:120]})"


def _verify_pass(patches, catalog, dur, t0) -> None:
    """Optional: run candidate tests against the patched source and drop the
    ones seen to pass.  Never widens risk: unverified tests stay scheduled."""
    try:
        from . import verify as V
    except Exception as e:  # noqa: BLE001
        log(f"verify unavailable: {e}")
        return
    if not hasattr(os, "fork") or not os.path.isdir(os.path.join(V.PRISTINE, "sqlglot")):
        log("verify: no pristine checkout; skipped")
        return
    by_num = {int(t["number"]): t for t in catalog}
    sec = {n: float(t["seconds"]) for n, t in by_num.items()}

    cands = []
    for P in patches:
        if P.full or not P.req or P.workers is None:
            continue
        static_cost = plan_cost(P.workers, dur) / 1000
        vreq = {t for t in P.req if sec[t] <= VERIFY_MAX_TEST_S}
        unverifiable = P.req - vreq
        after = plan_cost(schedule(unverifiable, set(), dur), dur) / 1000 if unverifiable else 0.0
        # plausible insurance: cheapest first within a small CPU budget
        vpl, spent = [], 0.0
        for t in sorted(P.pl, key=lambda t: (sec[t], t)):
            c = CPU_PER_CAT_S * sec[t]
            if sec[t] > VERIFY_MAX_TEST_S or spent + c > VERIFY_PLAUSIBLE_BUDGET_S:
                break
            vpl.append(t)
            spent += c
        vset = vreq | set(vpl)
        files = {by_num[t]["file"] for t in vset}
        est = (BASE_CPU_S + CPU_PER_CAT_S * sum(sec[t] for t in vset)
               + HEAVY_IMPORT_S * sum(1 for f in files if f in HEAVY_MODULES))
        expected_after = after + VERIFY_P_FAIL * 2.1 if not unverifiable else after + VERIFY_P_FAIL * 0.1
        gain = static_cost - expected_after - est
        if gain < VERIFY_MIN_GAIN_S or not vreq:
            continue
        heavy = any(f in HEAVY_MODULES for f in files)
        cands.append((gain, P, vset, est, heavy))
    if not cands:
        return
    cands.sort(key=lambda c: -c[0])
    if any(c[4] for c in cands):
        try:  # unpatched third-party modules, shared by every forked child
            import duckdb  # noqa: F401
            import numpy  # noqa: F401
            import pandas  # noqa: F401
            import pandas.testing  # noqa: F401
        except Exception as e:  # noqa: BLE001
            log(f"verify: prewarm failed: {e}")
    import unittest  # noqa: F401
    import concurrent.futures  # noqa: F401

    tmp = os.environ.get("TMPDIR") or "/tmp"
    queue = list(cands)
    running: list = []
    n_done = n_empty = 0
    while queue or running:
        now = time.monotonic() - t0
        while queue and len(running) < VERIFY_PAR and now < VERIFY_END_S:
            gain, P, vset, est, heavy = queue.pop(0)
            order = sorted(vset)  # catalog order = file order
            nids = [by_num[t]["id"] for t in order]
            deadline = min(2.0 + 3.0 * est, max(0.5, VERIFY_END_S + 20 - now))
            per_test = 2.0 + 5.0 * max(sec[t] for t in vset)
            try:
                job = V.start(P.text, nids, os.path.join(tmp, f"v{len(running)}_{P.commit}"),
                              deadline, per_test, key=(P, vset))
                running.append(job)
            except Exception as e:  # noqa: BLE001 - patch did not apply etc.
                log(f"  verify {P.commit}: not started ({type(e).__name__}: {str(e)[:100]})")
        if not running:
            break
        for job in V.poll(running, 1.0):
            running.remove(job)
            P, vset = job.key
            try:
                res = V.results(job)
                id2n = {by_num[t]["id"]: t for t in vset}
                passed = {id2n[i] for i, s in res.items() if s == V.PASS and i in id2n}
                failed = {id2n[i] for i, s in res.items() if s == V.FAIL and i in id2n}
                req = (P.req - passed) | failed
                pl = P.pl - passed - failed
                if failed - P.req:
                    # impact under-predicted this patch: keep every unverified
                    # plausible test as required
                    req |= pl
                workers = _plan(req, pl, dur) if req else []
                old = plan_cost(P.workers, dur)
                if plan_cost(workers, dur) <= old or failed - P.req:
                    P.workers = workers
                P.why += (f" | verify {len(passed)}p/{len(failed)}f/"
                          f"{len(vset) - len(passed) - len(failed)}u"
                          f"{' killed' if job.killed else ''} "
                          f"{time.monotonic() - job.t_start:.2f}s -> {sum(map(len, P.workers))} tests")
                n_done += 1
                n_empty += not P.workers
            except Exception as e:  # noqa: BLE001
                log(f"  verify {P.commit}: result error {e}")
    log(f"verify: {n_done}/{len(cands)} verified, {n_empty} proven clean")


def main() -> int:
    t0 = time.monotonic()
    catalog = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    dur = {int(t["number"]): duration_ms(float(t["seconds"])) for t in catalog}
    valid = set(dur)
    full_workers = [sorted(w) for w in schedule(valid, set(), dur) if w]

    patches = [Patch(p.stem) for p in sorted((DATA / "commits").glob("*.patch"))]
    select = _load_impact() if patches else None
    _impact_pass(patches, select, catalog, dur, valid, t0)
    if VERIFY and select is not None:
        try:
            _verify_pass(patches, catalog, dur, t0)
        except Exception as e:  # noqa: BLE001 - static plans stand
            log(f"verify pass crashed: {type(e).__name__}: {e}")

    out_lines: list[str] = []
    seen_commits: set[str] = set()
    n_full = 0
    for P in patches:
        workers = P.workers if P.workers is not None else full_workers
        obj = {"commit": P.commit, "workers": workers}
        try:
            validate_line(obj, valid, seen_commits)
        except AssertionError:
            obj = {"commit": P.commit, "workers": full_workers}
            P.why = "fallback (invalid plan)"
            workers = full_workers
        n_full += workers is full_workers
        seen_commits.add(P.commit)
        out_lines.append(json.dumps(obj, separators=(",", ":")))
        log(f"{P.commit}: {P.why} cost={plan_cost(workers, dur) / 1000:.3f}")

    data = ("\n".join(out_lines) + "\n") if out_lines else ""
    if len(data.encode()) > STDOUT_CAP:  # cannot happen with 1.6k tests, but be safe
        log("stdout too large; this should be impossible")
    sys.stdout.write(data)
    sys.stdout.flush()
    log(f"{len(patches)} patches, {n_full} full-suite, {time.monotonic() - t0:.2f}s wall, "
        f"cpu self {time.process_time():.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
