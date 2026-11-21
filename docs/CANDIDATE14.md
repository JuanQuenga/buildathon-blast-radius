# Candidate 14

Candidate 14 adds field-aware fixture attribution to candidate 13. Candidate 14 static applies the same map to candidate 5's static selector with all runtime probes disabled. Neither release includes the experimental reduced-data executor.

## What the scorer schedules

The four coordinator patches each cost 25.454 simulated seconds on candidate 13. The new plans below include every scheduled test. The two optimizer methods are on TestOptimizer. The other three are on TestExecutor.

| Patch | Edited field | Scheduled methods | Simulated seconds | Saving |
|---|---|---|---:|---:|
| dsex | TPC-DS expected | test_tpcds | 4.232 | 21.222 |
| dssr | TPC-DS source | test_tpcds, test_execute_tpcds | 25.454 | 0.000 |
| hhex | TPC-H expected | test_tpch, test_optimized_tpch | 5.892 | 19.562 |
| hhsr | TPC-H source | test_tpch, test_optimized_tpch, test_execute_tpch | 7.892 | 17.562 |

The full pytest run on these patches yielded respectively one, two, two and two failing methods. On hhsr the conservative source projection also scheduled test_execute_tpch, which passed.

## Pin recording

The recorder ran the 89 catalog methods that opened SQL fixtures or TPC CSVs on the pinned baseline. All 89 methods and 7,537 subtests passed. It wrapped both fixture loaders before test collection. A pair retains its file and index while stored in a class attribute. On consumption the recorder identifies individual tuple positions. It recognizes a discarded underscore only at a verified unpack instruction whose AST target matches the instruction's source positions and whose function never reads that name. Otherwise it retains all unpacked fields. Pickling a whole pair marks every field. Scalar values sent to the worker are marked at unpacking before process submission. A sequence entry is recorded when yielded. Direct file reads outside either loader retain their original readers.

The checked inventory includes 26 optimizer SQL files plus identity.sql, partial.sql and pretty.sql. TPC-DS and TPC-H have 99 and 22 pairs, respectively. Each other optimizer SQL file has one file-level reader. The two TPC files each have 61. The record separates loading a list during class setup from using one of its entries. The executor reads source and metadata for every submitted query, including queries whose worker ultimately skips execution. The optimized TPC-H comparison also consumes expected SQL.

At selection time, the fixture content must match the pin digest. The selector applies the actual diff, runs the loader's pairing or sequence rules, and unions consumers of the changed fields across entries. Directives, comment-line edits, changes on separator lines, and unmappable hunks preserve file-level attribution. Mixed code and fixture edits retain the original fixture fallback. Unknown catalog nodes are still required.

For TPC tables the recorder associates each table dictionary and DuckDB connection created by setup with its loaded files, then records which tests access those class attributes. An available Git binary literal or delta can be checked against the pinned gzip. The CSV check preserves the header and first data row used for type inference, and requires every subsequent field to have a value already present in that column. Other encodings, absent binary contents, and unknown or malformed changes keep the broad readers, because failure in the shared setup can fail both suites. Docker controls replacing a data row in each suite selected only the corresponding table consumers. Invalid binary payload contents retained all 60 readers in both controls.

## Reproduction

The selector image contains the committed pin map. Its build performs no new test recording. The pin map contains field masks per file, entry and consuming test. Patch names and oracle outcomes stay outside the image.

To reproduce the SQL regressions from the repository root, run the command below with a clean SQLGlot pin checkout and the starter catalog. Set TMPDIR to a directory with space for the temporary files. The tool runs the five relevant real pytest methods on every patch and fails on a missing receipt or a missed test.

```sh
PYTHONPATH=. python tools/check_fixture_regressions.py IMAGE /path/to/sqlglot /path/to/tests.json
```

To regenerate the map, enumerate the catalog methods in the pinned index's opens sets for fixture SQL and CSV gzip files. Write their node IDs one per line. Mount the frozen checkout at /app/sqlglot, the recorder directory tools/fixture_trace at /trace and an empty writable output directory at /out. Run the pin environment with PYTHONPATH=/trace:/app/sqlglot and PYTHON_CPU_COUNT=1.

```sh
python -m pytest -p fixture_fields -p no:cacheprovider -q @/nodes
PYTHONPATH=. python tools/build_fixture_fields.py RECORDING_DIRECTORY PIN_CHECKOUT NODE_LIST selector/maps/fixture-fields.json
```

## Practice scenarios

Candidate 13 officially scheduled 150.503 simulated seconds with zero misses and used 24.07–24.47 CPU seconds over three runs. The organizers did not disclose which files were changed. Holding all other schedules fixed, one dsex-like patch gives 129.281 simulated seconds, one hhex-like patch gives 130.941, and one hhsr-like patch gives 132.941. A dssr-like patch alone leaves 150.503. Allowing 23–26 CPU seconds gives **152.3–158.9 total** for one of the three productive edits and **173.5–176.5 total** for none. Two productive edits, other schedule changes, or a different probe mix fall outside these scenarios. One missed patch costs another 300 seconds.

Candidate 5's official static anchor was 177.103 simulated plus 0.67 CPU seconds. At 0.5–1.0 CPU seconds, candidate 14 static projects **156.4–160.5 total** with one productive edit and **177.6–178.1 total** without one. The leaders' scores do not identify the hidden fixture or the hidden failure-only floor.

## Release checks

The 36-patch regression set comprised the four coordinator mutations, 28 additional source or expected replacements across both files, and four mutations that changed statement boundaries. Independent Docker runs used the pinned executor and optimizer pytest methods. Both selectors covered all 67 failing-method observations. All four boundary mutations retained the 61 file readers. Candidate 13 scheduled 916.344 simulated seconds across all 36 patches. Both new selectors scheduled 449.576. Two runs of the primary image produced byte-identical JSONL with SHA-256 a28ba4928babb178e63c1f2233f1b15a5cc4872b9e969698f848e8e2da4070ee and used 0.270 and 0.274 total process-tree CPU seconds. The batch launched zero heavy probes.

The primary runtime commit passed the clean no-cache build in 36 seconds with a 9,462,087-byte archive checkout. The static variant took 34 seconds and 4,535,424 bytes. The build limits were four CPUs and 8 GiB. The sample passed the starter validator for both images. Sixteen selector tests, the forked timeout receipt control and the collection, import and execution controls passed. The first host pytest attempt segfaulted under its address-space limit. The reported fixture labels were rerun in memory-limited containers. A collection-control invocation lacked the catalog mount and was rerun with the starter catalog.

The 104 generated Python mutations retained all 125 known failing heavy targets and all six uncertain or unlabelled targets. The five-test fixture oracle measures the workload readers implicated in these mutations. It does not constitute a whole-suite oracle for arbitrary fixture edits.

| Matched partition | C13 simulated + CPU seconds | C14 simulated + CPU seconds | C14 misses |
|---|---:|---:|---:|
| Sample, 30 | 95.064 + 0.943 | 95.064 + 0.967 | 0 |
| Tuning, 161 including sample | 1302.610 + 81.409 | 1302.610 + 128.205 | 0 |
| Holdout, 197 | 1485.564 + 94.156 | 1483.558 + 84.580 | 0 |

The expanded replay covered 659 development and 218 holdout patches. It missed zero of 310 and 217 labeled patches, respectively. Field projection changed only three static reader sets on those partitions and none on the generated or stress partitions. The observed tuning CPU increase is real for this run. The conditional practice forecast assumes official CPU remains close to candidate 13's measured value.

**Historical heavy-drop monotonicity failed in the concurrent run.** Against both C8 and C13, C14 retained four previously excluded heavy tests on development, one TPC-DS and three generator tests. Both baselines passed on the holdout and generated sets. On two 60-patch stress batches it retained all published C8 drops, but lost 29 C13 drops on the dual-probe batch and eight on the stalled batch. Both batches hit the inherited probe CPU ceiling. Selector wall time was 162.91 and 160.99 seconds, respectively. Neither stress input edits fixtures. These timing-sensitive results limit the final-entry recommendation.

### Official practice and sequential reruns

The official batch scored 174.6 with zero missed commits. Simulated time was 150.503 seconds and CPU was 24.1–24.6 seconds. Candidate 13 produced the same simulated time. The tagged checkout measured 9,474,900 bytes and the clean build took 55 seconds.

A sequential replay of the affected 33 development patches recovered all four historical drops, with identical plans from C13 and C14. A repeat of the 60-patch dual-probe stress recovered all 29. Two repeats of the stalled batch lost five and four historically observed C13 drops. The quieter stalled run used 161.02 CPU seconds and 161.84 selector wall seconds. The historical stress monotonicity gate remains failed. Four independent table controls covered valid data changes in each family's tables and invalid headers breaking executor setup. All observed failures remained selected.
