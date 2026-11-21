#!/usr/bin/env python3
"""Run a selector on labelled case sets the way the scorer does, and score it.

  eval/run.py --cases dev,revert,exotic --split dev            # whole pool, in batches of 40
  eval/run.py --cases sample                                   # the official 30 (official labels)
  eval/run.py --cases holdout,exotic,revert --split holdout    # frozen holdout (don't tune on it!)
  eval/run.py --cases dev --sample 40 --reps 300 --mix practice  # score distribution of 40-patch batches
  eval/run.py --cases dev,exotic,revert --sample 60 --mix final --limit-s 270
  eval/run.py --selector "python3 /path/starter/selector.py" ...  # any command

The selector is run with cwd=--cwd (default: repo root), env BLAST_DATA=<batch dir>
(holding commits/*.patch and tests.json, read-only), BLAST_SQLGLOT=<read-only pin
checkout>, HOME/TMPDIR=<fresh temp dir>, pinned to 2 cores with taskset, killed
at --limit-s (180 practice / 270 final).  CPU = user+sys of the process tree
(rusage of the waited child, which includes all reaped descendants).

Reports: simulated s, CPU s, misses (ids, kinds, missed tests + files),
per-kind/per-family breakdowns, oracle-optimal lower bound and the full-suite
baseline (k=4 LPT, 36.817 s/patch).  ``--json`` saves everything.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paths import BASE, CASES, CATALOG, PYTHON, REPO, STARTER, WORKBENCH  # noqa: E402
import score_lib as S  # noqa: E402

PRACTICE_KINDS = {"code": 16, "fixture": 8, "comment": 3, "multi": 3}  # the public sample's mix
FINAL_EXTRA_KINDS = ["test", "newfile", "delete", "rename", "nonpy", "export", "syntax",
                     "importcycle", "conftest", "revert"]


# ------------------------------------------------------------------ cases


def load_cases(sets: list[str], split: str, kinds: set[str] | None, ignore_missing: bool = False):
    cases = {}
    id2num = {t["id"]: t["number"] for t in json.loads(Path(CATALOG).read_text())["tests"]}
    for s in sets:
        d = Path(s) if "/" in s else CASES / s
        ans_path = d / "answers.json"
        if not ans_path.exists():
            print(f"[run] {d}: no answers.json (label it with eval/oracle.py label-dir)", file=sys.stderr)
            continue
        answers = json.loads(ans_path.read_text())["commits"]
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        det_p = WORKBENCH / "oracle-details" / f"{d.name}.json"
        details = json.loads(det_p.read_text()) if ignore_missing and det_p.exists() else {}
        for cid, a in answers.items():
            m = meta.get(cid, {})
            sp = m.get("split", "holdout" if d.name == "holdout" else "dev")
            if split != "all" and sp != split:
                continue
            kind = m.get("kind", a.get("kind", "unknown"))
            if kinds and kind not in kinds:
                continue
            if cid in cases:
                raise ValueError(f"Duplicate case ID {cid}; run the sets separately")
            key = cid
            cases[key] = {
                "id": cid, "set": d.name, "split": sp, "patch": (d / "commits" / f"{cid}.patch").read_bytes(),
                "failing": sorted(set(a["failing"]) - {id2num[i] for i in (details.get(cid) or {}).get("missing") or []
                                                         if i in id2num}) if ignore_missing else a["failing"],
                "kind": kind,
                "family": m.get("family", kind), "op": m.get("op", kind), "area": m.get("area", "?"),
                "desc": m.get("desc", ""),
            }
    return cases


def ro_sqlglot() -> Path:
    """A read-only copy of the pin for selectors that need a pristine checkout."""
    ro = WORKBENCH / "ro-sqlglot"
    if not (ro / "sqlglot").exists():
        tmp = WORKBENCH / "ro-sqlglot.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for item in BASE.iterdir():
            if item.name != ".git":
                subprocess.run(["cp", "-a", "--reflink=auto", str(item), str(tmp / item.name)], check=True)
        subprocess.run(["chmod", "-R", "a-w", str(tmp)], check=True)
        tmp.rename(ro)
    return ro


# ----------------------------------------------------------------- running


def run_selector(cmd: list[str], cwd: Path, data: Path, limit_s: float, cores: str,
                 extra_env: dict[str, str]) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="br-run-tmp-"))
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp), "TMPDIR": str(tmp), "LANG": "C.UTF-8",
        "BLAST_DATA": str(data), "BLAST_SQLGLOT": str(ro_sqlglot()),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
    }
    env.update(extra_env)
    full = (["taskset", "-c", cores] if cores and shutil.which("taskset") else []) + cmd
    t0 = time.time()
    p = subprocess.Popen(full, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True)
    out = bytearray()
    err = bytearray()
    over = {"stdout": False, "killed": False}

    def pump(src, buf, cap):
        while True:
            chunk = src.read(65536)
            if not chunk:
                break
            if buf is out and len(buf) + len(chunk) > cap:
                over["stdout"] = True
            if len(buf) < (cap or 1 << 30) + 65536:
                buf.extend(chunk)

    th = [threading.Thread(target=pump, args=(p.stdout, out, S.STDOUT_CAP), daemon=True),
          threading.Thread(target=pump, args=(p.stderr, err, 4 << 20), daemon=True)]
    for t in th:
        t.start()

    def killer():
        over["killed"] = True
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer = threading.Timer(limit_s, killer)
    timer.start()
    _, status, ru = os.wait4(p.pid, 0)
    wall = time.time() - t0
    timer.cancel()
    # reap anything left in the session (stragglers would not be charged by rusage)
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for t in th:
        t.join(timeout=5)
    shutil.rmtree(tmp, ignore_errors=True)
    return {
        "stdout": bytes(out), "stderr": bytes(err), "wall": wall,
        "cpu_us": int((ru.ru_utime + ru.ru_stime) * 1_000_000), "maxrss_mb": ru.ru_maxrss / 1024,
        "exit": os.waitstatus_to_exitcode(status), "timeout": over["killed"], "stdout_over": over["stdout"],
    }


def make_batch_dir(root: Path, batch: list[dict], catalog_bytes: bytes) -> Path:
    d = root
    (d / "commits").mkdir(parents=True)
    for c in batch:
        (d / "commits" / f"{c['id']}.patch").write_bytes(c["patch"])
    (d / "tests.json").write_bytes(catalog_bytes)
    subprocess.run(["chmod", "-R", "a-w", str(d)], check=True)
    return d


# ----------------------------------------------------------------- scoring


def score_batch(run: dict, batch: list[dict], tests: list[dict], dur: dict[int, int]) -> dict:
    ids = [c["id"] for c in batch]
    res = {"wall": run["wall"], "cpu_us": run["cpu_us"], "maxrss_mb": run["maxrss_mb"], "exit": run["exit"]}
    if run["timeout"]:
        res.update(ok=False, code="wall_time_exceeded", line=None)
        return res
    if run["stdout_over"]:
        res.update(ok=False, code="stdout_over_2MB", line=None)
        return res
    if run["exit"] != 0:
        res["warn_exit"] = run["exit"]  # scorer rejects a crash; we still score the output
    ev = S.evaluate(run["stdout"], tests, ids)
    if not ev["ok"]:
        res.update(ok=False, code=ev["code"], line=ev["line"])
        if run["exit"] != 0:
            res["code"] = f"crash(exit {run['exit']})/{ev['code']}"
        return res
    per = {}
    by_num = {t["number"]: t for t in tests}
    for c in batch:
        ws = ev["plans"][c["id"]]
        sel = {t for w in ws for t in w}
        missed = [t for t in c["failing"] if t not in sel]
        opt = S.lower_bound(c["failing"], dur)
        per[c["id"]] = {
            "cost_ms": S.commit_cost_ms(ws, dur), "miss": bool(missed),
            "missed": [by_num[t]["id"] for t in missed], "n_sel": len(sel), "n_workers": len(ws),
            "n_fail": len(c["failing"]), "opt_ms": opt,
        }
    res.update(ok=run["exit"] == 0, per=per)
    if run["exit"] != 0:
        res["code"] = f"crash(exit {run['exit']})"
    return res


def fmt_s(ms):
    return f"{ms / 1000:.3f}"


def report(cases: dict, results: list[dict], full_ms: int, args, tests) -> dict:
    by_id = {c["id"]: c for c in cases.values()}
    per = {}
    cpu_us = 0
    rejected = []
    for r in results:
        cpu_us += r["cpu_us"]
        if "per" in r:
            per.update(r["per"])
        if not r.get("ok"):
            rejected.append(r)
    n = len(per)
    sim = sum(p["cost_ms"] for p in per.values())
    misses = [cid for cid, p in per.items() if p["miss"]]
    sim_total = sim + S.MISS_MS * len(misses)
    opt = sum(p["opt_ms"] for p in per.values())
    full = full_ms * n
    out = {"n": n, "sim_ms": sim_total, "sched_ms": sim, "misses": len(misses), "cpu_us": cpu_us,
           "opt_ms": opt, "full_ms": full, "rejected": [{k: v for k, v in r.items() if k != "per"} for r in rejected]}
    print()
    print(f"== {n} patches in {len(results)} batch(es)  [selector: {args.selector}]")
    for r in rejected:
        print(f"!! REJECTED batch: {r.get('code')} line={r.get('line')} wall={r['wall']:.1f}s exit={r['exit']}")
    if not n:
        return out
    tot_us = sim_total * 1000 + cpu_us
    print(f"simulated   {fmt_s(sim_total)} s  (schedule {fmt_s(sim)} + {len(misses)} misses x 300)")
    print(f"cpu         {cpu_us / 1e6:.3f} s   wall per batch: " +
          ", ".join(f"{r['wall']:.1f}" for r in results[:12]) + (" ..." if len(results) > 12 else ""))
    print(f"total       {tot_us / 1e6:.3f} s   = {tot_us / 1e6 / n:.3f} s/patch"
          f"  -> x40 = {tot_us / 1e6 / n * 40:.1f}   x60 = {tot_us / 1e6 / n * 60:.1f}")
    print(f"optimal LB  {fmt_s(opt)} s  ({opt / 1000 / n:.3f} s/patch; schedule only the failing tests)")
    print(f"full suite  {fmt_s(full)} s  ({full_ms / 1000:.3f} s/patch, k=4 LPT, zero misses)")
    print(f"max RSS     {max(r['maxrss_mb'] for r in results):.0f} MB")

    def group(keyf, title):
        g = collections.defaultdict(list)
        for cid, p in per.items():
            case = by_id[cid]
            g["held-out (aggregate)" if case["split"] == "holdout" else keyf(case)].append(p)
        print(f"\n-- by {title}: n  miss  avg_cost_s  avg_opt_s  sel/fail  zero-fail%")
        rows = []
        for k, ps in sorted(g.items(), key=lambda kv: -len(kv[1])):
            m = sum(p["miss"] for p in ps)
            rows.append({"key": k, "n": len(ps), "miss": m,
                         "avg_cost_s": sum(p["cost_ms"] for p in ps) / len(ps) / 1000,
                         "avg_opt_s": sum(p["opt_ms"] for p in ps) / len(ps) / 1000})
            print(f"   {k:<42} {len(ps):4d} {m:4d}  {rows[-1]['avg_cost_s']:9.3f}  {rows[-1]['avg_opt_s']:8.3f}"
                  f"  {sum(p['n_sel'] for p in ps) / len(ps):6.0f}/{sum(p['n_fail'] for p in ps) / len(ps):<5.1f}"
                  f" {100 * sum(p['n_fail'] == 0 for p in ps) / len(ps):5.0f}%")
        return rows

    out["by_kind"] = group(lambda c: c["kind"], "kind")
    if args.verbose:
        out["by_family"] = group(lambda c: c["family"], "family")
    else:
        out["by_op"] = group(lambda c: f"{c['kind']}:{c['op']}", "kind:op")
    misses = [cid for cid in misses if by_id[cid]["split"] == "dev"]
    if misses:
        print(f"\n-- misses ({len(misses)})")
        for cid in sorted(misses, key=lambda x: by_id[x]["kind"]):
            c, p = by_id[cid], per[cid]
            files = sorted({m.split('::')[0] for m in p["missed"]})
            print(f"   {cid} [{c['set']}/{c['kind']}/{c['op']}] {c['desc'][:90]}")
            print(f"        missed {len(p['missed'])}/{p['n_fail']} (selected {p['n_sel']}) in {', '.join(files)[:150]}")
            for m in p["missed"][:4]:
                print(f"          {m}")
    out["per"] = per
    return out


# --------------------------------------------------------------- sampling


def bootstrap(cases, per, cpu_per_patch_us, size, reps, mix, seed):
    rng = random.Random(seed)
    ids = [cid for cid in per]
    by_kind = collections.defaultdict(list)
    for cid in ids:
        by_kind[next(c for c in cases.values() if c["id"] == cid)["kind"]].append(cid)
    def draw():
        if mix == "pool":
            return rng.sample(ids, min(size, len(ids)))
        weights = dict(PRACTICE_KINDS)
        if mix == "final":
            extra = [k for k in FINAL_EXTRA_KINDS if by_kind.get(k)]
            # ~20% of the final from kinds absent from the sample mix
            for k in extra:
                weights[k] = 0.2 * sum(PRACTICE_KINDS.values()) / 0.8 / len(extra)
        ks = [k for k in weights if by_kind.get(k)]
        w = [weights[k] for k in ks]
        return [rng.choice(by_kind[rng.choices(ks, weights=w)[0]]) for _ in range(size)]
    totals, missn = [], []
    for _ in range(reps):
        b = draw()
        sim = sum(per[c]["cost_ms"] + (S.MISS_MS if per[c]["miss"] else 0) for c in b)
        totals.append(S.board_score_s(sim * 1000 + int(cpu_per_patch_us * size)))
        missn.append(sum(per[c]["miss"] for c in b))
    totals.sort()
    q = lambda f: totals[min(len(totals) - 1, int(f * len(totals)))]
    print(f"\n-- bootstrap: {reps} batches of {size} ({mix} mix), CPU est {cpu_per_patch_us * size / 1e6:.2f} s/batch")
    print(f"   board score: mean {statistics.mean(totals):.1f}  p10 {q(.1):.1f}  p50 {q(.5):.1f}  p90 {q(.9):.1f}"
          f"  | misses/batch mean {statistics.mean(missn):.2f}, P(0 misses) {sum(m == 0 for m in missn) / reps:.2f}")
    return {"mean": statistics.mean(totals), "p10": q(.1), "p50": q(.5), "p90": q(.9),
            "miss_mean": statistics.mean(missn)}


# ------------------------------------------------------------------- main


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default="dev", help="comma-separated set names under eval/cases or paths")
    ap.add_argument("--split", default="all", choices=["dev", "holdout", "all"])
    ap.add_argument("--kinds", help="only these kinds (comma-separated)")
    ap.add_argument("--selector", default=None, help="command (default: <venv python> -m selector.main)")
    ap.add_argument("--cwd", default=str(REPO))
    ap.add_argument("--env", action="append", default=[], help="KEY=VAL for the selector")
    ap.add_argument("--batch-size", type=int, default=40)
    ap.add_argument("--limit-s", type=float, default=180)
    ap.add_argument("--cores", default=os.environ.get("BR_RUN_CORES", "8,9"))
    ap.add_argument("--sample", type=int, help="bootstrap batches of this size from per-patch results")
    ap.add_argument("--reps", type=int, default=500)
    ap.add_argument("--mix", default="practice", choices=["practice", "final", "pool"])
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit", type=int, help="use only N random cases")
    ap.add_argument("--exclude", default="", help="comma-separated case ids (or @file) to leave out")
    ap.add_argument("--ignore-missing", action="store_true",
                    help="don't count catalog tests that never reported (renamed/deleted tests, crashes) as failing")
    ap.add_argument("--json")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="cross-check score_lib against the starter score.py")
    args = ap.parse_args(argv)

    catalog_bytes = Path(CATALOG).read_bytes()
    tests = json.loads(catalog_bytes)["tests"]
    dur = {t["number"]: S.duration_ms(t["seconds"]) for t in tests}
    full_ms, _ = S.best_schedule([t["number"] for t in tests], dur)
    if args.selector is None:
        args.selector = f"{PYTHON} -m selector.main"
    cmd = shlex.split(args.selector)
    extra_env = dict(e.split("=", 1) for e in args.env)

    cases = load_cases(args.cases.split(","), args.split, set(args.kinds.split(",")) if args.kinds else None,
                       args.ignore_missing)
    excl = args.exclude
    if excl.startswith("@"):
        excl = Path(excl[1:]).read_text().strip()
    excl = set(filter(None, excl.split(",")))
    cases = {k: v for k, v in cases.items() if v["id"] not in excl}
    if args.limit:
        keys = random.Random(args.seed).sample(sorted(cases), min(args.limit, len(cases)))
        cases = {k: cases[k] for k in keys}
    if not cases:
        sys.exit("no cases")
    order = sorted(cases)
    random.Random(args.seed).shuffle(order)
    batches = [[cases[k] for k in order[i:i + args.batch_size]] for i in range(0, len(order), args.batch_size)]
    # the same opaque id can exist in two sets; a batch must not contain duplicates
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (WORKBENCH / "runs").mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=stamp + "-", dir=WORKBENCH / "runs"))
    results = []
    for bi, batch in enumerate(batches):
        seen, b2 = set(), []
        for c in batch:
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            b2.append(c)
        d = make_batch_dir(root / f"batch{bi:03d}", b2, catalog_bytes)
        run = run_selector(cmd, Path(args.cwd), d, args.limit_s, args.cores, extra_env)
        (root / f"batch{bi:03d}.stdout").write_bytes(run["stdout"])
        (root / f"batch{bi:03d}.stderr").write_bytes(run["stderr"])
        r = score_batch(run, b2, tests, dur)
        results.append(r)
        st = "ok" if r.get("ok") else f"REJECTED {r.get('code')}"
        print(f"[run] batch {bi + 1}/{len(batches)}: {len(b2)} patches, wall {run['wall']:.1f}s, "
              f"cpu {run['cpu_us'] / 1e6:.2f}s, {st}", file=sys.stderr, flush=True)
        if args.selftest:
            _selftest(run["stdout"], b2, catalog_bytes, r)
    out = report(cases, results, full_ms, args, tests)
    if args.sample and out.get("per"):
        cpu_pp = out["cpu_us"] / max(1, out["n"])
        out["bootstrap"] = bootstrap(cases, out["per"], cpu_pp, args.sample, args.reps, args.mix, args.seed)
    print(f"\n[run] outputs and stderr in {root}", file=sys.stderr)
    if args.json:
        out["per"] = {cid: p for cid, p in out.get("per", {}).items()
                      if any(c["id"] == cid and c["split"] == "dev" for c in cases.values())}
        Path(args.json).write_text(json.dumps(out, indent=1))


def _selftest(stdout, batch, catalog_bytes, r):
    """Score the same output with the starter's score.py and compare."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "commits").mkdir()
        for c in batch:
            (td / "commits" / f"{c['id']}.patch").write_bytes(c["patch"])
        (td / "tests.json").write_bytes(catalog_bytes)
        (td / "answers.json").write_text(json.dumps({"commits": {c["id"]: {"failing": c["failing"]} for c in batch}}))
        (td / "out.jsonl").write_bytes(stdout)
        p = subprocess.run([sys.executable, "-I", str(STARTER / "score.py"), str(td), str(td / "answers.json"),
                            str(td / "out.jsonl")], capture_output=True, text=True)
        mine = "rejected" if "per" not in r else \
            f"{fmt_s(sum(x['cost_ms'] for x in r['per'].values()) + S.MISS_MS * sum(x['miss'] for x in r['per'].values()))}"
        print(f"[selftest] starter: {p.stdout.strip().splitlines()[0] if p.stdout else p.stderr.strip()} | mine: {mine}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
