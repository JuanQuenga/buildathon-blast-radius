"""Instrument a clean SQLGlot pytest run. BR_EQUIV_OUT must be an empty directory.

Every single-operator ``is``/``is not`` comparison and every single-operator
ordering comparison (``<``, ``<=``, ``>``, ``>=``) in ``sqlglot.*`` is replaced by
a call that computes the original result exactly once and returns it. The
alternative operator (``==``/``!=`` for identity, the non-strict/strict twin for
ordering) is evaluated only when both operand types have side-effect-free
comparison implementations. A site is recorded as ``agree`` only if every
observation was evaluable and both operators gave the same result.
"""
import ast
import builtins
import datetime
import decimal
from enum import Enum
import os
from pathlib import Path
import sys
from importlib.machinery import SourceFileLoader

out = os.environ.get("BR_EQUIV_OUT")
seen = set()
# Comparisons evaluated by the observer itself (e.g. Expression.__eq__ contains an
# instrumented ``is``) are not program executions: compute them without recording.
_busy = [False]

_SCALARS = (type(None), bool, int, float, str, bytes, complex)
_ORDERED = frozenset({bool, int, float, str, bytes, decimal.Decimal, datetime.date, datetime.datetime,
                      datetime.time, datetime.timedelta})
_BUILTIN_EQ = {t.__eq__ for t in (object, int, float, str, bytes, complex, bool, tuple, list, dict, set,
                                  frozenset, type, range, type(None))}
_BUILTIN_NE = {t.__ne__ for t in (object, int, float, str, bytes, complex, bool, tuple, list, dict, set,
                                  frozenset, type, range, type(None))}


def _known_python_eq(fn):
    """Pinned SQLGlot __eq__ implementations that return False for a foreign scalar
    without side effects (identity and type checks before any hashing)."""
    code = getattr(fn, "__code__", None)
    if code is None:
        return False
    name = code.co_filename.replace("\\", "/")
    return ((name.endswith("sqlglot/expressions/core.py") and fn.__qualname__ == "Expression.__eq__")
            or (name.endswith("sqlglot/dialects/dialect.py") and fn.__qualname__ in {"Dialect.__eq__", "_Dialect.__eq__"}))


def _known_python_ne(fn):
    code = getattr(fn, "__code__", None)
    if code is None:
        return False
    return code.co_filename.replace("\\", "/").endswith("sqlglot/expressions/core.py") and fn.__qualname__ == "Expression.__ne__"


def _scalar(value):
    tp = type(value)
    if tp in _SCALARS:
        return True
    return isinstance(value, Enum) and tp.__eq__ in _BUILTIN_EQ and tp.__ne__ in _BUILTIN_NE


def _pure_against_scalar(value, scalar):
    tp = type(value)
    if _known_python_eq(tp.__eq__) and "Dialect" in tp.__eq__.__qualname__ and isinstance(scalar, str):
        return False  # Dialect equality with a name looks the dialect up and may import it
    return ((tp.__eq__ in _BUILTIN_EQ or _known_python_eq(tp.__eq__))
            and (tp.__ne__ in _BUILTIN_NE or _known_python_ne(tp.__ne__)))


_CONTAINERS = frozenset({list, dict, tuple, set, frozenset})


def _object_safe(tp):
    """Equality that never runs foreign code: identity, or Expression structural equality
    (type check, then a hash that is cached consistently and invalidated on mutation)."""
    if tp.__eq__ is object.__eq__ and tp.__ne__ is object.__ne__:
        return True
    return _known_python_eq(tp.__eq__) and tp.__eq__.__qualname__ == "Expression.__eq__" and _known_python_ne(tp.__ne__)


def _pure_equality(left, right):
    if _scalar(left) and _scalar(right):
        return True
    if _scalar(right):
        return _pure_against_scalar(left, right)
    if _scalar(left):
        return _pure_against_scalar(right, left)
    lt, rt = type(left), type(right)
    if _object_safe(lt) and _object_safe(rt):
        return True
    # A builtin container returns NotImplemented for a non-container, and the reflected
    # identity or Expression equality only checks the type.
    return (lt in _CONTAINERS and _object_safe(rt)) or (rt in _CONTAINERS and _object_safe(lt))


def _orderable(value, depth=0):
    tp = type(value)
    if tp in _ORDERED:
        return True
    return tp is tuple and depth < 3 and all(_orderable(v, depth + 1) for v in value)


_pertest = bool(os.environ.get("BR_EQUIV_PERTEST"))
_bad = {}


def _record(site, status):
    key = (site, status)
    if key not in seen:
        seen.add(key)
        (Path(out) / f"{site}.{status}").touch()
    if status != "agree" and _pertest:
        test = getattr(builtins, "_br_test", None)
        if test is None or _in_module_body():
            key = (site, "outside")
            if key not in seen:
                seen.add(key)
                (Path(out) / f"{site}.outside").touch()
        else:
            _bad.setdefault(site, set()).add(test)


def _in_module_body():
    """A lazily imported module (class bodies, metaclasses, registries) keeps its
    state for every later test, so such observations are not attributed to one test."""
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_code.co_name == "<module>":
            name = frame.f_globals.get("__name__", "")
            if name == "sqlglot" or name.startswith(("sqlglot.", "tests")):
                return True
        frame = frame.f_back
    return False


def _dump_pertest():
    import json
    (Path(out) / f"pertest-{os.getpid()}.json").write_text(json.dumps({k: sorted(v) for k, v in _bad.items()}))


def observe_identity(site, left, right, negate):
    identity = left is right
    if _busy[0]:
        return not identity if negate else identity
    status = "unknown"
    _busy[0] = True
    try:
        if _pure_equality(left, right):
            status = "agree" if ((left == right) is identity and (left != right) is (not identity)) else "disagree"
    except Exception:
        status = "unknown"
    finally:
        _busy[0] = False
    _record("id-" + site, status)
    return not identity if negate else identity


def observe_order(site, left, right, op):
    if op == 0:
        result = left < right
    elif op == 1:
        result = left <= right
    elif op == 2:
        result = left > right
    else:
        result = left >= right
    if _busy[0]:
        return result
    status = "unknown"
    _busy[0] = True
    try:
        if _orderable(left) and _orderable(right):
            if op < 2:
                same = (left < right) is (left <= right)
            else:
                same = (left > right) is (left >= right)
            status = "agree" if same else "disagree"
    except Exception:
        status = "unknown"
    finally:
        _busy[0] = False
    _record("ord-" + site, status)
    return result


_ORDER_OPS = {ast.Lt: 0, ast.LtE: 1, ast.Gt: 2, ast.GtE: 3}
original = SourceFileLoader.get_code


def get_code(loader, fullname):
    if not fullname.startswith("sqlglot.") and fullname != "sqlglot":
        return original(loader, fullname)
    source = loader.get_data(loader.path)
    tree = ast.parse(source, loader.path)

    class Replace(ast.NodeTransformer):
        changed = False

        def visit_Compare(self, node):
            self.generic_visit(node)
            if len(node.ops) != 1:
                return node
            op = node.ops[0]
            site = f"{fullname}-{node.lineno}-{node.col_offset}"
            if isinstance(op, (ast.Is, ast.IsNot)):
                fn = "_br_observe_identity"
                extra = ast.Constant(isinstance(op, ast.IsNot))
            elif type(op) in _ORDER_OPS:
                fn = "_br_observe_order"
                extra = ast.Constant(_ORDER_OPS[type(op)])
            else:
                return node
            self.changed = True
            args = [ast.Constant(site), node.left, node.comparators[0], extra]
            return ast.copy_location(ast.Call(ast.Name(fn, ast.Load()), args, []), node)

    transform = Replace()
    transform.visit(tree)
    if not transform.changed:
        return original(loader, fullname)
    return compile(ast.fix_missing_locations(tree), loader.path, "exec", dont_inherit=True)


if out:
    if _pertest:
        import atexit
        atexit.register(_dump_pertest)
    builtins._br_observe_identity = observe_identity
    builtins._br_observe_order = observe_order
    SourceFileLoader.get_code = get_code
