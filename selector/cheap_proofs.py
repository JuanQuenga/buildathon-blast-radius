"""Cheap pin-derived noninterference checks before allocating a replay worker."""
import ast
import functools
import json
from pathlib import Path

from . import heavy, impact, predicate_domain, query_slice, static_guards, verify
from .patch import parse
from .rule_defaults import overridden_defaults

@functools.cache
def branch_sites():
    return json.loads((Path(__file__).parent / "maps/branches.json").read_text())[
        "tests/test_executor.py::TestExecutor::test_execute_tpcds"]


def unread_option(text, group):
    files = [f for f in parse(text) if f.hunks or f.is_new or f.is_deleted or f.is_rename or f.mode_change]
    if not files or any(f.is_binary or f.is_new or f.is_deleted or f.mode_change or f.is_rename
                        or f.paths != {f.path} for f in files):
        return False
    changes = verify.apply_patch(text)
    return all(option_file(f.path, changes[f.path], group) for f in files)


@functools.cache
def generator_reads():
    return set(json.loads((Path(__file__).parent / "maps/generator-options.json").read_text()))


def option_file(path, after, group):
    is_generator = group == "generator" and path.startswith("sqlglot/generators/")
    if not path.startswith("sqlglot/dialects/") and not is_generator:
        return False
    source = impact._sources()["src"][path]
    old, new = ast.parse(source), ast.parse(after)
    module = impact._module_name(path)
    reads, blocked = option_reads(group)
    if module in blocked:
        return False
    changed = False
    for a, b in zip(old.body, new.body):
        if not isinstance(a, ast.ClassDef) or not isinstance(b, ast.ClassDef):
            continue
        if not any(isinstance(base, ast.Name) and (base.id == "Dialect" or
                   is_generator and base.id.endswith("Generator")) for base in a.bases):
            continue
        for x, y in zip(a.body, b.body):
            if (isinstance(x, ast.Assign) and isinstance(y, ast.Assign) and len(x.targets) == 1
                    and isinstance(x.targets[0], ast.Name) and x.targets[0].id.isupper()
                    and isinstance(x.value, ast.Constant) and isinstance(y.value, ast.Constant)
                    and type(x.value.value) is bool and type(y.value.value) is bool
                    and x.value.value != y.value.value and module + ":" + x.targets[0].id not in reads
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
