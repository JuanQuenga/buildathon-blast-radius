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


def _extract_entries(node: ast.AST, table: str) -> tuple[list[str], dict[str, str]] | None:
    """Return ``(base_unpack_dumps, {key_str: value_dump})`` or ``None`` if unsupported."""
    if not isinstance(node, ast.Dict):
        return None
    unpacks: list[str] = []
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
            else:
                unpacks.append(ast.dump(v, include_attributes=False))
        else:
            ek = _decode_key(k, table)
            if ek is None:
                return None
            entries[ek] = ast.dump(v, include_attributes=False)
    return unpacks, entries


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
    """If ``o_stmts`` -> ``n_stmts`` only changes literal entries of ``table``
    (preserving all ``**base`` unpacks), return the exact set of changed key tokens."""
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
    o_unpacks, o_entries = old_parsed
    n_unpacks, n_entries = new_parsed
    if o_unpacks != n_unpacks:
        return None
    delta = {
        k for k in (o_entries.keys() | n_entries.keys())
        if o_entries.get(k) != n_entries.get(k)
    }
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
