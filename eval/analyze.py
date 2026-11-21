#!/usr/bin/env python3
"""Summarise labelled case sets (feeds FINDINGS.md).

  eval/analyze.py [--sets dev,exotic,revert,holdout,sample] [--show-comment-failures] [--expensive]
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paths import CASES, CATALOG, WORKBENCH  # noqa: E402

EXPENSIVE = [
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_generator.py::TestGenerator::test_generator_memory",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default="sample,dev,exotic,revert,holdout")
    ap.add_argument("--details", action="store_true", help="list failing cases of comment/nonpy/newfile kinds")
    a = ap.parse_args()
    cat = json.loads(Path(CATALOG).read_text())["tests"]
    num2id = {t["number"]: t["id"] for t in cat}
    id2num = {v: k for k, v in num2id.items()}
    rows = []
    for s in a.sets.split(","):
        d = CASES / s
        if not (d / "answers.json").exists():
            continue
        ans = json.loads((d / "answers.json").read_text())["commits"]
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        det_p = WORKBENCH / "oracle-details" / f"{s}.json"
        det = json.loads(det_p.read_text()) if det_p.exists() else {}
        for cid, x in ans.items():
            m = meta.get(cid, {})
            if m.get("split") == "holdout":
                continue
            rows.append({"set": s, "id": cid, "kind": m.get("kind", x.get("kind")), "op": m.get("op", "?"),
                         "area": m.get("area", "?"), "desc": m.get("desc", ""), "fail": x["failing"],
                         "missing": (det.get(cid) or {}).get("missing") or [],
                         "collect": (det.get(cid) or {}).get("collect_errors") or [],
                         "split": m.get("split", "dev")})
    print(f"{len(rows)} labelled cases")
    print("\n-- by set/kind: n, zero-fail%, median #fail, mean #fail, mean #files, all-fail(>1500)")
    g = collections.defaultdict(list)
    for r in rows:
        g[(r["set"], r["kind"])].append(r)
    cnt_kind = collections.Counter()
    for (s, k), rs in sorted(g.items()):
        nf = sorted(len(r["fail"]) for r in rs)
        files = [len({num2id[n].split("::")[0] for n in r["fail"]}) for r in rs]
        cnt_kind[k] += len(rs)
        print(f"   {s:8} {k:12} {len(rs):4d}  {100 * sum(x == 0 for x in nf) / len(rs):5.1f}%  "
              f"{nf[len(nf) // 2]:5d}  {sum(nf) / len(nf):7.1f}  {sum(files) / len(files):5.2f}  {sum(x > 1500 for x in nf):3d}")
    print("\n-- totals by kind:", dict(cnt_kind.most_common()))

    print("\n-- by kind:op (all sets): n, zero-fail%, mean #fail")
    g = collections.defaultdict(list)
    for r in rows:
        g[f"{r['kind']}:{r['op']}"].append(r)
    for k, rs in sorted(g.items(), key=lambda kv: -len(kv[1])):
        print(f"   {k:40} {len(rs):4d} {100 * sum(not r['fail'] for r in rs) / len(rs):5.1f}%  "
              f"{sum(len(r['fail']) for r in rs) / len(rs):7.1f}")

    print("\n-- code mutants by area: n, zero-fail%")
    g = collections.defaultdict(list)
    for r in rows:
        if r["kind"] in ("code", "revert"):
            g[(r["kind"], r["area"].split(":")[0])].append(r)
    for k, rs in sorted(g.items()):
        print(f"   {k[0]:7} {k[1]:30} {len(rs):4d} {100 * sum(not r['fail'] for r in rs) / len(rs):5.1f}%")

    print("\n-- the three expensive tests: cases failing each (excluding all-fail cases)")
    for e in EXPENSIVE:
        n = id2num[e]
        hits = [r for r in rows if n in r["fail"] and len(r["fail"]) < 1500]
        print(f"   {e.split('::')[-1]}: {len(hits)} cases ({len([r for r in rows if n in r['fail']])} incl. all-fail)")
        for r in hits[:25]:
            print(f"      {r['set']}/{r['id']} {r['kind']}/{r['op']} nfail={len(r['fail'])} {r['desc'][:90]}")

    print("\n-- most frequently failing tests (cases)")
    c = collections.Counter(n for r in rows if len(r["fail"]) < 1500 for n in r["fail"])
    for n, k in c.most_common(25):
        print(f"   {k:4d}  {num2id[n]}")

    print("\n-- cases with catalog tests that never reported (missing) or collection errors")
    for r in rows:
        if r["missing"] or r["collect"]:
            print(f"   {r['set']}/{r['id']} {r['kind']}/{r['op']} missing={len(r['missing'])} "
                  f"collect={[c['nodeid'] for c in r['collect']][:4]} nfail={len(r['fail'])} {r['desc'][:70]}")

    if a.details:
        print("\n-- non-code kinds that FAIL something")
        for r in rows:
            if r["kind"] in ("comment", "nonpy", "newfile", "conftest", "export", "rename", "delete", "test") and r["fail"]:
                files = sorted({num2id[n].split("::")[0] for n in r["fail"]})
                print(f"   {r['set']}/{r['id']} {r['kind']}/{r['op']} nfail={len(r['fail'])} {r['desc'][:80]}\n"
                      f"        files: {', '.join(files)[:200]}")


if __name__ == "__main__":
    main()
