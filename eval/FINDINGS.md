# Findings that matter for selection

Measured with `eval/oracle.py` on the pin (Python 3.13.18 venv + starter
`requirements.lock`, `SKIP_INTEGRATION=0`). Regenerate the tables with
`python3 eval/analyze.py --details`. Benchmark answers are measurement only —
never put them (or tables derived from them) into the selector or the image.

## Oracle validity

- **The official sample reproduces exactly: 30/30 patches, identical failing
  sets** (51 failing ids, incl. the 37-test `3utz`). No flake seen.
- Two clean runs of the whole suite under load: 0 failures, ~77 s wall alone,
  ~120 s with 6 concurrent runs. Pytest collects exactly the 1,621 catalog ids
  (1,620 numbered + `test_variadic_iif_execution`, skipped officially because the
  image's SQLite is < 3.49; our uv Python bundles SQLite 3.53, so it *runs* here
  — irrelevant, it has no number).
- **Collection errors abort the whole pytest session by default** ("Interrupted:
  1 error during collection"): every other test is then *not run*. The official
  rule ("unimportable test file → all tests in that file fail") implies the
  labeler continued past collection errors, so the oracle uses
  `--continue-on-collection-errors`. Without it, e.g. dropping one name from
  `sqlglot/__init__.py` that only `tests/test_build.py` imports labels all 1,620
  tests failing instead of 1.
- Renamed/deleted catalog tests never report; the oracle counts them as failing
  (`missing` in `oracle-details`). Official handling is unpublished; selecting
  them is cheap insurance.
- `tests/dialects/test_dialect.py::test_lazy_load` shells out to `python -c`
  (whatever `python` is on PATH) — the oracle puts the 3.13 venv first.

## Structure of the suite that matters

- **`tests/test_docs.py` contributes no catalog tests**: it uses the unittest
  `load_tests` protocol, which pytest ignores. Docstring examples (doctests) are
  therefore *never run* by the scorer; editing a docstring cannot fail a doctest.
- **`test_generator_memory` (6.7 s) imports every dialect** (`for name in
  Dialects: Dialect.get_or_raise(name)`) and builds 1,100 generators per dialect.
  Any import-time failure in any dialect / parser / generator / typing module, a
  dropped `DIALECTS` registration, or a leak in generator construction fails it.
  `test_time.py::test_canonical_time_format_vocabulary` and
  `test_dialect.py::test_enum` iterate all dialects the same way.
- **`TestExecutor.setUpClass` loads `tpc-h.sql` and `tpc-ds.sql` fixture pairs
  and the CSVs**: breaking/deleting those fixture files errors the class setup
  → all 60 executor tests (incl. `test_execute_tpcds`, 21.5 s) fail.
- `test_typing.py::test_no_redundant_expression_metadata` reads every
  `sqlglot/typing/*.py` source with `ast` and compares annotator code objects
  (`co_code`, `co_consts`, …) between dialects and parents: a typing edit that
  makes a child entry equal to its parent's fails it, even in an unrelated dialect.
- `test_sqlglotc.py` (3 tests) executes `sqlglotc/setup.py` → that "build-only"
  Python file is test-relevant.
- Fixture loaders (`tests/helpers.py`): only lines *starting* with `--` are
  dropped; a `-- comment` after SQL on the same line stays in the statement.
  `#` lines are metadata only at the start of a statement (`key: value`); a
  `#` line without `:` raises. Statements are split on `;`, so an extra `;`
  shifts every following input/expected pair in that file.
- `test_dialect_entry_points.py` clears `Dialect._classes` in setUp/tearDown —
  potential order dependence if tests are run in a different order/subset.

## Audited development labels at 60c75b8

- Identify the expensive parser test by its full catalog ID. Number 1490 in TestParser costs 28.817 s and exercises the default parser's AST budget on a million-node expression. TestAthena has a separate test_max_nodes, number 9, costing 0.001 s. None of the completed development exotic runs with exit 1 and fewer than 1,620 failures failed number 1490. Missing or broken shared imports can still prevent collection.
- Among those same development runs, three broke number 1106 (TPC-DS). The recorded exceptions in executor workers traced to a Trino parser rename, a missing Presto typing module and invalid Trino generator syntax. Fifteen broke number 1224 (generator memory) through failed imports, deletions, renames or missing dialect registration.
- The previous load-58 run exceeded a 180-second timeout on the large parser test. A timeout retry replaces the first outcome, so wall time and first-run timeout evidence matter. An interrupted pytest session can produce many missing reports. Those rows require separate treatment.

## Quiet-host timeout recheck

One cached patch carried timeout evidence and occurred in two sets. Its archived failure list differed from the independent quiet-host run. The timeout did not reproduce. The current holdout labels already reflect the corrected failure list, and the cross-set duplicate is excluded from the 150-patch comparison. The original record and the independent recheck remain in the private workbench for audit.

## Development measurements

Counts cover complete pytest sessions or the official sample. Each row uses only the development partition. The percentage is the share of labelled patches with zero numbered failures.

| Set | Labelled / planned | Clean | Clean share |
|---|---:|---:|---:|
| dev | 0/443 | 0 | — |
| exotic | 50/73 | 27 | 54.0% |
| revert | 0/113 | 0 | — |
| sample | 30/30 | 12 | 40.0% |

The expensive tests failed in the following labelled development patches. Phases come from pytest reports. Missing means the catalog ID never reported.

- `tests/test_executor.py::TestExecutor::test_execute_tpcds` (21.454 s) — call 3, official 2, setup 1.
- `tests/test_generator.py::TestGenerator::test_generator_memory` (6.686 s) — setup 1, subtest 15.
- `tests/test_parser.py::TestParser::test_max_nodes` (28.817 s) — missing 1, setup 1.

Independent uncached label repeats with identical failure sets — exotic 2/2, sample 3/3.
