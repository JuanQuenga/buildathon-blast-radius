"""Record identity/equality and strict/non-strict ordering agreement from a clean pinned pytest run.

usage: python tools/build_equiv_maps.py /path/to/clean/pin/checkout

Writes selector/maps/identity.json (sites where ``is``/``is not`` always agreed with
``==``/``!=``) and selector/maps/order.json (sites where ``<``/``<=`` or ``>``/``>=``
always agreed, i.e. the operands were never equal). A site is listed only if every
observation across the run was evaluable without side effects and agreed.

selector/maps/equiv-tests.json lists, for the remaining sites, the catalog tests in
whose call phase an observation disagreed or was not evaluable. A site is left out
when any such observation happened outside a test's call phase (collection, fixtures,
spawned workers) or inside a module body (lazy imports keep their state).
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
checkout = Path(sys.argv[1]).resolve()
assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip() == PIN
assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout)
with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ, BR_EQUIV_OUT=tmp, BR_EQUIV_PERTEST="1", SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="1",
               OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join(map(str, [ROOT / "tools/equiv_trace", checkout])),
               PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"])
    subprocess.run([sys.executable, "-m", "pytest", "tests", "-p", "no:cacheprovider", "-p", "br_equiv_plugin", "-q"],
                   cwd=checkout, env=env, check=True)
    status = {}
    bad = {}
    for p in Path(tmp).iterdir():
        if p.name.startswith("pertest-"):
            for site, tests in json.loads(p.read_text()).items():
                bad.setdefault(site, set()).update(tests)
            continue
        site, _, kind = p.name.rpartition(".")
        status.setdefault(site, set()).add(kind)
    agree = sorted(s for s, kinds in status.items() if kinds == {"agree"})
    identity = sorted(s[3:] for s in agree if s.startswith("id-"))
    order = sorted(s[4:] for s in agree if s.startswith("ord-"))
    (ROOT / "selector/maps/identity.json").write_text(json.dumps(identity, indent=0) + "\n")
    (ROOT / "selector/maps/order.json").write_text(json.dumps(order, indent=0) + "\n")
    pertest = {"identity": {}, "order": {}}
    for site, kinds in sorted(status.items()):
        if kinds == {"agree"} or "outside" in kinds or not bad.get(site):
            continue
        kind, key = ("identity", site[3:]) if site.startswith("id-") else ("order", site[4:])
        pertest[kind][key] = sorted(bad[site])
    (ROOT / "selector/maps/equiv-tests.json").write_text(json.dumps(pertest, indent=0, sort_keys=True) + "\n")
    counts = {k: sum(1 for s in status if s.startswith(k)) for k in ("id-", "ord-")}
    print(f"identity {len(identity)}/{counts['id-']} order {len(order)}/{counts['ord-']} "
          f"per-test identity {len(pertest['identity'])} order {len(pertest['order'])}")
