"""Dump module/class namespaces of the pinned sqlglot (run with the profiling
venv, checkout on sys.path).  Used by impact.py to decide whether a changed
module/class-level statement can still be evaluated at import time.

    VENV/bin/python tools/namespaces.py CHECKOUT OUT.json
"""

import ast
import importlib
import importlib.metadata
import inspect
import json
import pkgutil
import sys
import types
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import sqlglot  # noqa: E402

ns_mod, ns_kind, ns_cls = {}, {}, {}
mods = ["sqlglot"]
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    mods.append(m.name)
for name in mods:
    try:
        mod = importlib.import_module(name)
    except Exception as e:  # noqa
        print("skip", name, e, file=sys.stderr)
        continue
    ns_mod[name] = sorted(vars(mod))
    for k, v in vars(mod).items():
        full = f"{name}.{k}"
        if isinstance(v, types.ModuleType):
            ns_kind[full] = "mod"
        elif inspect.isclass(v):
            ns_kind[full] = "cls"
            if (v.__module__ or "").startswith("sqlglot"):
                # only classes that other modules import by name
                ns_cls.setdefault(f"{v.__module__}.{v.__qualname__}", None)

# classes imported by name somewhere (from X import C)
imported = set()
for p in list(root.joinpath("sqlglot").rglob("*.py")):
    try:
        tree = ast.parse(p.read_text())
    except SyntaxError:
        continue
    for s in ast.walk(tree):
        if isinstance(s, ast.ImportFrom) and s.module and not s.level:
            for a in s.names:
                imported.add(f"{s.module}.{a.name}")
cls_out = {}
for full in imported:
    if ns_kind.get(full) != "cls":
        continue
    modname, _, cname = full.rpartition(".")
    obj = getattr(importlib.import_module(modname), cname)
    cls_out[full] = sorted(dir(obj))
    cls_out[f"{obj.__module__}.{obj.__qualname__}"] = cls_out[full]

importable = set(sys.stdlib_module_names) | {"sqlglot", "tests", "sqlglotrs", "sqlglotc"}
for top in importlib.metadata.packages_distributions():
    importable.add(top)
json.dump({"ns_mod": ns_mod, "ns_kind": ns_kind, "ns_cls": cls_out,
           "importable": sorted(importable)}, open(sys.argv[2], "w"))
print(len(ns_mod), "modules", len(cls_out), "classes", len(importable), "importable", file=sys.stderr)
