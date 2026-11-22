"""Instrument a clean SQLGlot pytest run to record:
1. Exact per-test ``(dialect_cls_full, UPPER_ATTR)`` reads on ``Dialect``
   instances and classes (via ``Dialect.__getattribute__`` and
   ``_Dialect.__getattribute__``).
2. Exact per-test ``(module:lineno:param)`` default-argument fallbacks for
   non-generator ``sqlglot.*`` functions with immutable constant defaults.
"""
import ast
import atexit
import builtins
import json
import os
from pathlib import Path
import sys
from importlib.machinery import SourceFileLoader

out = os.environ.get("BR_ATTR_OUT")
_ATTR_ONLY = os.environ.get("BR_ATTR_ONLY") == "1"
_UNSET = object()
_busy = [False]

_attr_reads = {}     # "cls_full|ATTR" -> set(test_id)
_attr_outside = set()  # "cls_full|ATTR" read outside a test call phase (import / metaclass / setUpClass)
_def_reads = {}      # "mod:lineno:param" -> set(test_id)
_def_outside = set() # "mod:lineno:param" defaulted outside a test call phase
_def_all = set()     # all instrumented "mod:lineno:param" sites

_METACLASS_DERIVED_EXEMPT = frozenset({
    "TIME_MAPPING", "TIME_TRIE", "INVERSE_TIME_MAPPING", "INVERSE_TIME_TRIE",
    "FORMAT_MAPPING", "FORMAT_TRIE", "INVERSE_FORMAT_MAPPING", "INVERSE_FORMAT_TRIE",
    "CREATABLE_KIND_MAPPING", "INVERSE_CREATABLE_KIND_MAPPING",
})


def _outside_kind():
    """Return 'new' if inside _Dialect.__new__, 'mod' if inside a module body, else None."""
    frame = sys._getframe(2)
    in_new = False
    while frame is not None:
        co = frame.f_code
        if co.co_name == "__new__" and co.co_filename.replace("\\", "/").endswith("sqlglot/dialects/dialect.py"):
            in_new = True
        if co.co_name == "<module>":
            name = frame.f_globals.get("__name__", "")
            if name == "sqlglot" or name.startswith(("sqlglot.", "tests")):
                return "new" if in_new else "mod"
        frame = frame.f_back
    return "new" if in_new else None


def _record_attr(cls, name):
    if _busy[0] or not name or not name.isupper() or name.startswith("__"):
        return
    _busy[0] = True
    try:
        cls_full = f"{cls.__module__}.{cls.__qualname__}"
        key = f"{cls_full}|{name}"
        test = getattr(builtins, "_br_test", None)
        okind = _outside_kind()
        if okind == "new" and name in _METACLASS_DERIVED_EXEMPT:
            return
        if test is None or okind is not None:
            _attr_outside.add(key)
        else:
            _attr_reads.setdefault(key, set()).add(test)
    finally:
        _busy[0] = False


def _record_default(site):
    if _busy[0]:
        return
    _busy[0] = True
    try:
        test = getattr(builtins, "_br_test", None)
        if test is None or _outside_kind() is not None:
            _def_outside.add(site)
        else:
            _def_reads.setdefault(site, set()).add(test)
    finally:
        _busy[0] = False


def _hook_dialect_module(mod):
    Dialect = getattr(mod, "Dialect", None)
    _Dialect = getattr(mod, "_Dialect", None)
    if Dialect is None or _Dialect is None or getattr(Dialect, "_br_hooked", False):
        return
    orig_inst = object.__getattribute__
    orig_meta = type.__getattribute__

    def dialect_getattribute(self, name):
        _record_attr(orig_inst(self, "__class__"), name)
        return orig_inst(self, name)

    def meta_getattribute(cls, name):
        _record_attr(cls, name)
        return orig_meta(cls, name)

    Dialect.__getattribute__ = dialect_getattribute
    _Dialect.__getattribute__ = meta_getattribute
    Dialect._br_hooked = True


def _is_const_default(node):
    return (
        isinstance(node, ast.Constant)
        and type(node.value) in (type(None), bool, int, float, str, bytes)
    )


def _has_yield(fn):
    # Check direct body (do not descend into nested defs/classes).
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            return True
        stack.extend(ast.iter_child_nodes(n))
    return False


_orig_get_code = SourceFileLoader.get_code
_orig_exec_module = SourceFileLoader.exec_module


def _get_code(loader, fullname):
    if _ATTR_ONLY or (not fullname.startswith("sqlglot.") and fullname != "sqlglot"):
        return _orig_get_code(loader, fullname)
    source = loader.get_data(loader.path)
    tree = ast.parse(source, loader.path)

    class DefaultInstrumenter(ast.NodeTransformer):
        changed = False

        def visit_FunctionDef(self, node):
            self.generic_visit(node)
            if node.name.startswith("__") or _has_yield(node):
                return node
            args = node.args
            inserts = []
            # Positional defaults align to the tail of posonlyargs + args
            pos = list(args.posonlyargs) + list(args.args)
            n_def = len(args.defaults)
            if n_def:
                start = len(pos) - n_def
                new_defs = list(args.defaults)
                for idx, d in enumerate(args.defaults):
                    if _is_const_default(d):
                        pname = pos[start + idx].arg
                        site = f"{fullname}:{node.lineno}:{pname}"
                        _def_all.add(site)
                        new_defs[idx] = ast.Name("_br_unset", ast.Load())
                        inserts.append((pname, site, d))
                args.defaults = new_defs
            # Keyword-only defaults
            if args.kw_defaults:
                new_kw = list(args.kw_defaults)
                for idx, d in enumerate(args.kw_defaults):
                    if d is not None and _is_const_default(d):
                        pname = args.kwonlyargs[idx].arg
                        site = f"{fullname}:{node.lineno}:{pname}"
                        _def_all.add(site)
                        new_kw[idx] = ast.Name("_br_unset", ast.Load())
                        inserts.append((pname, site, d))
                args.kw_defaults = new_kw
            if not inserts:
                return node
            self.changed = True
            # Keep docstring first if present
            body = list(node.body)
            prefix = []
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                prefix.append(body.pop(0))
            guards = []
            for pname, site, orig_val in inserts:
                guards.append(
                    ast.If(
                        test=ast.Compare(
                            left=ast.Name(pname, ast.Load()),
                            ops=[ast.Is()],
                            comparators=[ast.Name("_br_unset", ast.Load())],
                        ),
                        body=[
                            ast.Expr(
                                value=ast.Call(
                                    func=ast.Name("_br_record_default", ast.Load()),
                                    args=[ast.Constant(site)],
                                    keywords=[],
                                )
                            ),
                            ast.Assign(
                                targets=[ast.Name(pname, ast.Store())],
                                value=orig_val,
                            ),
                        ],
                        orelse=[],
                    )
                )
            node.body = prefix + guards + body
            return node

    tr = DefaultInstrumenter()
    tr.visit(tree)
    if not tr.changed:
        return _orig_get_code(loader, fullname)
    return compile(ast.fix_missing_locations(tree), loader.path, "exec", dont_inherit=True)


def _exec_module(loader, module):
    _orig_exec_module(loader, module)
    if module.__name__ == "sqlglot.dialects.dialect":
        _hook_dialect_module(module)


def _dump():
    if not out:
        return
    payload = {
        "attr_reads": {k: sorted(v) for k, v in _attr_reads.items()},
        "attr_outside": sorted(_attr_outside),
        "def_reads": {k: sorted(v) for k, v in _def_reads.items()},
        "def_outside": sorted(_def_outside),
        "def_all": sorted(_def_all),
    }
    (Path(out) / f"trace-{os.getpid()}.json").write_text(json.dumps(payload))


if out:
    builtins._br_unset = _UNSET
    builtins._br_record_default = _record_default
    SourceFileLoader.get_code = _get_code
    SourceFileLoader.exec_module = _exec_module
    atexit.register(_dump)
