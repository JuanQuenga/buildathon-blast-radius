"""Price static evidence and bounded frontend fingerprints for the TPC-DS test."""
import ast
import functools
import json
from pathlib import Path
import tempfile
import time
import sys
from .schedule import plan_cost, schedule

from . import heavy, impact, predicate_domain, query_slice, static_guards, verify, warm_replay
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


def observe(text):
    files = parse(text)
    if not files or any(f.paths != {f.path} or not f.path.startswith("sqlglot/")
                        or not f.path.endswith(".py") or f.is_new or f.is_deleted
                        or f.is_rename or f.mode_change or f.is_binary for f in files):
        return "structural", ""
    changes = verify.apply_patch(text)
    if unread_option(text, "ds"):
        return "proved-option", ""
    if len(changes) == 1:
        path, after = next(iter(changes.items()))
        if overridden_defaults(path, impact._sources()["src"][path], after):
            return "proved-default", ""
    edit = heavy.stage_edit(text, "ds")
    mode, paths, _, cases = edit if edit else ("opaque", ",".join(changes), changes, [])
    if mode == "slice" and "sqlglot/executor/env.py" in changes:
        env = "sqlglot/executor/env.py"
        regions = heavy.changed_regions(text)
        others = {p: r for p, r in regions.items() if p != env} if regions else None
        if others is not None and query_slice.indices("query", others) == []:
            before = impact._sources()["src"][env]
            if predicate_domain.equivalent(before, changes[env], cases["worker"]):
                return "proved-predicate", mode
    if (mode == "rules" and len(changes) == 1 and
            static_guards.module_equivalent(impact._module_name(paths), impact._sources()["src"][paths],
                                            changes[paths], branch_sites())):
        return "proved-branch", mode
    if mode in {"unreached", "imported"} or mode == "slice" and not cases["active"]:
        return "proved-coverage", mode
    if mode == "rules":
        from . import rule_corpus
        if rule_corpus.estimate(cases) > 0.55:
            return "incomplete-rule-set", mode
        check, args = "rules", [json.dumps(cases)]
    elif mode in {"corpus", "tokens"}:
        check, args = mode, []
    elif mode == "slice":
        if cases["worker"] or cases["execute"]:
            return "execution-or-planning", mode
        if len(cases["active"]) > 4:
            return "incomplete-query-set", mode
        check, args = "frontend", [json.dumps(cases["active"])]
    else:
        return "unresolved", mode
    with tempfile.TemporaryDirectory() as work:
        verify.build_overlay(work, changes)
        # Fork with only the pinned root imported when the changed modules can
        # be loaded fresh. For an incompatible change use a fresh interpreter.
        warm_replay.prepare_core()
        result = heavy.replay(check, args, work, time.monotonic() + 2, paths, cpu_limit=0.8)
    return ("complete" if result == "pass" else "different-or-incomplete"), mode


def classify(text):
    try:
        return observe(text)
    except Exception:
        return "unavailable", ""

def refine(patches, catalog, durations, *, bundles=True):
    evidence_by_target = {}
    targets = {heavy.TARGETS[t["id"]]: t["number"] for t in catalog if t["id"] in heavy.TARGETS}
    start = heavy.cpu()
    for patch in patches:
        if patch.full:
            print("static-evidence", patch.commit, "full", file=sys.stderr)
            continue
        for group in ("ds", "generator"):
            number = targets.get(group)
            if number not in patch.req:
                continue
            required, plausible = patch.req - {number}, patch.pl - {number}
            alternative = schedule(required, plausible, durations) if required else []
            gain = (plan_cost(patch.workers, durations) - plan_cost(alternative, durations)) / 1000
            if gain <= 0 or any(number in worker for worker in alternative):
                print("static-evidence", patch.commit, group, "free", file=sys.stderr)
                continue
            if group == "ds":
                evidence, mode = classify(patch.text)
            else:
                try:
                    evidence = "proved-option" if unread_option(patch.text, group) else "unresolved"
                except Exception:
                    evidence = "unavailable"
                mode = ""
            evidence_by_target[patch, group] = evidence
            drop = evidence.startswith("proved-") or evidence == "complete"
            print("static-evidence", patch.commit, group, evidence, mode, round(gain, 3),
                  "drop" if drop else "keep", file=sys.stderr)
            if drop:
                patch.req, patch.pl, patch.workers = required, plausible, alternative
    if bundles:
        shrink_small_workers(patches, catalog, durations)
    print("static-refine-cpu", round(heavy.cpu()-start, 4), file=sys.stderr)
    return evidence_by_target


def shrink_small_workers(patches, catalog, durations):
    ids = {t["number"]: t["id"] for t in catalog}
    queue = []
    for patch in patches:
        if patch.full:
            continue
        try:
            queue.extend(p for p in heavy.options(patch, ids, (), durations) if p.group == "bundle")
        except Exception:
            continue
    # Limit the number of small real-test bundles, independent of wall load.
    for probe in sorted(queue, key=lambda p: (-p.gain / p.estimate, p.patch.commit))[:2]:
        try:
            warm_replay.prepare_core()
            with tempfile.TemporaryDirectory() as work:
                verify.build_overlay(work, verify.apply_patch(probe.patch.text))
                result = heavy.replay("bundle", [json.dumps(probe.cases)], work,
                                      time.monotonic() + 2, probe.paths, cpu_limit=0.55)
                if result != "pass":
                    continue
                passed = Path(work) / ".blast-passed.json"
                if passed.is_file():
                    nodes = json.loads(passed.read_text())
                    if not isinstance(nodes, list) or not set(nodes) < set(probe.cases):
                        continue
                    keep = {n for n, node in ids.items() if node in probe.cases and node not in nodes}
                    pl = probe.patch.pl - {n for n, node in ids.items() if node in probe.cases}
                    probe.patch.req, probe.patch.pl, probe.patch.workers = keep, pl, schedule(keep, pl, durations)
                else:
                    probe.patch.req, probe.patch.pl, probe.patch.workers = probe.proposed
        except Exception:
            continue
