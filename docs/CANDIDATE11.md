# Candidate 11

Practice forecast conditional on zero misses is **146.1 simulated + 4.6 CPU = 150.7 seconds**. This uses the two official anchors and effective sample/holdout weights in [candidate 10](CANDIDATE10.md). Candidate 10 was rejected during its image build and has no official runtime measurement. The new forecast is only 1.3 seconds better than its forecast. The measurements do not support a 136-second prediction.

| Partition | Patches | Misses C10 / C11 | Simulated seconds C10 / C11 | CPU seconds C10 / C11 |
|---|---:|---:|---:|---:|
| Sample | 30 | 0 / 0 | 97.760 / 100.296 | 2.703 / 2.357 |
| Tuning including sample | 161 | 0 / 0 | 1562.431 / 1607.228 | 32.649 / 17.008 |
| Registered holdout | 197 | 0 / 0 | 1785.542 / 1763.097 | 25.264 / 22.459 |

The host evaluated label revision `8cba26f`. The selector received only the catalog and diffs. C11 had one frozen aggregate holdout pass. Both score components include all patches in the partition. CPU includes waited-for children. Timing gates can change individual plans across runs. A diagnostic repeat of C10 reproduced 12 full holdouts and used 1787.566 simulated seconds, 2.024 more than its release measurement.

## Why the full plans happened

There are 1,620 numbered tests. “Full” below means every number was scheduled, including tests added to otherwise unused worker capacity. In the holdout, four C10 full plans needed zero tests, six needed one to three, and two needed 1,133 and 1,600. On tuning, four needed zero, five needed one to seven, one needed 226 and one needed all 1,620. The cost of sending a full plan was 36.817 seconds. The oracle excess on the twelve holdouts was 363.879 seconds.

| C10 holdout | Trigger | C11 still full |
| --- | --- | --- |
| vg6o | Module moved behind a star re-export. Old-name import risk and observed users | Yes |
| 5m6m | Removed property class, references during import and generator dispatch edits | No |
| cqy2 | Dialect function signatures, expression class users and worker padding | Yes |
| dprc | MySQL parser class attribute and method removal, then padding | Yes |
| fx9t | Removed generator function referenced in import-time code | Yes |
| jdug | Removed DuckDB typing helper and its import-time references | Yes |
| 57hi | Tokenizer keyword map plus a fixture reader | Yes |
| apy3 | Base dialect date-unit dictionary and its derived class attributes | No |
| oczu | Same dictionary mechanism plus two optimizer fixture edits | No |
| ow2y | Comment inside a class-body loop. AST differ read the nonexistent `For.name` | No |
| vmiw | Widely executed parser statement, then padding | Yes |
| 2b19b223 | Syntax error in a module loaded during collection | Yes |

Candidate 8 had collected 5m6m and jdug and verified a passing parser-budget test on both, saving 7.363 seconds per patch. C10 admitted no holdout collection probes and lost both drops. That accounts for the increase from ten to twelve. C11 converted four C10 full plans. Two other plans (96dx and w7a2) became full under its different probe allocation, leaving ten.

| C10 tuning | Trigger | Failed tests | C11 still full |
|---|---|---:|---|
| 18df | Newly collected unittest module | 0 | Yes |
| 4c2w | Pytest plugin configuration change | 0 | Yes |
| fiwi | Raising autouse fixture applies to the entire suite | 1,620 | Yes |
| mwlk | Pytest class discovery configuration | 0 | No |
| 6i6f | Changed closure bindings in JSONPath parser | 1 | No |
| 8jzd | Parser lines used by many tests, then padding | 3 | No |
| eksn | Parser and tokenizer edits used by many tests | 2 | Yes |
| f4s2 | Expression-class member removal and wide static coverage | 0 | Yes |
| gocp | Changed class bindings and join optimizer import risk | 7 | Yes |
| jl5q | Removed optimizer helper and its references | 1 | No |
| lnoy | Shared dialect construction and escape mapping | 226 | Yes |

C11 converted four tuning plans and added ijzp, leaving eight. Neither a read timeout nor an unknown test number caused any of the C10 full plans listed here.

## What the selector can distinguish

The collection probe starts on a patched overlay, imports SQLGlot and invokes pytest with the pin's test directory and continuation on collection errors. It reconciles the collected IDs against every numbered catalog node. Missing IDs and tests in failed test files are scheduled alongside the static requirements. A failed collector receipt, unexplained collection error or timeout preserves the full fallback. Successful collection permits the static plan where the impact model offers a conditional alternative. Configuration with arbitrary runtime effects still uses its broader plan. An inert new unittest module and discovery-only configuration can qualify. The checked tuning collection cost about two CPU seconds including setup. The probe budget does not admit every eligible patch.

Pin evidence for README.md, the eight .github files, four benchmark files, 17 posts, two pdoc files and setup.py shows zero traced opens or covered files. Existing data edits use actual readers, and additions, removals and moves also use traced directory enumerators. The two packaging configurations are inspected for pytest changes even though the regular suite did not open them. New package modules have no importer at the pin, but all other edits in the patch are analyzed. New package initializers and top-level modules can shadow existing imports. Deleted modules retain their observed importers. This release leaves the broad fallback for unknown paths and unsupported conftest/plugin behavior.

The class differ now compares the old and new compound statement inside a class or module. A comment-only change inside a loop produces an equal AST. Ordinary closure changes also retain users of the enclosing function and analyze the rest of the edit. Protocol changes such as turning a function into a generator retain the broader guard. Removed definitions exclude their own deleted declaration from name-reference searches.

## Heavy tests and CPU

The pin-derived date-unit profile records actual dictionary keys and derived interval-set membership during collection and 20 individual expensive tests, including spawned interpreters. The edit guard checks an otherwise identical module AST and a literal string dictionary. It includes old and new units in the affected set, preserves the DuckDB import-time removal, and accounts separately for fixture readers. The pin contains one runtime dictionary lookup and four interval-set membership sites.

A rule mismatch can fall through to query-slice verification if at most five worker queries reach the change and the CPU allowance remains. The existing executor compares the plan fingerprint and executes divergent plans, then compares the ordered rows and columns. This continuation produced **no documented holdout drop**. The two holdout collection attempts timed out. The saving came primarily from narrower static plans.

Optimizer admission moved from .78 to .53 against 6/7 tuning and 1/5 holdout passes. For small workers, an offline enumeration of eligible tuning plans found 5/6 passing in the sample fold and 1/14 in the revert fold. A pooled .30 prior shrinks toward observed probe outcomes within the current batch. That difference between folds limits prediction on new kinds. The prior holdout data influenced calibration, so this holdout is a matched comparison, not a blind estimate of transfer. Generator admission fell to .28. Exact thresholds and time budgets remain heuristic.

Holdout probe CPU was **14.208 seconds, or 2.885 per 40 patches**. Tuning used 9.856 seconds, or 2.449 per 40. Holdout passing parser selections fell from 12 to 9 and passing generator selections from 20 to 17. Passing TPC-DS selections rose from 32 to 33. Critical-worker costs changed by −86.410 seconds for passing parser and +63.964 seconds for passing TPC-DS. Demand for expensive TPC-DS verification exceeded the budget.

| 40-patch scenario | C10 simulated + CPU | C11 simulated + CPU | C11 probe CPU | C11 misses |
|---|---:|---:|---:|---:|
| Sample plus ten development patches | 143.255 + 2.954 | 141.177 + 3.286 | 2.617 | 0 |
| One previously known heavy opportunity | 141.889 + 4.086 | 150.664 + 3.577 | 2.862 | 0 |

C11 also had zero misses on the 103 additional development patches. It used 1657.829 simulated and 13.230 CPU seconds there, compared with C10's 1702.508 and 24.088. The targeted 40-patch batch regressed by 8.266 total seconds.

## Failure controls and generated mutations

| Target | Failing kept / dropped | Passing kept / dropped | Unknown kept |
|---|---:|---:|---:|
| TPC-DS | 76 / 0 | 11 / 13 | 1 |
| Parser budget | 22 / 0 | 4 / 75 | 0 |
| Generator memory | 27 / 0 | 10 / 62 | 5 |

The entrypoint evaluated 104 generated patches with all labels held on the host. The forced verifier separately kept all 125 failures and all six unknowns. It dropped 12 passing TPC-DS, 60 passing parser and 60 passing generator selections. The 16 inherited tests and the timeout, premature-exit, collection-import and spawned-worker failure controls passed. A clean collection returned zero missing IDs. An import failure deliberately introduced in the parser test module returned exactly its 81 catalog tests. Deleting the same module returned those same 81 IDs. Separate patched pytest runs reproduced the optimizer and keyword controls. The two 60-patch deadlines produced valid output in 22.695 and 21.420 seconds including container startup.

## Build inputs

Candidate 10 was rejected by the official builder at the optimizer rule-recording step. Candidate 11 uses the validated recordings from the pinned SQLGlot checkout in the candidate-10 image. The archive contains the parser and executor traces, TPC-DS stage fingerprints and input tables, rule inputs and stage costs, the token and parse corpora, and the parser and generator profiles. The existing Python sources contain the original recorders. None of the partition files or answers were inputs to those recorders.

The untouched candidate-10 build inputs and Python environment supplied the recordings. The release archive is 10,316,204 bytes (9.9 MiB). Its SHA-256 is `596bd1bfab9c82f5fce0fcfccabe9206d59be868b62abd43eb9770d4501a27b5`. The build fetches the pinned source and locked Python packages, extracts these inputs, builds the local runtime indexes and precompiles Python. There is no Dockerfile address-space limit. Before tagging, run `bash tools/check_build_budget.sh <release-commit> <local-image-name>`. It starts a fresh BuildKit container with four CPUs and 8 GiB, uses a clean Git archive, disables the build cache, and requires the complete image build and load to finish within six minutes.

## Cold-build diagnosis

A fresh Linux/amd64 BuildKit builder with four CPUs and 8 GiB ran candidate 10 from a Git archive without cache. The diagnostic wrapped each unchanged RUN command to sample the sum of resident-process memory every 100 ms. Shared pages may be counted more than once, and short peaks can fall between samples. The full build took **325.029 seconds before export**. It exceeded our four-minute local target. It completed under the official ten-minute and 8 GiB limits here. The official log ended at the rule-recording command, before any exit code or underlying exception. It cannot distinguish a timeout from an allocation failure.

| Candidate-10 RUN | Wall s | Peak sampled RSS MiB |
|---|---:|---:|
| Install locked Python packages | 23.84 | 137.9 |
| Unpack pin | 0.20 | 21.5 |
| TPC-DS record, timing run and parser recording | 144.31 | 1,666.6 |
| Staged fingerprints | 36.28 | 366.3 |
| Per-query coverage | 23.52 | 310.2 |
| Parser corpus | 20.20 | 299.2 |
| Optimizer rule corpus | 51.29 | 370.7 |
| Generator and parser profiles | 5.84 | 86.3 |
| Runtime indexes | 7.84 | 171.1 |
| Bytecode and permissions | 1.21 | 59.7 |

An experimental rule-to-query recorder failed inside DuckDB with an allocation error under the old virtual-memory cap. Two retries with different memory settings also failed. The frozen C10 diagnostic completed the original recorder. The experimental continuation was discarded. Candidate 11 removes both the long build path and the address-space caps.

The cold build of selector revision `7410469` took **56 seconds including image export and load** under the six-minute timeout. Every file under /app other than Python bytecode was compared with the measured image. Using decompressed contents for gzip files, the sole difference across 1,467 files was the build-only index preparer, updated to write deterministic gzip headers. The cold image's default entrypoint scored **100.296 simulated seconds and zero misses** with the starter validator.

A separate exact invocation of the original rule-recording command in the C10 image with four CPUs and 8 GiB also succeeded, taking 48.54 seconds and 326.3 MiB peak sampled RSS. In the new image, a diagnostic cold build measured installation at 21.42 s / 139.8 MiB, unpacking the SQLGlot pin at 0.20 s / 21.6 MiB, extracting the recorded inputs at 0.60 s / 41.8 MiB, runtime indexes at 7.75 s / 170.2 MiB, and bytecode/permissions at 1.21 s / 57.7 MiB. The network-enabled install is the longest remaining RUN.

An independent control on the cold image changed an observed date-unit alias and ran its actual Snowflake test. Pytest failed and the selector retained the test. A second edit added an unused alias. Pytest passed and the read profile excluded the test from the required set.

Two additional cold-image controls forced a divergent optimized AST through a real TPC-DS query. Changing only source-line metadata reached Python execution and passed the ordered result comparison. Replacing the query with a different SELECT reached the same execution stage and failed the comparison. The release-shape generated matrix and all inherited/failure-path checks also repeated successfully on the cold image.

The unchanged candidate-8 tag also passed the fresh four-CPU, 8 GiB, six-minute build-and-load gate. It took **269 seconds**. This fits the release timeout and exceeds the 240-second local target. Its prior official result remains the only official evidence for that exact image.
