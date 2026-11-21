"""Quick in-process evaluation of selector.impact on labelled case dirs.

    python3 tools/eval_impact.py CASEDIR [CASEDIR...] [--split dev] [-v] [--only ID]

CASEDIR has commits/*.patch and answers.json (official format); the catalog
defaults to the starter sample tests.json.  Reports misses (must be 0),
simulated seconds with selector.schedule, full fallbacks and selector CPU.
Labels are used for measurement only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from selector import impact  # noqa: E402
from selector.schedule import duration_ms, plan_cost, schedule  # noqa: E402

DEFAULT_CATALOG = ROOT.parent.parent / "blast-radius/starter/sample-kit/sample/tests.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    ap.add_argument("--split", default="dev", help="dev|holdout|all (meta.json split)")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--kind", default="")
    a = ap.parse_args()
    catalog = json.loads(Path(a.catalog).read_text())["tests"]
    dur = {t["number"]: duration_ms(t["seconds"]) for t in catalog}
    name = {t["number"]: t["id"] for t in catalog}
    full = schedule(set(dur), set(), dur)
    full_cost = plan_cost(full, dur)
    tot = dict(n=0, cost=0, miss=0, full=0, cpu=0.0, opt=0)
    by_kind: dict[str, list] = {}
    impact._index(); impact._sources()
    for d in a.dirs:
        d = Path(d)
        ans = json.loads((d / "answers.json").read_text())["commits"]
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        for p in sorted((d / "commits").glob("*.patch")):
            cid = p.stem
            if cid not in ans:
                continue
            m = meta.get(cid, {})
            split = m.get("split", "dev")
            if a.split != "all" and split != a.split:
                continue
            if a.only and cid != a.only:
                continue
            kind = ans[cid].get("kind") or m.get("kind", "?")
            if a.kind and kind != a.kind:
                continue
            failing = set(ans[cid]["failing"])
            t0 = time.process_time()
            try:
                res = impact.select(p.read_text(), catalog)
            except Exception as e:  # noqa
                res = {"full": True, "required": set(), "plausible": set(),
                       "reasons": [f"CRASH {type(e).__name__}: {e}"]}
            cpu = time.process_time() - t0
            if res["full"]:
                ws, cost = full, full_cost
                sel = set(dur)
                tot["full"] += 1
            else:
                ws = schedule(res["required"], res["plausible"], dur)
                cost = plan_cost(ws, dur)
                sel = {t for w in ws for t in w}
            missed = failing - sel
            opt = plan_cost(schedule(failing, set(), dur), dur) if failing else 0
            tot["n"] += 1
            tot["cost"] += cost + (300000 if missed else 0)
            tot["miss"] += bool(missed)
            tot["cpu"] += cpu
            tot["opt"] += opt
            by_kind.setdefault(kind, []).append((cost, bool(missed)))
            flag = "MISS" if missed else ("FULL" if res["full"] else "    ")
            print(f"{flag} {d.name}/{cid} {kind:8s} fail={len(failing):3d} "
                  f"req={len(res['required']):4d} cost={cost / 1000:6.1f}s opt={opt / 1000:5.1f}s "
                  f"cpu={cpu * 1000:5.0f}ms {m.get('desc', '')[:60]}")
            if missed:
                print("      missed:", sorted(name[t] for t in missed)[:8])
            if a.v or missed or res["full"]:
                for r in res["reasons"][:12]:
                    print("      ", r[:200])
    n = max(tot["n"], 1)
    print(f"\n{tot['n']} patches: misses={tot['miss']} full={tot['full']} "
          f"simulated={tot['cost'] / 1000:.1f}s (opt {tot['opt'] / 1000:.1f}s, full-suite "
          f"{n * full_cost / 1000:.1f}s) avg={tot['cost'] / 1000 / n:.2f}s/patch "
          f"cpu={tot['cpu']:.2f}s")
    for k, v in sorted(by_kind.items()):
        print(f"  {k:12s} n={len(v):3d} misses={sum(m for _, m in v)} "
              f"avg={sum(c for c, _ in v) / len(v) / 1000:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
