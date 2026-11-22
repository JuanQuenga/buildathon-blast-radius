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
import copy
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


def _attribution():
    """(test, outside): outside means the observation cannot be pinned to one test's call phase."""
    test = getattr(builtins, "_br_test", None)
    return test, test is None or _in_module_body()


def _record(site, status, attribution=None):
    key = (site, status)
    if key not in seen:
        seen.add(key)
        (Path(out) / f"{site}.{status}").touch()
    if status != "agree" and _pertest:
        test, outside = attribution or _attribution()
        if outside:
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


_bstack = []
_UNSET = object()  # second operand not evaluated by the original


def _truth(value):
    try:
        return bool(value)
    except Exception:
        return None


def _entry(site, fid):
    for i in range(len(_bstack) - 1, -1, -1):
        e = _bstack[i]
        if e[0] == site and e[1] == fid:
            return i
    return None


def bool_pre(site, a):
    """Record the first operand's truthiness; return it unchanged (the operator re-tests it)."""
    if not _busy[0]:
        _busy[0] = True
        try:
            _bstack.append([site, id(sys._getframe(1)), _truth(a), _UNSET, _attribution() if _pertest else None])
        finally:
            _busy[0] = False
    return a


def bool_mid(site, b):
    """Only reached when the original evaluates the second operand."""
    if not _busy[0]:
        _busy[0] = True
        try:
            i = _entry(site, id(sys._getframe(1)))
            if i is not None:
                _bstack[i][3] = _truth(b)
        finally:
            _busy[0] = False
    return b


def bool_post(site, result, pure_b):
    """Decide whether swapping and/or would give the same truthiness for this evaluation.

    The value of these operations is only truth-tested, and exactly one of the two forms
    evaluates the second operand. The swap agrees when both operands' truthiness is known
    and equal and the second operand is effect-free (``pure_b`` is its thunk, else None):
    the swapped form either skips the effects the original ran or runs effects the
    original skipped. When the original skipped it, it is evaluated here to learn the
    truthiness the swapped form would see (or that it would raise)."""
    if _busy[0]:
        return result
    _busy[0] = True
    try:
        i = _entry(site, id(sys._getframe(1)))
        if i is None:
            status = "unknown"
        else:
            for stale in _bstack[i + 1:]:
                _record("bool-" + stale[0], "unknown", stale[4])
            e = _bstack[i]
            del _bstack[i:]
            a, b = e[2], e[3]
            if a is None or pure_b is None:
                status = "unknown"
            elif b is not _UNSET:
                status = "unknown" if b is None else "agree" if a == b else "disagree"
            else:
                try:
                    alt = _truth(pure_b())
                except Exception:
                    alt = None
                # The mutant would raise (or test an unknown value) where the original did not.
                status = "agree" if alt is not None and alt == a else "disagree"
    finally:
        _busy[0] = False
    _record("bool-" + site, status)
    return result


def bool_flush():
    """Evaluations whose second operand raised never reach bool_post: the swap could change them."""
    stale = _bstack[:]
    del _bstack[:]
    for e in stale:
        _record("bool-" + e[0], "unknown", e[4])


def ret_obs(site, thunk):
    """``return X`` -> ``return None and X`` returns None without evaluating X (X is
    effect-free here). ``thunk`` evaluates X, so an X that raises (where the mutant would
    return None) is recorded too."""
    try:
        value = thunk()
    except BaseException:
        if not _busy[0]:
            _record("ret-" + site, "disagree")
        raise
    if not _busy[0]:
        _record("ret-" + site, "agree" if value is None else "disagree")
    return value


# Calls with no side effect the program can observe (they may raise; caches aside).
_FREE_BUILTINS = frozenset({"isinstance", "issubclass", "len", "type", "id", "callable", "bool"})
_FREE_HELPERS = frozenset({"is_int", "seq_get"})  # sqlglot.helper, pure
_FREE_METHODS = frozenset({"is_type", "upper", "lower", "startswith", "endswith", "isdigit", "isspace",
                           "text", "meta_get", "find_ancestor"})


def _free_names(tree):
    """Free-call names that refer to the builtin (or sqlglot.helper) function everywhere in this module."""
    bound, helpers = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            bound.add(n.id)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for alias in n.names:
                name = alias.asname or alias.name.split(".")[0]
                if (isinstance(n, ast.ImportFrom) and n.module == "sqlglot.helper" and n.level == 0
                        and alias.name in _FREE_HELPERS and alias.asname is None):
                    helpers.add(name)
                else:
                    bound.add(name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
        elif isinstance(n, ast.MatchAs) and n.name:
            bound.add(n.name)
    return (_FREE_BUILTINS - bound) | (helpers - bound)


def _effect_free(node, names):
    """Evaluating ``node`` has no side effect the program can observe (it may raise)."""
    ef = lambda x: x is None or _effect_free(x, names)  # noqa: E731
    if isinstance(node, (ast.Name, ast.Constant)):
        return True
    if isinstance(node, ast.Attribute):
        return ef(node.value)
    if isinstance(node, ast.Subscript):
        return ef(node.value) and ef(node.slice)
    if isinstance(node, ast.Slice):
        return ef(node.lower) and ef(node.upper) and ef(node.step)
    if isinstance(node, ast.UnaryOp):
        return ef(node.operand)
    if isinstance(node, ast.BinOp):
        return ef(node.left) and ef(node.right)
    if isinstance(node, ast.BoolOp):
        return all(map(ef, node.values))
    if isinstance(node, ast.Compare):
        return ef(node.left) and all(map(ef, node.comparators))
    if isinstance(node, ast.IfExp):
        return ef(node.test) and ef(node.body) and ef(node.orelse)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(not isinstance(e, ast.Starred) and ef(e) for e in node.elts)
    if isinstance(node, ast.Call):
        if any(isinstance(a, ast.Starred) or not ef(a) for a in node.args):
            return False
        if any(k.arg is None or not ef(k.value) for k in node.keywords):
            return False
        f = node.func
        if isinstance(f, ast.Name):
            return f.id in names
        if isinstance(f, ast.Attribute):
            if f.attr in _FREE_METHODS:
                return ef(f.value)
            # Expression.args is a plain dict.
            return f.attr == "get" and isinstance(f.value, ast.Attribute) and f.value.attr == "args" and ef(f.value.value)
    return False


def _bool_sites(tree, names):
    """2-operand BoolOps whose value is only truth-tested, outside class bodies."""
    ctx = set()

    def mark(node):
        if isinstance(node, ast.BoolOp):
            ctx.add(id(node))
            for v in node.values:
                mark(v)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            mark(node.operand)

    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            mark(node.test)
        elif isinstance(node, ast.comprehension):
            for cond in node.ifs:
                mark(cond)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            mark(node.operand)

    def strip(node, in_class):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                strip(child, False)
            elif isinstance(child, ast.ClassDef):
                strip(child, True)
            else:
                if in_class:
                    ctx.discard(id(child))
                strip(child, in_class)

    strip(tree, False)
    sites = {}
    for node in ast.walk(tree):
        if id(node) in ctx and isinstance(node, ast.BoolOp) and len(node.values) == 2:
            b = node.values[1]
            sites[id(node)] = copy.deepcopy(b) if _effect_free(b, names) else None
    return sites


def _pure_returns(tree, names):
    return {id(n) for n in ast.walk(tree)
            if isinstance(n, ast.Return) and n.value is not None and _effect_free(n.value, names)
            and not (isinstance(n.value, ast.Constant) and n.value.value is None)}


_ORDER_OPS = {ast.Lt: 0, ast.LtE: 1, ast.Gt: 2, ast.GtE: 3}
original = SourceFileLoader.get_code


def get_code(loader, fullname):
    if not fullname.startswith("sqlglot.") and fullname != "sqlglot":
        return original(loader, fullname)
    source = loader.get_data(loader.path)
    tree = ast.parse(source, loader.path)
    names = _free_names(tree)
    bool_sites = _bool_sites(tree, names)
    pure_returns = _pure_returns(tree, names)

    class Replace(ast.NodeTransformer):
        changed = False

        def visit_BoolOp(self, node):
            pure_b = bool_sites.get(id(node), False)
            self.generic_visit(node)
            if pure_b is False:
                return node
            key = f"{fullname}-{node.lineno}-{node.col_offset}"
            a, b = node.values
            node.values = [ast.Call(ast.Name("_br_bool_pre", ast.Load()), [ast.Constant(key), a], []),
                           ast.Call(ast.Name("_br_bool_mid", ast.Load()), [ast.Constant(key), b], [])]
            lam = ast.Lambda(ast.arguments([], [], None, [], [], None, []), pure_b) if pure_b is not None else ast.Constant(None)
            self.changed = True
            return ast.copy_location(ast.Call(ast.Name("_br_bool_post", ast.Load()), [ast.Constant(key), node, lam], []), node)

        def visit_Return(self, node):
            pure = id(node) in pure_returns
            self.generic_visit(node)
            if pure:
                site = ast.Constant(f"{fullname}-{node.lineno}-{node.col_offset}")
                thunk = ast.Lambda(ast.arguments([], [], None, [], [], None, []), node.value)
                node.value = ast.Call(ast.Name("_br_ret", ast.Load()), [site, thunk], [])
                self.changed = True
            return node

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
    import atexit
    if _pertest:
        atexit.register(_dump_pertest)
    atexit.register(bool_flush)
    builtins._br_observe_identity = observe_identity
    builtins._br_observe_order = observe_order
    builtins._br_bool_pre = bool_pre
    builtins._br_bool_mid = bool_mid
    builtins._br_bool_post = bool_post
    builtins._br_ret = ret_obs
    builtins._br_bool_flush = bool_flush
    SourceFileLoader.get_code = get_code
