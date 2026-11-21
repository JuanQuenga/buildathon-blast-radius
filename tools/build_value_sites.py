"""Run every pinned test that reaches a nested builder, including spawned workers."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from value_trace.sitecustomize import site_parts
from selector.impact import _index, _sources


def main():
    idx = _index()
    tests = set()
    for path, source in _sources()["src"].items():
        if "exp.alias_(" not in source or "exp.table_(" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if site_parts(node):
                sid = idx["cov"].get(path, {}).get(node.lineno)
                if sid is not None:
                    tests.update(n for i, n in enumerate(idx["tests"]) if idx["sets"][sid] & (1 << i))
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, BR_VALUES=tmp,
                   PYTHONPATH=str(Path(__file__).parent / "value_trace") + ":/app/sqlglot")
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *sorted(tests)],
                       cwd="/app/sqlglot", env=env, check=True, stdout=sys.stderr)
        files = list(Path(tmp).glob("*.json"))
        assert files and {p.stem for p in files} == {p.stem for p in Path(tmp).glob("*.start")}
        facts = {}
        for path in files:
            for site, safe in json.loads(path.read_text()).items():
                facts[site] = facts.get(site, True) and safe
        print(json.dumps(sorted(site for site in facts if facts[site])))


if __name__ == "__main__":
    main()
