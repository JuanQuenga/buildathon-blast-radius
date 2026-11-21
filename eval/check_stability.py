"""Independently repeat a seeded sample of labels, recording only aggregate agreement."""
import argparse
import json
import random

import oracle
from paths import CASES, WORKBENCH


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sets", nargs="+")
    ap.add_argument("-n", default=2, type=int, help="draws per set")
    ap.add_argument("--seed", type=int, default=20261120)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    tests, skipped = oracle.load_catalog()
    results = []
    output = WORKBENCH / ("stability-" + "-".join(args.sets) + f"-{args.seed}.json")
    for name in args.sets:
        d = CASES / name
        labels = json.loads((d / "answers.json").read_text())["commits"]
        eligible = sorted(cid for cid, entry in labels.items() if not entry.get("basis"))
        for cid in rng.sample(eligible, args.n):
            measured = oracle.label((d / "commits" / f"{cid}.patch").read_bytes(),
                                    tests, skipped, use_cache=False)
            ok = (measured["status"] == "ok" and measured["rc"] in (0, 1, 5)
                  and measured["failing"] == labels[cid]["failing"])
            results.append(dict(set=name, id=cid, match=ok, status=measured["status"],
                                reference=labels[cid]["failing"], repeat=measured["failing"]))
            output.write_text(json.dumps(results, separators=(",", ":")) + "\n")
        rows = [r for r in results if r["set"] == name]
        print(f"{name}: {sum(r['match'] for r in rows)}/{len(rows)} identical", flush=True)
    return int(any(not r["match"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
