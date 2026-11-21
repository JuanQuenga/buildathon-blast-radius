#!/usr/bin/env python3
"""Ground-truth labeler: apply a patch to a fresh copy of the pin, run pytest,
and return the official-format label (failing test numbers).

Failure = any failed report for the test (setup/call/teardown, or any failing
subtest), a timeout (pytest-timeout raises in the test), or the test's file
failing to import/collect (all its tests fail).  Catalog tests that never
report (crash, renamed/deleted test) are counted as failing and listed under
``missing`` so they can be audited.

Usage:
  eval/oracle.py label PATCH...            # print labels
  eval/oracle.py label-dir DIR [-j 6]      # DIR/commits/*.patch -> DIR/answers.json (+ DIR/oracle.json)
  eval/oracle.py baseline [-n 2]           # clean runs, report non-passing tests (flakes)
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import BASE, CACHE, CATALOG, EVAL, PYTHON, WORKBENCH  # noqa: E402

ORACLE_VERSION = "v1"
TEST_TIMEOUT_S = int(os.environ.get("BR_TEST_TIMEOUT", "300"))
CASE_TIMEOUT_S = int(os.environ.get("BR_CASE_TIMEOUT", "3600"))
PLUGIN_DIR = EVAL / "oracle_plugin"
# Pin all oracle pytest runs to a core range (duckdb spawns threads); "" disables.
_CORES = os.environ.get("BR_ORACLE_CORES", "10-15")
TASKSET = ["taskset", "-c", _CORES] if _CORES and shutil.which("taskset") else []
COPY_ITEMS = None  # everything except .git


def load_catalog(path: Path = CATALOG):
    d = json.loads(Path(path).read_text())
    return d["tests"], set(d.get("skipped", []))


def _copy_base(dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in BASE.iterdir():
        if item.name == ".git":
            continue
        subprocess.run(
            ["cp", "-a", "--reflink=auto", str(item), str(dst / item.name)], check=True
        )


def _apply(patch: bytes, work: Path) -> tuple[bool, str]:
    p = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        input=patch,
        cwd=work,
        capture_output=True,
    )
    return p.returncode == 0, p.stderr.decode(errors="replace")


def _tmpdir() -> str:
    d = WORKBENCH / "tmp"  # same btrfs volume as BASE, so cp --reflink is free
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def patch_key(patch: bytes) -> str:
    return hashlib.sha256(ORACLE_VERSION.encode() + b"\0" + patch).hexdigest()[:24]


def run_pytest(work: Path, out: Path, log: Path) -> dict:
    env = {
        # test_dialect.py::test_lazy_load shells out to `python`: make that the 3.13 venv
        "PATH": f"{PYTHON.parent}:" + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(work),
        "LANG": "C.UTF-8",
        "SKIP_INTEGRATION": "0",
        "PYTHONPATH": f"{work}:{PLUGIN_DIR}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "BR_ORACLE_OUT": str(out),
        "TMPDIR": str(work / ".tmp"),
    }
    (work / ".tmp").mkdir(exist_ok=True)
    cmd = TASKSET + [
        str(PYTHON), "-m", "pytest", "tests",
        "-p", "no:cacheprovider", "-p", "br_oracle_plugin",
        "-q", "-rN", "--no-header", "--tb=short",
        f"--timeout={TEST_TIMEOUT_S}",
        # without this a collection error in ANY file aborts the whole session; the official
        # rule is "unimportable test file -> all tests in that file fail", others still run
        "--continue-on-collection-errors",
    ]
    t0 = time.time()
    with open(log, "wb") as lf:
        try:
            p = subprocess.run(cmd, cwd=work, env=env, stdout=lf, stderr=subprocess.STDOUT,
                               timeout=CASE_TIMEOUT_S)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc = "case_timeout"
    return {"rc": rc, "wall": round(time.time() - t0, 2)}


def interpret(out: Path, tests: list[dict], skipped: set[str]) -> dict:
    recs = []
    if out.exists():
        for line in out.read_text(errors="replace").splitlines():
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass
    ended = any(r["t"] == "end" for r in recs)
    failed: dict[str, list[str]] = {}
    reported: set[str] = set()
    durations: dict[str, float] = {}
    collect_errors: list[dict] = []
    for r in recs:
        if r["t"] == "collect":
            collect_errors.append({"nodeid": r["nodeid"], "msg": r.get("msg")})
        elif r["t"] == "test":
            nid = r["nodeid"]
            reported.add(nid)
            if r["when"] == "call" and not r["sub"]:
                durations[nid] = r["duration"]
            if r["outcome"] == "failed":
                why = ("subtest" if r["sub"] else r["when"]) + ": " + (r.get("msg") or "")[-300:]
                failed.setdefault(nid, []).append(why)
    by_id = {t["id"]: t for t in tests}
    failing: set[int] = set()
    reasons: dict[str, str] = {}
    for nid, why in failed.items():
        if nid in by_id:
            failing.add(by_id[nid]["number"])
            reasons[nid] = why[0]
    for ce in collect_errors:
        node = ce["nodeid"]
        for t in tests:
            if node in ("", ".", "tests") or t["id"] == node or t["id"].startswith(node + "::") \
                    or t["file"] == node:
                if t["number"] not in failing:
                    failing.add(t["number"])
                    reasons[t["id"]] = "collect: " + (ce["msg"] or "")[-300:]
    missing = []
    for t in tests:
        if t["id"] not in reported and t["number"] not in failing:
            missing.append(t["id"])
            failing.add(t["number"])
            reasons[t["id"]] = "missing: no report (crash, rename or deletion)"
    slow = {k: v for k, v in durations.items() if v >= 1.0}
    return {
        "failing": sorted(failing),
        "ended": ended,
        "collect_errors": collect_errors,
        "missing": missing,
        "reasons": reasons,
        "slow": slow,
        "n_reported": len(reported),
        "unknown_failed": sorted(n for n in failed if n not in by_id and n not in skipped),
    }


def label(patch: bytes, tests, skipped, *, keep_log: bool = True, use_cache: bool = True) -> dict:
    key = patch_key(patch)
    cache_file = CACHE / f"{key}.json"
    if use_cache and cache_file.exists():
        return json.loads(cache_file.read_text())
    CACHE.mkdir(parents=True, exist_ok=True)
    logs = WORKBENCH / "oracle-logs"
    logs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="br-oracle-", dir=_tmpdir()) as td:
        work = Path(td) / "sqlglot"
        _copy_base(work)
        ok, err = _apply(patch, work)
        if not ok:
            res = {"status": "apply_failed", "error": err[-1000:], "failing": None, "key": key}
        else:
            out = Path(td) / "reports.jsonl"
            log = logs / f"{key}.log"
            run = run_pytest(work, out, log)
            res = interpret(out, tests, skipped)
            # A pytest-timeout on the shared machine is usually load, not the patch
            # (test_max_nodes took 184 s instead of 29 s at load 58): rerun once and keep
            # the timeout only if it reproduces.
            timed_out = sorted(t for t, why in res["reasons"].items() if "Timeout (>" in why)
            if timed_out:
                out.unlink(missing_ok=True)
                run = run_pytest(work, out, log)
                res2 = interpret(out, tests, skipped)
                res2["timeout_first_run"] = timed_out
                res2["timeout_reproduced"] = sorted(t for t, why in res2["reasons"].items() if "Timeout (>" in why)
                res = res2
            res.update(run)
            res["key"] = key
            if run["rc"] == "case_timeout":
                res["status"] = "case_timeout"
            elif not res["ended"]:
                res["status"] = "crash"
            else:
                res["status"] = "ok"
            if not keep_log:
                log.unlink(missing_ok=True)
    cache_file.write_text(json.dumps(res))
    return res


def label_many(patches: dict[str, bytes], jobs: int = 6, use_cache: bool = True, quiet=False,
               on_result=None) -> dict[str, dict]:
    tests, skipped = load_catalog()
    results: dict[str, dict] = {}
    todo = {}
    for cid, pb in patches.items():
        cf_ = CACHE / f"{patch_key(pb)}.json"
        if use_cache and cf_.exists():
            results[cid] = json.loads(cf_.read_text())
        else:
            todo[cid] = pb
    if not quiet:
        print(f"[oracle] {len(results)} cached, {len(todo)} to run with -j{jobs}", file=sys.stderr)
    if on_result:
        on_result(results)
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(label, pb, tests, skipped, use_cache=use_cache): cid for cid, pb in todo.items()}
        for i, f in enumerate(cf.as_completed(futs), 1):
            cid = futs[f]
            try:
                results[cid] = f.result()
            except Exception as e:  # pragma: no cover
                results[cid] = {"status": "oracle_error", "error": repr(e), "failing": None}
            r = results[cid]
            if not quiet:
                print(f"[oracle] {i}/{len(todo)} {cid}: {r['status']} fail={None if r['failing'] is None else len(r['failing'])}"
                      f" wall={r.get('wall')} ({time.time()-t0:.0f}s)", file=sys.stderr, flush=True)
            if on_result and (i % 6 == 0 or i == len(todo)):
                on_result(results)
    return results


def _cmd_label_dir(a):
    d = Path(a.dir)
    meta_path = d / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    patches = {p.stem: p.read_bytes() for p in sorted((d / "commits").glob("*.patch"))}
    if a.only:
        patches = {k: v for k, v in patches.items() if k in set(a.only.split(","))}
    ans_path = d / "answers.json"
    # bulky per-test failure reasons stay outside the repo
    (WORKBENCH / "oracle-details").mkdir(parents=True, exist_ok=True)
    oracle_path = WORKBENCH / "oracle-details" / f"{d.resolve().name}.json"
    answers = json.loads(ans_path.read_text())["commits"] if ans_path.exists() else {}
    oracle = json.loads(oracle_path.read_text()) if oracle_path.exists() else {}
    bad = {}

    def write(res):
        for cid, r in res.items():
            if r.get("status") != "ok" or r.get("rc") not in (0, 1, 5):
                bad[cid] = r["status"]
                answers.pop(cid, None)
                oracle.pop(cid, None)
                continue
            m = meta.get(cid, {})
            answers[cid] = {"failing": r["failing"], "kind": m.get("kind", "unknown")}
            oracle[cid] = {k: r.get(k) for k in ("status", "key", "missing", "collect_errors", "rc", "wall", "slow")}
            oracle[cid]["reasons"] = r.get("reasons", {})
        # answers.json is written atomically so readers never see a partial file
        tmp = ans_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"commits": dict(sorted(answers.items()))}, separators=(",", ":")) + "\n")
        tmp.replace(ans_path)
        tmp = oracle_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(sorted(oracle.items())), indent=0) + "\n")
        tmp.replace(oracle_path)

    label_many(patches, jobs=a.jobs, use_cache=not a.no_cache, on_result=write)
    for cid, st in bad.items():
        print(f"[oracle] UNLABELED {cid}: {st}", file=sys.stderr)
    print(f"[oracle] {len(answers)} answers in {ans_path}", file=sys.stderr)


def _cmd_label(a):
    tests, skipped = load_catalog()
    for p in a.patches:
        r = label(Path(p).read_bytes(), tests, skipped, use_cache=not a.no_cache)
        print(json.dumps({"patch": p, "status": r["status"], "failing": r["failing"],
                          "reasons": r.get("reasons"), "missing": r.get("missing")}, indent=1))


def _cmd_baseline(a):
    tests, skipped = load_catalog()
    # identical empty patches defeat the cache, so salt them with a no-op header
    patches = {f"clean{i}": f"# clean run {i}\n".encode() for i in range(a.n)}
    res = {}
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = {ex.submit(_label_clean, i, tests, skipped): i for i in range(a.n)}
        for f in cf.as_completed(futs):
            i = futs[f]
            res[i] = f.result()
    by_num = {t["number"]: t for t in tests}
    for i, r in sorted(res.items()):
        print(f"clean run {i}: status={r['status']} wall={r.get('wall')} failing={len(r['failing'])}")
        for n in r["failing"]:
            print("   ", by_num[n]["id"], "|", r["reasons"].get(by_num[n]["id"], "")[:200].replace("\n", " "))
    (WORKBENCH / "baseline.json").write_text(json.dumps(res, indent=1))


def _label_clean(i, tests, skipped):
    with tempfile.TemporaryDirectory(prefix="br-clean-", dir=_tmpdir()) as td:
        work = Path(td) / "sqlglot"
        _copy_base(work)
        out = Path(td) / "reports.jsonl"
        logs = WORKBENCH / "oracle-logs"
        logs.mkdir(parents=True, exist_ok=True)
        run = run_pytest(work, out, logs / f"clean{i}.log")
        r = interpret(out, tests, skipped)
        r.update(run)
        r["status"] = "ok" if r["ended"] else "crash"
        r["durations_all"] = None
        return r


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("label")
    p.add_argument("patches", nargs="+")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(fn=_cmd_label)
    p = sub.add_parser("label-dir")
    p.add_argument("dir")
    p.add_argument("-j", "--jobs", type=int, default=6)
    p.add_argument("--only")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(fn=_cmd_label_dir)
    p = sub.add_parser("baseline")
    p.add_argument("-n", type=int, default=2)
    p.add_argument("-j", "--jobs", type=int, default=2)
    p.set_defaults(fn=_cmd_baseline)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
