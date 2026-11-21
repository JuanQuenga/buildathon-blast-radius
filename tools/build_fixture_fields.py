"""Compile the pristine pytest fixture-field recording into the selector map."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path

from selector.impact import _index


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("profile", type=Path)
    ap.add_argument("pin", type=Path)
    ap.add_argument("nodes", type=Path)
    ap.add_argument("out", type=Path)
    args = ap.parse_args()
    sizes, fields, owners, tables = json.loads((args.profile / "profile.json").read_text())
    tested = set(args.nodes.read_text().splitlines())
    raw = defaultdict(set)
    for p in args.profile.glob("raw-*.json"):
        for node, name in json.loads(p.read_text()):
            raw[name].add(node)
    index = _index()
    result = {}
    for name in sorted(set(sizes) | set(tables)):
        path = "tests/fixtures/" + name
        bits = index["sets"][index["opens"][path]]
        readers = {n for i,n in enumerate(index["tests"]) if bits >> i & 1}
        if not readers <= tested or "*" in raw[name]:
            continue
        row = {"sha256": hashlib.sha256((args.pin/path).read_bytes()).hexdigest()}
        if name in sizes:
            paired, size = sizes[name]
            consumed = fields.get(name, {})
            if "*" in consumed:
                continue
            row.update(paired=paired, size=size, raw=sorted(raw[name]),
                       reads={n:[indices.get(str(i),0) for i in range(size)]
                              for n,indices in consumed.items()})
        else:
            if "*" in owners and name in owners["*"]:
                continue
            row["table"] = sorted(n for n in readers if not n.startswith(
                "tests/test_executor.py::TestExecutor::") or name in owners.get(n, []))
        result[name] = row
    args.out.write_text(json.dumps(result, sort_keys=True, separators=(",",":"))+"\n")


if __name__ == "__main__":
    main()
