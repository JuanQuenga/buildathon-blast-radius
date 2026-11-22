"""Sound per-key observation projection for SQLGlot dispatch dictionaries.

Uses ``selector/maps/dict-keys.json.gz`` (exact per-test key lookups — both
hits and misses — plus bulk reads on the pristine pin) to intersect static
attribute/module reader sets down to only tests that actually looked up a
changed key or performed a bulk operation on the dictionary.
"""
from __future__ import annotations

import ast
import functools
import gzip
import json
import os
from pathlib import Path

TABLES = frozenset({
    "TRANSFORMS",
    "TYPE_MAPPING",
    "PROPERTIES_LOCATION",
    "FUNCTIONS",
    "FUNCTION_PARSERS",
    "STATEMENT_PARSERS",
    "PROPERTY_PARSERS",
    "UNARY_PARSERS",
    "EXPRESSION_METADATA",
})

_STR_TABLES = frozenset({"FUNCTIONS", "FUNCTION_PARSERS", "PROPERTY_PARSERS"})
_EXPR_TABLES = frozenset({"TRANSFORMS", "PROPERTIES_LOCATION", "EXPRESSION_METADATA"})
_DTYPE_TABLES = frozenset({"TYPE_MAPPING"})
_TOKEN_TABLES = frozenset({"STATEMENT_PARSERS", "UNARY_PARSERS"})


def _maps_dir() -> Path:
    return Path(os.environ.get("BLAST_MAPS", str(Path(__file__).resolve().parent / "maps")))


@functools.cache
def _load():
    import marshal
    cache = Path(os.environ.get("BLAST_APP_DIR", "/app")) / "pin-cache" / "dict-keys.marshal"
    if cache.is_file():
        return marshal.loads(cache.read_bytes())
    path = _maps_dir() / "dict-keys.json.gz"
    if not path.is_file():
        return None
    data = json.loads(gzip.decompress(path.read_bytes()))
    bulk = {}
    for table, idxs in data.get("bulk", {}).items():
        bits = 0
        for i in idxs:
            bits |= 1 << i
        bulk[table] = bits
    keys = {}
    for table, mapping in data.get("keys", {}).items():
        tmap = {}
        for k, idxs in mapping.items():
            bits = 0
            for i in idxs:
                bits |= 1 << i
            tmap[k] = bits
        keys[table] = tmap
    return bulk, keys


def _decode_key(node: ast.AST, table: str) -> str | None:
    if table in _STR_TABLES:
        if isinstance(node, ast.Constant) and type(node.value) is str:
            return "S:" + node.value
        return None
    if table in _EXPR_TABLES:
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "exp"
        ):
            return "E:" + node.attr
        return None
    if table in _DTYPE_TABLES:
        if isinstance(node, ast.Attribute):
            # exp.DType.FOO or exp.DataType.Type.FOO or DataType.Type.FOO
            base = node.value
            if (
                isinstance(base, ast.Attribute)
                and base.attr in {"DType", "Type"}
            ):
                return "D:" + node.attr
        return None
    if table in _TOKEN_TABLES:
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "TokenType"
        ):
            return "T:" + node.attr
        return None
    return None


def _filtered_base_unpack(v: ast.AST, table: str) -> tuple[str, set[str]] | None:
    """Match ``{k: v for k, v in <BASE>.items() if k != KEY}`` or
    ``{k: v for k, v in <BASE>.items() if k not in (KEY1, KEY2)}``.
    Returns ``(base_dump, excluded_key_tokens)`` or ``None``."""
    if (
        not isinstance(v, ast.DictComp)
        or len(v.generators) != 1
        or len(v.generators[0].ifs) != 1
        or v.generators[0].is_async
    ):
        return None
    gen = v.generators[0]
    if not (
        isinstance(gen.target, ast.Tuple)
        and len(gen.target.elts) == 2
        and isinstance(gen.target.elts[0], ast.Name)
        and isinstance(gen.target.elts[1], ast.Name)
    ):
        return None
    k_id, v_id = gen.target.elts[0].id, gen.target.elts[1].id
    if not (
        isinstance(v.key, ast.Name)
        and v.key.id == k_id
        and isinstance(v.value, ast.Name)
        and v.value.id == v_id
    ):
        return None
    if not (
        isinstance(gen.iter, ast.Call)
        and isinstance(gen.iter.func, ast.Attribute)
        and gen.iter.func.attr == "items"
        and not gen.iter.args
        and not gen.iter.keywords
    ):
        return None
    cond = gen.ifs[0]
    if not (
        isinstance(cond, ast.Compare)
        and len(cond.ops) == 1
        and len(cond.comparators) == 1
        and isinstance(cond.left, ast.Name)
        and cond.left.id == k_id
    ):
        return None
    op, rhs = cond.ops[0], cond.comparators[0]
    excluded: set[str] = set()
    if isinstance(op, ast.NotEq):
        ek = _decode_key(rhs, table)
        if ek is None:
            return None
        excluded.add(ek)
    elif isinstance(op, ast.NotIn) and isinstance(rhs, (ast.Tuple, ast.Set, ast.List)):
        for elt in rhs.elts:
            ek = _decode_key(elt, table)
            if ek is None:
                return None
            excluded.add(ek)
    else:
        return None
    return ast.dump(gen.iter.func.value, include_attributes=False), excluded


def _extract_entries(node: ast.AST, table: str) -> tuple[list[str], list[set[str]], dict[str, str]] | None:
    """Return ``(base_unpack_dumps, unpack_exclusions, {key_str: value_dump})``
    or ``None`` if unsupported."""
    if not isinstance(node, ast.Dict):
        return None
    unpacks: list[str] = []
    exclusions: list[set[str]] = []
    entries: dict[str, str] = {}
    for k, v in zip(node.keys, node.values):
        if k is None:
            # Handle {**{k: val for k in {exp.A, exp.B}}} used in typing/generator maps.
            if (
                isinstance(v, ast.DictComp)
                and len(v.generators) == 1
                and not v.generators[0].ifs
                and not v.generators[0].is_async
                and isinstance(v.generators[0].target, ast.Name)
                and isinstance(v.key, ast.Name)
                and v.key.id == v.generators[0].target.id
                and isinstance(v.generators[0].iter, (ast.Set, ast.Tuple, ast.List))
            ):
                val_dump = ast.dump(v.value, include_attributes=False)
                for elt in v.generators[0].iter.elts:
                    ek = _decode_key(elt, table)
                    if ek is None:
                        return None
                    entries[ek] = val_dump
            elif (
                isinstance(v, ast.Call)
                and isinstance(v.func, ast.Attribute)
                and isinstance(v.func.value, ast.Name)
                and v.func.value.id == "dict"
                and v.func.attr == "fromkeys"
                and len(v.args) == 2
                and not v.keywords
                and isinstance(v.args[0], (ast.Set, ast.Tuple, ast.List))
            ):
                val_dump = ast.dump(v.args[1], include_attributes=False)
                for elt in v.args[0].elts:
                    ek = _decode_key(elt, table)
                    if ek is None:
                        return None
                    entries[ek] = val_dump
            else:
                filt = _filtered_base_unpack(v, table)
                if filt is not None:
                    base_dump, excl = filt
                    unpacks.append(base_dump)
                    exclusions.append(excl)
                else:
                    unpacks.append(ast.dump(v, include_attributes=False))
                    exclusions.append(set())
        else:
            ek = _decode_key(k, table)
            if ek is None:
                return None
            entries[ek] = ast.dump(v, include_attributes=False)
    return unpacks, exclusions, entries


def _single_assign_rhs(stmts: list[ast.stmt], target_name: str) -> ast.AST | None:
    non_doc = [
        s for s in stmts
        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
    ]
    if len(non_doc) != 1:
        return None
    s = non_doc[0]
    if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
        return s.value if s.targets[0].id == target_name else None
    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) and s.value is not None:
        return s.value if s.target.id == target_name else None
    return None


def changed_keys(table: str, o_stmts: list[ast.stmt], n_stmts: list[ast.stmt]) -> set[str] | None:
    """If ``o_stmts`` -> ``n_stmts`` only changes literal entries or filtered
    base-dict exclusions of ``table`` (preserving all ``**base`` unpacks),
    return the exact set of changed key tokens."""
    if table not in TABLES:
        return None
    o_rhs = _single_assign_rhs(o_stmts, table)
    n_rhs = _single_assign_rhs(n_stmts, table)
    if o_rhs is None or n_rhs is None:
        return None
    old_parsed = _extract_entries(o_rhs, table)
    new_parsed = _extract_entries(n_rhs, table)
    if old_parsed is None or new_parsed is None:
        return None
    o_unpacks, o_excls, o_entries = old_parsed
    n_unpacks, n_excls, n_entries = new_parsed
    if o_unpacks != n_unpacks:
        return None
    delta = {
        k for k in (o_entries.keys() | n_entries.keys())
        if o_entries.get(k) != n_entries.get(k)
    }
    for oe, ne in zip(o_excls, n_excls):
        delta |= oe ^ ne
    return delta


def narrow_bits(table: str, bits: int, o_stmts: list[ast.stmt], n_stmts: list[ast.stmt]) -> int | None:
    """Return narrowed bitmask ``bits & (bulk[table] | union(obs[k]))`` or ``None``."""
    loaded = _load()
    if loaded is None:
        return None
    delta = changed_keys(table, o_stmts, n_stmts)
    if delta is None:
        return None
    bulk, keys = loaded
    obs = bulk.get(table, 0)
    tmap = keys.get(table, {})
    for k in delta:
        obs |= tmap.get(k, 0)
    return bits & obs


@functools.cache
def _expr_key_to_classes() -> dict[str, set[str]]:
    """Map every pinned ``exp.Expr`` subclass's ``.key`` to its class name(s)."""
    from .impact import _sources

    mapping: dict[str, set[str]] = {}
    for path, src in _sources()["src"].items():
        if not path.startswith("sqlglot/expressions/") or not path.endswith(".py"):
            continue
        tree = ast.parse(src)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            key = node.name.lower()
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and stmt.targets[0].id == "key"
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    key = stmt.value.value
            mapping.setdefault(key, set()).add(node.name)
    return mapping


def sql_method_bits(method_name: str) -> int | None:
    """Bitmask of tests that can dynamically dispatch ``<key>_sql`` on a Generator.

    Every ``Generator.sql()`` dynamic dispatch first checks ``self.TRANSFORMS.get(type(expr))``.
    Thus ``bulk['TRANSFORMS'] | union(keys['TRANSFORMS']['E:' + cls])`` over all
    pinned ``exp.Expr`` classes with matching ``.key`` is a sound superset of all
    tests that can dynamically invoke ``method_name``.
    """
    if not method_name.endswith("_sql") or method_name.startswith("_"):
        return None
    key = method_name[:-4]
    if not key or key in {"and", "or", "connector"}:
        return None
    loaded = _load()
    if loaded is None:
        return None
    bulk, keys = loaded
    obs = bulk.get("TRANSFORMS", 0)
    tmap = keys.get("TRANSFORMS", {})
    for cls_name in _expr_key_to_classes().get(key, ()):
        obs |= tmap.get("E:" + cls_name, 0)
    return obs
