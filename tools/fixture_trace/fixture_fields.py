import ast
from collections import defaultdict
import dis
import functools
import inspect
import json
from pathlib import Path
import sys

import pytest
from tests import helpers
import sitecustomize as trace

fields = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
sizes = {}
tables = defaultdict(set)
owners = defaultdict(set)


@functools.cache
def discarded(code, offset):
    op = next((i for i in dis.get_instructions(code) if i.offset == offset), None)
    if not op or op.opname != "UNPACK_SEQUENCE":
        return set()
    tree = ast.parse(Path(code.co_filename).read_text())
    pos = op.positions
    target = next((n for n in ast.walk(tree) if isinstance(n, ast.Tuple) and
                   (n.lineno, n.col_offset, n.end_lineno, n.end_col_offset) ==
                   (pos.lineno, pos.col_offset, pos.end_lineno, pos.end_col_offset)), None)
    if target is None or not all(isinstance(n, ast.Name) for n in target.elts):
        return set()
    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.lineno == code.co_firstlineno and n.name == code.co_name), None)
    if fn is None or any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                         and n.id in {"_", "locals", "eval", "exec"} for n in ast.walk(fn)):
        return set()
    return {i for i, n in enumerate(target.elts) if n.id == "_"}


class Entry:
    def __init__(self, filename, index, values):
        self.filename, self.index, self.values = filename, index, values

    def mark(self, mask):
        fields[self.filename][trace.node or "*"][self.index] |= mask

    def __iter__(self):
        frame = sys._getframe(1)
        unused = discarded(frame.f_code, frame.f_lasti)
        self.mark(sum(1 << i for i in range(3) if i not in unused))
        return iter(self.values)

    def __getitem__(self, key):
        self.mark(1 << (key % 3) if type(key) is int else 7)
        return self.values[key]

    def __reduce__(self):
        self.mark(7)
        return tuple, (self.values,)


def wrap(loader, paired):
    @functools.wraps(loader)
    def tracked(filename):
        trace.loading = str(Path(helpers.FIXTURES_DIR, filename))
        try:
            values = list(loader(filename))
        finally:
            trace.loading = None
        sizes[filename] = [paired, len(values)]
        for i, value in enumerate(values):
            if paired:
                yield Entry(filename, i, value)
            else:
                fields[filename][trace.node or "*"][i] |= 2
                yield value
    return tracked


helpers.load_sql_fixture_pairs = wrap(helpers.load_sql_fixture_pairs, True)
helpers.load_sql_fixtures = wrap(helpers.load_sql_fixtures, False)
from tests.test_executor import TestExecutor
original_setup = TestExecutor.setUpClass.__func__


def setup(cls):
    groups = defaultdict(set)
    def called(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "setup" and frame.f_back.f_code is original_setup.__code__:
            local = frame.f_locals
            name = str(Path(local["directory"], local["table"] + ".csv.gz").relative_to(helpers.FIXTURES_DIR))
            for field in ("conn", "tables"):
                groups[id(local[field])].add(name)
    sys.setprofile(called)
    try:
        original_setup(cls)
    finally:
        sys.setprofile(None)
    for name, value in vars(cls).items():
        if id(value) in groups:
            tables[name].update(groups[id(value)])


TestExecutor.setUpClass = classmethod(setup)
original_get = TestExecutor.__getattribute__


def get(self, name):
    if name in tables:
        owners[trace.node or "*"].update(tables[name])
    return original_get(self, name)


TestExecutor.__getattribute__ = get


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item):
    trace.node = item.nodeid
    yield
    trace.node = None


def pytest_sessionfinish(session, exitstatus):
    if exitstatus == 0:
        Path("/out/profile.json").write_text(json.dumps([sizes, fields, {k: sorted(v) for k,v in owners.items()},
                                                         sorted(set().union(*tables.values()))]))
