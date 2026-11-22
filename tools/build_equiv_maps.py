"""Record identity/equality and strict/non-strict ordering agreement from a clean pinned pytest run.

usage: python tools/build_equiv_maps.py /path/to/clean/pin/checkout

Writes selector/maps/identity.json (sites where ``is``/``is not`` always agreed with
``==``/``!=``) and selector/maps/order.json (sites where ``<``/``<=`` or ``>``/``>=``
always agreed, i.e. the operands were never equal). A site is listed only if every
observation across the run was evaluable without side effects and agreed.

selector/maps/equiv-tests.json.gz (identity, order) and equiv-bool.json.gz (bool, ret;
see bool.json/ret.json for the globally certified sites) list, for the remaining sites, the catalog tests in
whose call phase an observation disagreed or was not evaluable. A site is left out
when any such observation happened outside a test's call phase (collection, fixtures,
spawned workers) or inside a module body (lazy imports keep their state).
"""
import concurrent.futures
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
checkout = Path(sys.argv[1]).resolve()
if shutil.which("git"):
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip() == PIN
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout)
else:  # e.g. inside the selector image: the caller mounts a clean pinned checkout
    print("git unavailable: not verifying the checkout", file=sys.stderr)
RUNS = int(os.environ.get("BR_EQUIV_RUNS", "2"))
MAPS = ROOT / "selector/maps"


def trace():
    """One instrumented pytest run: ({site: {kinds}}, {site: {tests}})."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, BR_EQUIV_OUT=tmp, BR_EQUIV_PERTEST="1", SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="1",
                   OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=os.pathsep.join(map(str, [ROOT / "tools/equiv_trace", checkout])),
                   PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"])
        subprocess.run([sys.executable, "-m", "pytest", "tests", "-p", "no:cacheprovider", "-p", "br_equiv_plugin", "-q"],
                       cwd=checkout, env=env, check=True)
        status, bad = {}, {}
        for p in Path(tmp).iterdir():
            if p.name.startswith("pertest-"):
                for site, tests in json.loads(p.read_text()).items():
                    bad.setdefault(site, set()).update(tests)
                continue
            site, _, kind = p.name.rpartition(".")
            status.setdefault(site, set()).add(kind)
        return status, bad


# Several runs (and, for identity/order, the committed maps) are merged monotonically:
# a site is certified only if every run agreed, and per-test lists are unions.
with concurrent.futures.ThreadPoolExecutor(RUNS) as pool:
    runs = list(pool.map(lambda _: trace(), range(RUNS)))
status, bad = {}, {}
for site in set().union(*(s1 for s1, _ in runs)):
    status[site] = set().union(*(s1.get(site, set()) for s1, _ in runs))
    if any(site not in s1 for s1, _ in runs):
        status[site].add("unknown")  # observed in some runs only: not reproducible
for _, b1 in runs:
    for site, tests in b1.items():
        bad.setdefault(site, set()).update(tests)
prefixes = (("id-", "identity"), ("ord-", "order"), ("bool-", "bool"), ("ret-", "ret"))
old_pertest = {}
if (MAPS / "equiv-tests.json.gz").is_file():
    old_pertest = json.loads(gzip.decompress((MAPS / "equiv-tests.json.gz").read_bytes()))
for prefix, kind in prefixes[:2]:
    certified = set(json.loads((MAPS / f"{kind}.json").read_text())) if (MAPS / f"{kind}.json").is_file() else None
    for site in [s for s in status if s.startswith(prefix)]:
        key = site[len(prefix):]
        if certified is not None and key not in certified:
            status[site] = status[site] | {"unknown"}
        if key in old_pertest.get(kind, {}):
            bad.setdefault(site, set()).update(old_pertest[kind][key])
agree = sorted(s for s, kinds in status.items() if kinds == {"agree"})
maps = {kind: sorted(s[len(prefix):] for s in agree if s.startswith(prefix)) for prefix, kind in prefixes}
for name, kind in (("identity", "identity"), ("order", "order"), ("bool", "bool"), ("ret", "ret")):
    (MAPS / f"{name}.json").write_text(json.dumps(maps[kind], indent=0) + "\n")
pertest = {kind: {} for _, kind in prefixes}
for site, kinds in sorted(status.items()):
    if kinds == {"agree"} or "outside" in kinds or not bad.get(site):
        continue
    prefix, kind = next(x for x in prefixes if site.startswith(x[0]))
    pertest[kind][site[len(prefix):]] = sorted(bad[site])
for name, kinds in (("equiv-tests.json.gz", ("identity", "order")), ("equiv-bool.json.gz", ("bool", "ret"))):
    blob = (json.dumps({k: pertest[k] for k in kinds}, indent=0, sort_keys=True) + "\n").encode()
    (MAPS / name).write_bytes(gzip.compress(blob, compresslevel=9, mtime=0))
counts = {kind: sum(1 for s in status if s.startswith(prefix)) for prefix, kind in prefixes}
print(" ".join(f"{kind} {len(maps[kind])}/{counts[kind]}" for kind in counts)
      + " per-test " + " ".join(f"{k} {len(v)}" for k, v in pertest.items()))
