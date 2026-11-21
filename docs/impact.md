# Impact analysis (`selector/impact.py`)

`select(patch_text, catalog) -> {"required", "plausible", "full", "reasons"}`
decides, for one unified diff against the pinned sqlglot
(`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`), which catalog tests can change
outcome. It is pure standard library, loads its maps once per process
(~50 ms) and spends a few ms per patch (largest files: ~100 ms).

The economics drive every choice: a missed failing test costs 300 s, the whole
suite 36.8 s, an empty plan 0 s. A narrowing that saves Δ seconds is worth it
only if it adds less than Δ/300 miss probability, so the selector is built to
be **sound by construction** wherever it can be and widens (ultimately to the
full suite) wherever it cannot prove a bound.

## Maps (built offline from the pin only)

`tools/` traces the pinned test suite (Python 3.13, the starter
`requirements.lock`, `SKIP_INTEGRATION=0`) with a pytest plugin. Each of the
1,621 collected tests runs in its own fork after collection, so lazy dialect
imports, caches and `setUpClass` work are attributed to *every* test that needs
them. Recorded per test:

* **executed lines** of `sqlglot/**` and `tests/**` (`sys.monitoring` LINE
  events). Lines run while a test *file* is collected (imports, class bodies)
  are attributed to every test of that file;
* **files opened** and directories listed (audit hook, import system excluded):
  fixtures, CSVs, `sqlglot/typing/*.py` read by `test_typing`,
  `sqlglotc/setup.py` run by `test_sqlglotc`;
* **classes** instantiated (wrapped `__init__`) or whose class object was used
  (metaclass `__getattribute__`), with their MRO -> "class family" users;
* the test class MRO (e.g. every dialect test is a `Validator`).

The optimizer/executor tests ship work to a spawn `ProcessPoolExecutor`; the
tracer swaps in an in-process executor that still pickles arguments and results,
because pickling runs `sqlglot.serde` (this is how `serde.py` reaches 37 tests
in 6 files, sample `3utz`).

Four runs are unioned: two forked runs with different hash seeds and two
sequential (warm cache, real order) runs. They agree exactly: the sequential
and second-seed runs add **zero** line/test pairs, so coverage on this suite is
deterministic. All 1,621 tests pass under tracing.

Also baked: per-file def/class scopes and multi-line statement spans, the
module-level import graph, module/class namespaces and the MRO of every sqlglot
class (from importing the package), and the pinned sources themselves
(`sqlglot/**.py`, `tests/**.py`, `pyproject.toml`, `setup.cfg`). Total
`selector/maps/` is about 1.3 MB.

## Decision policy

The patch is parsed (git/unified diffs, `a/ b/`, mnemonic `c/ i/` and no
prefixes, renames, adds, deletes, binary markers), applied to the pinned source
(exact context, small offset search) and both versions are parsed with `ast`.
Statements are aligned with a tree diff (`difflib` over statement dumps without
positions), recursing into matching compound statements, so **comment,
blank-line and formatting changes produce no semantic change at all**.

| Change | Selected tests |
| --- | --- |
| statement inside a function body | tests that executed that statement on the pin (a test that never reaches it cannot observe it) |
| changed `if`/`while`/loop/`with`/`except` header | tests that executed the header |
| inserted statement(s) | tests reaching the insertion point: previous sibling, else first statement of the block, else the owning header (new `else`/`elif` branches) |
| def signature / decorators | tests that ran the body (+ class family or name references for decorator changes) |
| class attribute (dispatch tables, constants, `arg_types`, ...) | class family (tests instantiating or touching the class or a subclass) **∩** tests executing a textual read of that attribute; plus test code reading it, plus users of classes whose class body derives from it (subclasses or explicit `Cls.NAME`) |
| new / removed / renamed method, base classes | whole class family (dispatch by name, e.g. `*_sql`, MRO) |
| module-level binding (constants, imports) | tests reaching references of the changed names. References are resolved per file through its imports (`from m import x as y`, `import m as alias`, star re-exports, relative imports): runtime references by coverage, class-body references by class family, module-level re-exports recursively |
| strings in a module with `__getattr__`/`__all__` (lazy exports, e.g. `DIALECTS`) | treated as exported names added/removed |
| new module-level import edge | cycle check + tests observing import state (`sys.modules`, dialect registry, subprocess lazy-load test) |
| docstring only | nothing (doctests are not in the catalog: `test_docs.py` uses `load_tests`) |
| statement never executed by any test (dead code, `TYPE_CHECKING`) | nothing |
| source file read at runtime (`sqlglot/typing/*.py`) | its readers, even for comment edits |
| fixture / data file | tests that opened it; added/removed/renamed files also tests that listed its directory |
| non-code files nobody reads (README, CHANGELOG, posts, Makefile, `py.typed`) | nothing |
| `pyproject.toml` / `setup.cfg` | nothing unless the pytest sections (or build-system) change, then full |
| test method body | tests executing those lines (the test itself; helpers reach all their callers) |
| new test method | nothing (not in the catalog) |
| removed / renamed test, deleted test file | the vanished tests (their ids go missing) + importers |
| `conftest.py`, `pytest.ini`, `tox.ini` | full |

**Import breakage.** A change that may make a module fail to import — syntax
error, deleted/renamed module, removed name that someone imports, unresolvable
name or attribute evaluated at import (checked against the baked namespaces),
unknown import, new import cycle, module-level statement that may raise, removed
copy at import time (`X = PARENT.copy()` → aliasing shared state) — hits every
test that imported the module. If **any test file imports that module while
being collected**, the selector returns `full`: plain pytest aborts the whole
session on a collection error, and the official handling of that case is not
published, so only the full suite is safe. Otherwise (module imported lazily at
runtime, e.g. most dialect modules) every runtime importer is required.

`full=True` is also returned for unparseable patches, hunks that do not apply
to the pin, binary changes to Python files, unknown Python paths and modified
pytest configuration. Catalog tests unknown to the maps are always required.
`plausible` = every test that entered a changed function block (free insurance
the scheduler adds only into slack).

Nothing in the selector or the maps comes from sample or benchmark answers;
the maps are coverage/imports/fixture reads of the pinned checkout. The
dialect-specific behaviour (inheritance Hive → Spark2 → Spark → Databricks,
Presto → Trino/Athena, parser/generator/tokenizer dispatch tables) is *derived*
from class families and attribute reads, not from hand-written tables.

## Measured (sample + bench `exotic` set, labels used for measurement only)

`python3 tools/eval_impact.py eval/cases/sample` (selector.schedule, k-search):

| Set | Patches | Misses | Full fallbacks | Simulated | Optimal (labels) | Full suite | Selector CPU |
| --- | --- | --- | --- | --- | --- | --- | --- |
| official sample | 30 | **0** | 0 | 166.0 s | 94.4 s | 1,104.5 s | 0.7 s |
| bench `exotic` (all) | 63 | **0** | 22 | 1,068 s | 939 s | 2,320 s | 0.5 s |

End to end through `selector.main` on the sample (`eval/run.py --cases
sample`): 166.0 s simulated + 0.73 s CPU = 166.8 s, 0 misses.

Audit cases from the brief:

* `ary6` (BigQuery `if rest and this` → `or`): the `if` header was executed
  by exactly 10 tests on the pin — the 10 failing tests in 4 files. 4.4 s
  (= optimal).
* `535z` (T-SQL `WEEK_OFFSET`): class family of `TSQL` (171 tests, incl.
  `test_generator_memory`) ∩ tests that execute a read of `WEEK_OFFSET` →
  7 tests incl. the failing `test_simplify`; 2.9 s (optimal 2.6 s).
* `3utz` (`serde.dump`): 37 tests, exactly the failing set, via the pickled
  pool round trip; 25.5 s (= optimal, `test_execute_tpcds` is among them).

Most of the remaining gap to optimal is patches whose changed line is executed
by `test_execute_tpcds`/`test_max_nodes` but that do not actually break them
(e.g. `2haz` `is` → `==` on an enum, `66ks` in the executor): coverage cannot
prove those harmless.

## Known gaps / risks

* **Collection errors → full.** If the official labeler continues past
  collection errors (`--continue-on-collection-errors`, as the bench oracle now
  does), these patches could be narrowed to the importing files; we pay ~30 s
  per such patch for not betting on it.
* Class-object reads of non-`Dialect` classes without instantiation and
  without a textual attribute read in test code, and reads by computed
  attribute names, are invisible to the class-attribute refinement (generator
  `*_sql` dispatch via `dir()` is handled by widening to the family).
* Cross-test state: a change executed by test A that corrupts shared state
  observed by a later test B in the same process is only caught for
  import-time copies; runtime aliasing inside function bodies is not modelled.
* Behaviour that depends on the host (SQLite version: one officially skipped
  test) is traced on SQLite 3.53; the image's SQLite may take other paths in
  sqlite-executing tests.
* New statements that only become reachable through *new* dispatch keys (e.g.
  a key added to a dict built inside a function) are covered through the
  enclosing statement's coverage, not by key.
