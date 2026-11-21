import os,sys,json,time,tempfile,importlib
from pathlib import Path
sys.path.insert(0,"/app")
from selector import heavy, impact, verify, warm_replay
from selector.patch import parse
files=sorted(Path("/data/commits").glob("*.patch"))
cat=json.loads(Path("/data/tests.json").read_text())["tests"]
ids={t["number"]:t["id"] for t in cat}
def trial(p):
    text=p.read_text()
    info=impact.select(text,cat)
    cases=sorted(info["required"])
    if len(cases)>12 or not cases:return
    paths=",".join(impact._module_name(f.path) for f in parse(text))
    with tempfile.TemporaryDirectory() as work:
        verify.build_overlay(work,verify.apply_patch(text))
        with tempfile.TemporaryFile() as receipt:
            if not warm_replay.compatible(paths):return
            start=heavy.cpu()
            result=warm_replay.run(Path("/trial/pytest_small.py"),"bundle",[json.dumps([ids[n] for n in cases])],work,time.monotonic()+3,paths,receipt,cpu_limit=1.8)
            print(p.stem,len(cases),result,round(heavy.cpu()-start,4),flush=True)
start=heavy.cpu()
import pytest
warm_replay.prepare_core()
print("warm",round(heavy.cpu()-start,4))
for p in files:
 if p.stem in {"bj7g","fryt","535z","j6uz"}:trial(p)
