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
# 0: off | collect: pytest-collection check only | insure: + run cheap plausible
# tests (can only ADD tests) | drop: + drop required tests seen to pass.
VERIFY_MODE = os.environ.get("BLAST_VERIFY", "0")
VERIFY = VERIFY_MODE in ("collect", "insure", "drop")
COLLECT_MODULES = os.environ.get("BLAST_COLLECT_MODULES", "/app/collect_modules.json")
VERIFY_END_S = float(os.environ.get("BLAST_VERIFY_END_S", "130"))  # stop starting jobs after this
VERIFY_PAR = int(os.environ.get("BLAST_VERIFY_PAR", "2"))
VERIFY_MAX_TEST_S = float(os.environ.get("BLAST_VERIFY_MAX_TEST_S", "3.0"))
VERIFY_PLAUSIBLE_BUDGET_S = float(os.environ.get("BLAST_VERIFY_PLAUSIBLE_S", "1.0"))
VERIFY_P_CLEAN = float(os.environ.get("BLAST_VERIFY_P_CLEAN", "0.3"))
VERIFY_MIN_GAIN_S = float(os.environ.get("BLAST_VERIFY_MIN_GAIN_S", "0.3"))
CPU_PER_CAT_S = 0.9     # measured local CPU / catalog seconds ~0.75 (conservative)
BASE_CPU_S = 0.15       # fork + fresh sqlglot import + overlay
HEAVY_MODULES = ("tests/test_optimizer.py", "tests/test_executor.py")  # import duckdb/pandas
HEAVY_IMPORT_S = 0.15   # per child, after the parent pre-imported duckdb/pandas
PREWARM_S = 1.0
COLLECT_S = 0.3         # importing every test module (pytest collection)


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
    __slots__ = ("commit", "text", "req", "pl", "workers", "why", "full", "alt", "missing")

    def __init__(self, commit: str):
        self.commit = commit
        self.text = ""
        self.req: set[int] = set()
        self.pl: set[int] = set()
        self.workers: list[list[int]] | None = None  # None = full suite
        self.why = "full"
        self.full = True
        self.missing = set()
        self.alt = None  # (required, plausible) valid only if collection succeeds


def _plan(req: set[int], pl: set[int], dur: dict[int, int], *, free_riders=True) -> list[list[int]]:
    ws = [sorted(w) for w in schedule(req, pl, dur, free_riders=free_riders) if w]
    covered = {t for w in ws for t in w}
    if not req <= covered:
        raise ValueError("schedule dropped a required test")
    return ws


def _impact_pass(patches, select, catalog, dur, valid, t0) -> None:
    cpu_start = time.process_time()
    use_alarm = hasattr(signal, "SIGPROF")
    if use_alarm:
        signal.signal(signal.SIGPROF, _alarm)
        signal.signal(signal.SIGALRM, _alarm)
    for P in patches:
        elapsed = time.process_time() - cpu_start
        try:
            P.text = (DATA / "commits" / f"{P.commit}.patch").read_text(
                encoding="utf-8", errors="replace")
        except Exception as e:
            P.why = f"fallback (read: {e})"
            continue
        wall_left = t0 + 1.5 * GLOBAL_BUDGET_S - time.monotonic()
        if select is None or elapsed >= GLOBAL_BUDGET_S or wall_left <= 0:
            continue
        try:
            budget = max(0.05, min(PER_PATCH_S, GLOBAL_BUDGET_S - elapsed))
            if use_alarm:
                signal.setitimer(signal.ITIMER_REAL, min(4 * budget, wall_left))
                signal.setitimer(signal.ITIMER_PROF, budget)
            try:
                res = select(P.text, catalog)
            finally:
                if use_alarm:
                    signal.setitimer(signal.ITIMER_PROF, 0)
                    signal.setitimer(signal.ITIMER_REAL, 0)
            alt = res.get("if_collects") if res.get("full") else None
            if alt and (VERIFY or os.environ.get("BLAST_HEAVY") == "1"):
                # impact went full only for fear of a collection error: keep
                # the narrower plan as a candidate, used only if the
                # verifier's collection check passes (see _verify_pass)
                areq = {int(t) for t in alt.get("required", ())}
                apl = {int(t) for t in alt.get("plausible", ())} & valid
                if areq <= valid:
                    P.alt = (areq, apl - areq)
            if not res.get("full"):
                req = {int(t) for t in res.get("required", ())}
                pl = {int(t) for t in res.get("plausible", ())}
                if not req <= valid:
                    raise ValueError("unknown test numbers in required")
                pl &= valid
                P.workers = _plan(req, pl, dur, free_riders=not res.get("precise_fixture"))
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

    collect_files = None
    try:
        with open(COLLECT_MODULES, encoding="utf-8") as f:
            collect_files = set(json.load(f))
    except Exception:  # noqa: BLE001 - unknown: check every patch
        pass

    def needs_collect(P) -> bool:
        try:
            from .patch import parse
            fds = parse(P.text)
        except Exception:  # noqa: BLE001
            return True
        for fd in fds:
            for path in fd.paths:
                if not path.endswith(".py"):
                    continue
                if fd.is_new or fd.is_deleted or fd.is_rename:
                    return True
                if collect_files is None or path in collect_files:
                    return True
        return False

    cands = []
    for P in patches:
        if P.full and P.alt is not None:
            # collection check only; on success switch to the narrower plan
            cands.append((36.8, P, set(), BASE_CPU_S + COLLECT_S, True, True, False))
            continue
        if P.full or P.workers is None:
            continue
        static_cost = plan_cost(P.workers, dur) / 1000
        coll = needs_collect(P)
        vreq: set[int] = set()
        if VERIFY_MODE == "drop" and P.req:
            vreq = {t for t in P.req if sec[t] <= VERIFY_MAX_TEST_S}
        vpl: list[int] = []
        if VERIFY_MODE in ("insure", "drop"):
            spent = 0.0
            for t in sorted(P.pl, key=lambda t: (sec[t], t)):
                c = CPU_PER_CAT_S * sec[t]
                if sec[t] > VERIFY_MAX_TEST_S or spent + c > VERIFY_PLAUSIBLE_BUDGET_S:
                    break
                vpl.append(t)
                spent += c
        vset = vreq | set(vpl)
        if not vset and not coll:
            continue
        files = {by_num[t]["file"] for t in vset}
        est = (BASE_CPU_S + CPU_PER_CAT_S * sum(sec[t] for t in vset)
               + HEAVY_IMPORT_S * sum(1 for f in files if f in HEAVY_MODULES)
               + (COLLECT_S if coll else 0.0))
        heavy = any(f in HEAVY_MODULES for f in files) or coll
        if vreq:
            unverifiable = P.req - vreq
            after = plan_cost(schedule(unverifiable, set(), dur), dur) / 1000 if unverifiable else 0.0
            gain = VERIFY_P_CLEAN * (static_cost - after) - est
            if gain < VERIFY_MIN_GAIN_S:
                vreq = set()  # not worth dropping; still insure below
                vset = set(vpl)
                if not vset and not coll:
                    continue
        cands.append((static_cost, P, vset, est, heavy, coll, bool(vreq)))
    if not cands:
        return
    cands.sort(key=lambda c: -c[0])  # most expensive static plans first
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
            _, P, vset, est, heavy, coll, dropping = queue.pop(0)
            order = sorted(vset)  # catalog order = file order
            nids = [by_num[t]["id"] for t in order]
            deadline = min(2.0 + 3.0 * est, max(0.5, VERIFY_END_S + 20 - now))
            per_test = 2.0 + 5.0 * max([sec[t] for t in vset] or [0.0])
            try:
                job = V.start(P.text, nids, os.path.join(tmp, f"v_{P.commit}"),
                              deadline, per_test, key=(P, vset, coll, dropping), collect=coll)
                running.append(job)
            except Exception as e:  # noqa: BLE001 - patch did not apply etc.
                log(f"  verify {P.commit}: not started ({type(e).__name__}: {str(e)[:100]})")
        if not running:
            break
        for job in V.poll(running, 1.0):
            running.remove(job)
            P, vset, coll, dropping = job.key
            try:
                res = V.results(job)
                if coll and res.get(V.COLLECT) == V.COLLECT_ERROR:
                    P.workers = None  # pytest would error in collection: everything fails
                    P.full = True
                    P.why += " | verify: COLLECTION ERROR -> full suite"
                    n_done += 1
                    continue
                coll_ok = (not coll) or res.get(V.COLLECT) == V.PASS
                if P.full and P.alt is not None:
                    if coll_ok and not job.killed:
                        areq, apl = P.alt
                        P.workers = _plan(areq, apl, dur) if areq else []
                        P.req, P.pl, P.full = areq, apl, False
                        P.why += f" | collection ok -> narrowed to {sum(map(len, P.workers))} tests"
                    n_done += 1
                    continue
                id2n = {by_num[t]["id"]: t for t in vset}
                passed = {id2n[i] for i, s in res.items() if s == V.PASS and i in id2n}
                failed = {id2n[i] for i, s in res.items() if s == V.FAIL and i in id2n}
                req = set(P.req) | failed
                if dropping and coll_ok and not job.killed:
                    req -= passed - failed
                pl = P.pl - passed - failed
                if failed - P.req:
                    # impact under-predicted this patch: keep every unverified
                    # plausible test as required
                    req |= pl
                if req != P.req:
                    P.workers = _plan(req, pl, dur) if req else []
                P.why += (f" | verify{' +coll' if coll else ''}{'(ok)' if coll and coll_ok else ''} "
                          f"{len(passed)}p/{len(failed)}f/"
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
    full_workers = None

    def full_plan():
        nonlocal full_workers
        if full_workers is None:
            full_workers = [sorted(w) for w in schedule(valid, set(), dur) if w]
        return full_workers

    patches = [Patch(p.stem) for p in sorted((DATA / "commits").glob("*.patch"))]
    select = _load_impact() if patches else None
    _impact_pass(patches, select, catalog, dur, valid, t0)
    if os.environ.get("BLAST_HEAVY") == "1":
        from . import heavy
        try:
            (heavy.run if VERIFY else heavy.bounded_run)(patches, catalog, dur, t0)
        except Exception as exc:
            log(f"heavy verification stopped ({type(exc).__name__})")
    if VERIFY and select is not None:
        try:
            _verify_pass(patches, catalog, dur, t0)
        except Exception as e:  # noqa: BLE001 - static plans stand
            log(f"verify pass crashed: {type(e).__name__}: {e}")

    out_lines: list[str] = []
    seen_commits: set[str] = set()
    n_full = 0
    for P in patches:
        workers = P.workers if P.workers is not None else full_plan()
        obj = {"commit": P.commit, "workers": workers}
        try:
            validate_line(obj, valid, seen_commits)
        except AssertionError:
            obj = {"commit": P.commit, "workers": full_plan()}
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
