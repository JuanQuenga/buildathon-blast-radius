# Candidate 12

The original 40-patch forecast was **117.8 simulated + 15.6 CPU = 133.4 seconds**, conditional on zero misses. We withdrew that forecast after C11 lost the previously observed hidden TPC-DS saving. The replacement scenarios and measured C12 CPU breakdown appear in [the C13 audit](CANDIDATE13.md). The host-derived estimates below use the same 8cba26f labels and batches as candidate 11. Only patches and the numbered catalog enter the container. Tuning includes the sample.

| Partition | Patches | Misses C11 / C12 | Simulated seconds C11 / C12 | CPU seconds C11 / C12 | Full plans C11 / C12 |
|---|---:|---:|---:|---:|---:|
| Sample | 30 | 0 / 0 | 100.296 / 95.064 | 2.357 / 1.303 | 0 / 0 |
| Tuning | 161 | 0 / 0 | 1607.228 / 1346.408 | 17.008 / 57.618 | 8 / 5 |
| Registered holdout | 197 | 0 / 0 | 1763.097 / 1495.894 | 22.459 / 103.816 | 10 / 4 |

The matched holdout saving was 267.203 simulated seconds at an additional 81.357 CPU seconds. An experimental predecessor spent 118.995 CPU seconds and kept 22 passing TPC-DS tests. The final mechanism kept 19. A second run of the corrected candidate under heavier concurrent load used 1.519, 65.010 and 103.723 CPU seconds on the three partitions, with identical JSONL. A diagnostic rerun of unmodified C11 had 29 passing keeps, eight full plans and 1679.679 simulated seconds. C11's elapsed-time gates explain why that comparison is smaller than the comparison against its release record.

## Sample accounting

Durations are positive-cost passing catalog IDs on the critical worker. The catalog assigns zero milliseconds to the remaining padding. Each listed worker contains a genuine failure unless the entire worker is identified as removable. The other 23 patches exactly attain their failure-only floor.

| Patch | Passing catalog numbers | Extra load s | Extra fee s | C12 excess s |
|---|---|---:|---:|---:|
| 2qn5 | 85, 99, 125, 259, 345, 516 | .536 | 2 | 0 |
| 5y72 | 1087, 1327, 1355 | .466 | 2 | 0 |
| 535z | 85, 392, 400, 532, 541, 929 | .259 | 0 | .259 |
| 6ze8 | 28, 151, 167, 218, 231, 253, 259, 288–296, 298–301, 303–310, 635, 641, 670, 760, 1221, 1222, 1486 | .432 | 0 | .432 |
| 986c | 1619 | .011 | 0 | .011 |
| bj7g | 93 | .230 | 0 | 0 |
| rszg | 1619 | .011 | 0 | .011 |
| **Total** | | **1.945** | **4** | **.713** |

C12 recovered 5.232 seconds of the 5.945-second gap above 94.351. Two typing-source checks cost .022 seconds combined. Test 302 genuinely fails on 6ze8 and test 1434 on 535z. The remaining .691 seconds on those two occupied workers has no further verified exclusion.

The pin constructs dialect versions as three integers. A two-component tuple cannot equal that version, so switching the strict comparison to its inclusive form at the same boundary preserves the branch. For the fresh table builder, three clean-pin tests and their spawned workers observed ordinary string arguments and the original copy implementations. Copying that newly constructed tree before aliasing it preserves its value and ownership. Both guards check the entire edited function AST and require a single-file change. A changed boundary, sibling edit or escaped input falls back to ordinary impact analysis. On bj7g the real ClickHouse test 93 passed before test 94 failed. C12 removes the confirmed pass and keeps the failure and every unchecked candidate.

The pin's validation trace also records missing expression arguments and dispatched SHOW keys. The only reader of required flag values builds the class's required set. Its runtime validation site is instrumented, including missing-field ordering. Explicitly derived dictionaries retain the wider guard. The SHOW proof requires unchanged keys and a single Boolean argument to the pinned closure factory. The keyword and class proofs still account separately for readers of Python source. These recordings let two formerly replayed small workers drop without starting pytest or unittest.

| Sample CPU component in an instrumented repeat | C11 s | C12 s |
|---|---:|---:|
| Startup, catalog, supervision, serialization and process-exit residual | .209 | .198 |
| Pin impact maps | .053 | .045 |
| Diff application, compilation, AST analysis and catalog resolution, excluding map loads | .220 | .238 |
| Static scheduling | .017 | .022 |
| Probe queue construction | .112 | .034 |
| Timed individual probes | 1.619 | .814 |
| Remaining verifier overhead, including warmup | .001 | .102 |
| **Process tree total** | **2.230** | **1.453** |

The four C12 attempts used .243 s for an optimizer mismatch, .017 s for a parser/serialization exception, .215 s for a passing bundle and .338 s for the partial bundle. The profiler measures CPU in the selection and map-loading calls. Startup and finalization are measured together by subtraction. Pin line-to-query and line-to-rule bitsets now load directly. Compatible probes fork from a warm package root and optimizer checks load only the tables the query requests.

## Why candidate 11 kept 33 passing TPC-DS selections

The C11 release had 43 static TPC-DS selections, of which ten failed. The release recorded aggregate probe outcomes and discarded individual holdout receipts. Reapplying the frozen classifier recovers all 33 passing candidates and their eligibility. The groups below partition those 33. Ambiguous historical timeouts are reported at the resolution supported by the release log.

| C11 first decision | Count | Patches | C12 removed |
|---|---:|---|---|
| Execution-stage guard requested all 99 prefixes including the 57 executions. The price gate rejected the probe | 13 | 5m6m, 5st1, c4t0, cu8s, d4ax, dprc, hgiw, n0q7, sbbf, vn4u, vzb4, w7a2, yum6 | sbbf, vn4u |
| Slice required all 57 execution-stage queries and missed the price gate | 3 | 03i4, f129, rm2j | None |
| Fixture or dialect registration/base-class hazard | 3 | 57hi, 7y2o, cqy2 | 57hi |
| Collection required before TPC-DS was eligible. vg6o also moved a module | 3 | fx9t, jdug, vg6o | fx9t |
| Optimizer rule checks. Three passing tests had changed intermediate rule fingerprints. Two candidates received no successful verification | 5 | 4lzh, dtvw, fkls, 7haz, wmdi | All five |
| Cheap query slices competing for budget. Two slice attempts timed out, one in parse and one in optimize. The release has no per-ID timeout receipt | 4 | ancs, pihd, wnp3, zeno | All four |
| Corpus or tokens. The parser corpus received no successful verification and the token fingerprint changed | 2 | 96dx, rxmc | 96dx |

The four historical rule failures include the genuinely failing dfx6 outside this table. There were two collection timeouts across the release. The archive cannot identify their patch IDs with certainty. The alternate C11 replay produced different collection outcomes, so its receipts do not fill that gap.

**Cost of verification.** The measured passing fraction before replay is 33/43, or 76.7%. At that rate a 23.454-second removable worker warrants up to 18.00 CPU seconds. A complete-plan gain of 9 seconds warrants 6.91 CPU seconds. A 2-second gain warrants 1.53 CPU seconds. The pin's 57 Python executions cost 10.734 CPU seconds, with 1.984 for optimizer work and .288 for planning. An independent full one-worker pytest run including reference checks cost 25.030 CPU seconds. For example, on 5m6m the frozen static alternative saves only 8.938 seconds, so the 76.7% prior pays at most 6.86 CPU seconds. On 03i4 it saves 12.515 seconds, allowing 9.60 CPU seconds. Both thresholds are below the 10.734 seconds for all 57 executions even before optimization and planning. The difference matters when the parser-budget test or other occupied workers determine the makespan. Scheduling uses the actual change in makespan plus worker fees. The price for a query slice is .28 + .009 per active query plus 1.18 times the pin's optimizer, plan and required execution costs. The selector compares that price to .76 times the gain. Full fallback after a corpus mismatch uses a discounted .65 probability. These are estimates of remaining work. A passing selection can still cost more to verify than it saves.

**Divergent results.** The recorder matched the inputs, outputs, in-place changes, argument fingerprints and rule identities of all 798 original recordings to their query IDs. Changed rules run on the pin inputs. When a rule diverges, the verifier follows the affected query through parsing, optimization and planning, executes when the plan diverges or downstream code was touched, and compares column names and ordered rows. Once the whole query passes, later recordings for it need no separate check. A changed DuckDB reference requires the original comparison against the patched reference. Method-local checks can share an imported overlay, and independent checks use isolated children. Frozen missing inputs, exceptions, incomplete receipts and inconclusive checks retain the test.

The rule continuation removed 4lzh, dtvw and fkls after their intermediate fingerprints changed. In an ordinary two-CPU Docker run these cost 18.237, 6.599 and 23.975 CPU seconds for 12.923, 15.616 and 16.655 simulated seconds saved respectively. Two individual verifications lost on total time. The planner currently prices eligibility before it knows every divergent query. Execution economics and CPU transfer remain the main forecast uncertainty.

## Broad plans

On the holdout, C12 converted vg6o, fx9t and jdug through actual collection and the narrower static alternative. It converted 96dx and w7a2 through individual heavy-test verification. On 57hi, the unrelated Oracle generator edit was AST-identical. The tokenizer proof could therefore exclude expensive nonreaders while the fixture's observed readers stayed required. The four remaining full plans were cqy2 (dialect decorators and cross-class changes), dprc (MySQL dispatch and method removal), vmiw (widely reached parser statement, 1,133 actual failures) and 2b19b223 (collection-breaking syntax, 1,600 actual failures).

On tuning, collection narrowed 18df (new test module) and 4c2w (disabling an absent plugin), and parser verification narrowed ijzp. For 4c2w the selector checked pytest builtins, installed entry points, importable modules and pinned references, then reconciled the patched collection with the catalog. Remaining full plans were fiwi (raising autouse fixture, all 1,620 tests fail), eksn (broad parser/tokenizer coverage), f4s2 (expression member removal), gocp (join bindings and import risk) and lnoy (shared dialect construction and escaping). Some broad static plans fill the remaining worker space with the rest of the catalog at zero marginal cost.

## Build and forecast

The corrected selector at 5740518 passed a clean four-CPU, 8 GiB build and Docker load in 59 seconds, with a 9,333,140-byte tracked checkout. The release pins the same 43 wheel artifacts by URL and SHA-256 and checks their versions against the starter lock. Both the previous and new images reported identical names and versions for all 44 installed Python distributions, including pip. Ordinary admission uses a fixed estimate allowance and at most 12 scheduled attempts per first-stage mode. This cut a repetitive 60-patch test from 168.99 to 69.91 real seconds.

Repacking the pin archive with raw inner members lets xz compress shared optimizer inputs across recordings. It shrank from 10,316,204 to 4,304,780 bytes. The build restores the inner files and creates runtime indexes in the same layer. The evaluation answers, candidate reports and benchmark code stay outside the runtime image.

The pre-official practice forecast applies the candidate-10 model's 21.93 effective sample patches and 18.07 effective registered holdouts to changes against C11. The CPU multiplier of 1.65 reproduces the rounded C5/C11 CPU anchors with their measured partitions. The two slowest divergent passes alone can move the cost appreciably. The final forecast and miss sensitivity are in [the final decision](FINAL.md).


## Release checks

The starter scorer accepted the corrected default entrypoint at 95.064 seconds and zero misses. The finished image contained no benchmark paths or answer files. Five cold-image sample invocations before the package-initializer fix and five more after it produced byte-identical plans (SHA-256 `b07684ac6a6724c33773a5121e726f0d66889028502d3d43f8a75c54b4992674`). Process CPU ranged from 0.985 to 1.034 seconds in the first five. The five corrected invocations under concurrent load ranged from 1.360 to 1.530 seconds. One of those five exceeded the 1.5-second CPU target by .030 seconds.

All 16 inherited checks passed on the frozen image. Additional controls covered parent/worker failure, timeout, premature exit and collection errors. A clean collection returned zero missing catalog IDs; removing the parser test module or breaking its import returned exactly 81. Two live divergent-query controls reached execution and distinguished equal and different ordered results. Two additional DuckDB controls accepted a changed-but-equivalent reference and rejected an actually different reference. Every inverted query, worker and rule index matched the original pin line membership exactly.

A first attempt to trace every validation in one 1.5 GB pytest process was killed. An early split-by-file retry stopped on a module with no collected tests (pytest exit 5). Fresh processes per file/directory then completed all 1,620 passing pin tests. The earlier CPU-only static timer failed the inherited blocked-I/O test, taking 15 seconds across three artificial sleeps. The independent wall watchdog restored prompt fallback and the test passed. These failures occurred before the release tag.

Two additional development patches exposed a release-blocking bug during validation. Dropping a public re-export made one test module uncollectable in each case. Collection returned the missing catalog ID, but a subsequent unittest replay forked a process with the pin package already imported. Python imports a package initializer under its package name. The verifier previously checked the dotted filename ending in `.__init__`, which did not detect the loaded package. The release resolves initializer paths to the actual package identity and preserves missing collection IDs through every subsequent verification. Both reproductions now select the failed test. The exact image had zero misses across 112 additional development and 20 additional holdout labels from 71d5f24. All 38 subsequent development labels through 3a8085f also passed, including the cross-checked stalled iterator. None of these counts are in the 161/197 matched partitions.

Across the generated 104-patch matrix on the corrected image the entrypoint kept every one of the 125 labeled heavy-test failures and all six uncertain targets. In a separate forced-verifier run the checks retained those same failures and uncertain cases and cleared 12 passing TPC-DS, 60 passing parser and 60 passing generator selections.

On the corrected image the two repeated 60-patch deadline fixtures completed in 79.59 and 108.45 wall seconds, respectively. Both emitted the same complete plans as the preceding image.

A fixed 60-patch batch combined all 30 sample patches, 18 mechanical holdouts of sample kinds and 12 of other kinds. Candidate 12 scored 401.446 simulated seconds, zero misses and 26.673–30.036 CPU seconds across five runs. The plans had identical SHA-256 `bc7cc1504ccb3c1e934a009ba79418c7cc0d61f193a62dbd77ff031d3915c74f`. Wall time ranged from 27.41 to 30.91 seconds. Candidate 5 scored 507.053 simulated + 2.320 CPU seconds and zero misses on the same inputs.
