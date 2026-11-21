"""Record reads of concrete dialect options during pinned pytest invocations."""
import ast
import atexit
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def install():
    (Path(os.environ["BR_ATTR_DIR"]) / f"start-{os.getpid()}").touch()
    from sqlglot.dialects.dialect import Dialect
    import inspect
    import textwrap
    meta = type(Dialect)
    creator = meta.__new__
    tree = ast.parse(textwrap.dedent(inspect.getsource(creator)))
    parent = {k: n for n in ast.walk(tree) for k in ast.iter_child_nodes(n)}
    dict_keys = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr == "__dict__":
            attr = parent[n]
            call = parent[attr]
            assert isinstance(attr, ast.Attribute) and attr.attr == "get"
            assert isinstance(call, ast.Call) and isinstance(call.args[0], ast.Constant)
            dict_keys.add(call.args[0].value)
    reads = set()
    blocked = set()
    prior = [k for k in sys.modules if k.startswith("sqlglot.dialects.")]
    def observe(name, cls, caller):
        if name == "__dict__" and caller.f_code is not creator.__code__:
            blocked.add(type.__getattribute__(cls, "__module__"))
        if name.isupper() or name == "__dict__":
            keys = (name,) if name != "__dict__" else dict_keys
            for key in keys:
                for base in type.__getattribute__(cls, "__mro__"):
                    if key in type.__getattribute__(base, "__dict__"):
                        reads.add(type.__getattribute__(base, "__module__") + ":" + key)
                        break
    old_class, old_instance = meta.__getattribute__, Dialect.__getattribute__
    def class_get(cls, name):
        observe(name, cls, sys._getframe(1))
        return old_class(cls, name)
    def instance_get(obj, name):
        observe(name, type(obj), sys._getframe(1))
        return old_instance(obj, name)
    meta.__getattribute__ = class_get
    Dialect.__getattribute__ = instance_get
    atexit.register(lambda: (Path(os.environ["BR_ATTR_DIR"]) / f"{os.getpid()}.json").write_text(
        json.dumps([sorted(reads), sorted(blocked | set(prior))])))


def record(test, out):
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "sitecustomize.py").write_bytes(Path(__file__).read_bytes())
        env = dict(os.environ, BR_ATTR_DIR=tmp, PYTHONPATH=f"{tmp}:/app/sqlglot")
        subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", test],
                       cwd="/app/sqlglot", env=env, check=True)
        reads, blocked = set(), set()
        files = list(folder.glob("*.json"))
        started = {p.name[6:] for p in folder.glob("start-*")}
        if started != {p.stem for p in files} or not files or ("tpcds" in test and len(files) < 2):
            raise RuntimeError("missing process receipts")
        for p in files:
            names, modules = json.loads(p.read_text())
            reads.update(names)
            blocked.update(modules)
        Path(out).write_text(json.dumps([sorted(reads), sorted(blocked)]))


if os.environ.get("BR_ATTR_DIR"):
    install()
elif __name__ == "__main__":
    record(*sys.argv[1:])
