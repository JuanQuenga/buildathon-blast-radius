"""Build compact per-test class-attribute read and default-argument usage maps
from ONE clean pinned pytest run.

Usage: python tools/build_attr_default_maps.py /path/to/clean/pin/checkout

Writes:
  - selector/maps/class-attr-reads.json.gz
  - selector/maps/default-args.json.gz
"""
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"

sys.path.insert(0, str(ROOT))
from selector import impact

checkout = Path(sys.argv[1]).resolve()
attrs_only = "--attrs-only" in sys.argv[2:]
assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip() == PIN
assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout)

tests = impact._index()["tests"]
test_idx = {t: i for i, t in enumerate(tests)}

with tempfile.TemporaryDirectory() as tmp:
    env = dict(
        os.environ,
        BR_ATTR_OUT=tmp,
        BR_ATTR_ONLY="1" if attrs_only else "0",
        SKIP_INTEGRATION="0",
        PYTHON_CPU_COUNT="1",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join(map(str, [ROOT / "tools/attr_trace", checkout])),
        PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"],
    )
    subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-p", "no:cacheprovider", "-p", "br_attr_plugin", "-q"],
        cwd=checkout,
        env=env,
        check=True,
    )

    attr_reads = {}
    attr_outside = set()
    def_reads = {}
    def_outside = set()
    def_all = set()
    for p in sorted(Path(tmp).glob("trace-*.json")):
        d = json.loads(p.read_text())
        for k, tlist in d["attr_reads"].items():
            attr_reads.setdefault(k, set()).update(tlist)
        attr_outside.update(d["attr_outside"])
        for k, tlist in d["def_reads"].items():
            def_reads.setdefault(k, set()).update(tlist)
        def_outside.update(d["def_outside"])
        def_all.update(d["def_all"])

    # Convert nodeids to integer indices into impact._index()["tests"].
    # Any (cls_full, attr) seen by an unindexed test is conservatively marked outside.
    attr_by_name = {}
    outside_by_name = {}
    for k in sorted(attr_outside):
        if "|" in k:
            cls_full, aname = k.split("|", 1)
            outside_by_name.setdefault(aname, set()).add(cls_full)
    for k, tset in sorted(attr_reads.items()):
        cls_full, aname = k.split("|", 1)
        if any(t not in test_idx for t in tset):
            outside_by_name.setdefault(aname, set()).add(cls_full)
            continue
        attr_by_name.setdefault(aname, {})[cls_full] = sorted(test_idx[t] for t in tset)

    attr_payload = {
        "outside": {a: sorted(cs) for a, cs in sorted(outside_by_name.items())},
        "reads": attr_by_name,
    }
    attr_out = ROOT / "selector/maps/class-attr-reads.json.gz"
    attr_out.write_bytes(
        gzip.compress(json.dumps(attr_payload, separators=(",", ":")).encode(), mtime=0)
    )
    print(
        f"class-attr-reads.json.gz: {attr_out.stat().st_size/1024:.1f} KiB "
        f"({len(attr_by_name)} attrs, {len(outside_by_name)} attrs with outside classes)"
    )

    if not attrs_only:
        clean_defs = {}
        for site in sorted(def_all):
            if site in def_outside:
                continue
            tset = def_reads.get(site, set())
            if any(t not in test_idx for t in tset):
                continue
            clean_defs[site] = sorted(test_idx[t] for t in tset)
        def_out = ROOT / "selector/maps/default-args.json.gz"
        def_out.write_bytes(
            gzip.compress(json.dumps(clean_defs, separators=(",", ":")).encode(), mtime=0)
        )
        print(
            f"default-args.json.gz: {def_out.stat().st_size/1024:.1f} KiB "
            f"({len(clean_defs)}/{len(def_all)} clean sites, {len(def_outside)} outside)"
        )
