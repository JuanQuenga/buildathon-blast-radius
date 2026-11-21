"""Profile value reads in fresh pytest processes on the unmodified SQLGlot pin."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

def profile(args):
    with tempfile.TemporaryDirectory() as work:
        env = dict(os.environ, BR_KEYWORD_TRACE=work, PYTHON_CPU_COUNT="1",
                   PYTHONPATH=str(Path(__file__).parent / "keyword_trace") + ":/app/sqlglot")
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
                              cwd="/app/sqlglot", env=env, capture_output=True, timeout=170)
        files = sorted(Path(work).glob("*.json"))
        if proc.returncode or {p.stem for p in files} != {p.stem for p in Path(work).glob("*.start")}:
            raise ValueError(f"incomplete pytest run {args}")
        keys = set()
        installed = False
        for f in files:
            loaded, observed = json.loads(f.read_text())
            installed |= loaded
            keys.update(observed)
        if not installed:
            raise ValueError("tokenizer was not instrumented")
        return keys

if __name__ == "__main__":
    result = {}
    collection = profile(["--collect-only", "tests"])
    for t in json.loads(Path(sys.argv[1]).read_text())["tests"]:
        if t["seconds"] < 0.5:
            continue
        try:
            result[t["id"]] = sorted(collection | profile([t["id"]]))
        except (ValueError, subprocess.TimeoutExpired) as exc:
            print(exc, file=sys.stderr)
    sys.path.insert(0, "/app/sqlglot")
    from sqlglot.dialects.dialect import Dialect, Dialects
    excluded = set()
    single = set()
    for dialect in Dialects:
        cls = Dialect.get_or_raise(dialect).tokenizer_class
        excluded.add(cls.HINT_START)
        single.update(cls.SINGLE_TOKENS)
    import ast
    for path in Path("/app/sqlglot/sqlglot").rglob("*.py"):
        if "KEYWORDS.pop" in path.read_text():
            for n in ast.walk(ast.parse(path.read_text())):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "pop":
                    obj = n.func.value
                    if isinstance(obj, ast.Name) and obj.id == "KEYWORDS":
                        excluded.add(ast.literal_eval(n.args[0]))
    print(json.dumps(dict(profiles=result, excluded=sorted(excluded), single=sorted(single)), sort_keys=True))
