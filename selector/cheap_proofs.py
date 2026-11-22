"""Cheap pin-derived noninterference checks before allocating a replay worker."""
import ast
import functools
import json
import re
from pathlib import Path

from . import heavy, impact, predicate_domain, query_slice, static_guards, verify
from .patch import parse
from .rule_defaults import overridden_defaults

_BOOL_OPT_LINE_RE = re.compile(r"^\s+[A-Z][A-Z0-9_]*\s*=\s*(?:True|False)\s*(?:#.*)?$")


def _hunks_only_bool_options(fd) -> bool:
    """Fast O(hunk_lines) pre-filter avoiding full-file ast.parse on non-option patches."""
    if not fd.hunks:
        return False
    saw_change = False
    for h in fd.hunks:
        for raw in h.lines:
            if raw[:1] not in {"+", "-"}:
                continue
            body = raw[1:]
            if not body.strip() or body.lstrip().startswith("#"):
                continue
            if not _BOOL_OPT_LINE_RE.match(body):
                return False
            saw_change = True
    return saw_change


@functools.cache
def branch_sites():
    return json.loads((Path(__file__).parent / "maps/branches.json").read_text())[
        "tests/test_executor.py::TestExecutor::test_execute_tpcds"]


def unread_option(text, group):
    files = [f for f in parse(text) if f.hunks or f.is_new or f.is_deleted or f.is_rename or f.mode_change]
    if not files or any(f.is_binary or f.mode_change for f in files):
        return False
    sql_files = [f for f in files if f.path and f.path.startswith("sqlglot/") and f.path.endswith(".py")]
    non_sql_files = [f for f in files if f not in sql_files]
    if not sql_files:
        return False
    if any(f.is_new or f.is_deleted or f.is_rename or f.paths != {f.path} for f in sql_files):
        return False
    for f in sql_files:
        is_gen = group == "generator" and f.path.startswith("sqlglot/generators/")
        if not f.path.startswith("sqlglot/dialects/") and not is_gen:
            return False
        if not _hunks_only_bool_options(f):
            return False
    if non_sql_files and not all(heavy._target_unreachable(f, group) for f in non_sql_files):
        return False
    changes = verify.apply_patch(text)
    return all(option_file(f.path, changes[f.path], group) for f in sql_files)


@functools.cache
def generator_reads():
    return set(json.loads((Path(__file__).parent / "maps/generator-options.json").read_text()))


def option_file(path, after, group):
    is_generator = group == "generator" and path.startswith("sqlglot/generators/")
    if not path.startswith("sqlglot/dialects/") and not is_generator:
        return False
    source = impact._sources()["src"].get(path)
    if source is None:
        return False
    old, new = ast.parse(source), ast.parse(after)
    module = impact._module_name(path)
    reads, blocked = option_reads(group)
    if module in blocked:
        return False
    mro = impact._index().get("mro", {})
    changed = False
    for a, b in zip(old.body, new.body):
        if not isinstance(a, ast.ClassDef) or not isinstance(b, ast.ClassDef) or a.name != b.name:
            continue
        cls_full = f"{module}.{a.name}"
        bases_mro = mro.get(cls_full, ())
        is_dialect_cls = (
            "sqlglot.dialects.dialect.Dialect" in bases_mro
            or any(isinstance(base, ast.Name) and base.id == "Dialect" for base in a.bases)
        )
        is_gen_cls = is_generator and (
            "sqlglot.generator.Generator" in bases_mro
            or any(isinstance(base, ast.Name) and base.id.endswith("Generator") for base in a.bases)
        )
        if not (is_dialect_cls or is_gen_cls):
            continue
        # Subclasses inherit class attributes; verify neither this module nor any
        # subclass module is blocked or read the modified attribute.
        affected_mods = {module} | {
            c.rsplit(".", 1)[0] for c, m in mro.items() if cls_full in m
        }
        if affected_mods & blocked:
            return False
        for x, y in zip(a.body, b.body):
            if (isinstance(x, ast.Assign) and isinstance(y, ast.Assign) and len(x.targets) == 1
                    and isinstance(x.targets[0], ast.Name) and x.targets[0].id.isupper()
                    and isinstance(x.value, ast.Constant) and isinstance(y.value, ast.Constant)
                    and type(x.value.value) is bool and type(y.value.value) is bool
                    and x.value.value != y.value.value
                    and all(m + ":" + x.targets[0].id not in reads for m in affected_mods)
                    and (not is_generator or x.targets[0].id not in generator_reads())):
                y.value = x.value
                changed = True
    return changed and ast.dump(old) == ast.dump(new)


@functools.cache
def option_reads(group):
    reads, blocked = json.loads((Path(__file__).parent / f"maps/attrs-{group}.json").read_text())
    return set(reads), set(blocked)



def proven(text, group, edit):
    if group not in {"ds", "generator"}:
        return False
    if unread_option(text, group):
        return True
    if edit is None or group != "ds":
        return False
    mode, paths, changes, cases = edit
    if mode == "fixture-query":
        return False
    if len(changes) == 1:
        before = impact._sources()["src"][paths]
        if overridden_defaults(paths, before, changes[paths]):
            return True
        if mode == "rules" and static_guards.module_equivalent(
            impact._module_name(paths), before, changes[paths], branch_sites()
        ):
            return True
    env = "sqlglot/executor/env.py"
    if mode == "slice" and env in changes:
        regions = heavy.changed_regions(text)
        if regions and query_slice.indices("query", {p:r for p,r in regions.items() if p != env}) == []:
            return predicate_domain.equivalent(impact._sources()["src"][env], changes[env], cases["worker"])
    return False
