# Candidate 8

**Expected official practice score is 146.7 seconds.** The selector is at 610cc8f. Candidate 5 scored 177.8 officially, with 177.103 simulated seconds, 0.67 CPU seconds and zero misses on every run. Applying the candidate-6 holdout gain to that result gives 164.2 seconds for candidate 6. Candidate 8 saved another 86.373 seconds over 197 holdouts, or 17.54 seconds per 40 patches. The hidden practice mix remains unknown. The public sample had identical simulated time across candidates 6, 7 and 8. An official candidate-6 score above 167.5 would take this projection above 150.

## Matched results

| Partition | Patches | Misses C6 / C7 / C8 | Simulated seconds C6 / C7 / C8 | Docker CPU seconds C6 / C7 / C8 |
|---|---:|---:|---:|---:|
| Official sample | 30 | 0 / 0 / 0 | 115.613 / 115.613 / 115.613 | 2.081 / 1.851 / 1.475 |
| Tuning including sample | 161 | 0 / 0 / 0 | 1513.615 / 1497.046 / 1450.403 | 83.875 / 66.153 / 71.002 |
| Registered holdout | 197 | 0 / 0 / 0 | 1831.459 / 1784.854 / 1745.024 | 78.016 / 77.991 / 78.078 |

The selector ran inside the same two-core starter restrictions in batches of at most 40. The measurement charged the selector process tree. Answers stayed in the host scorer. Label revision 8cba26f supplies the matched partitions. A separate check of the 48 additional audited reverts at revision 6b2acfc found zero misses. The extra 15 holdout cases took 241.975 simulated and 11.299 CPU seconds. The extra 33 tuning cases took 482.885 simulated and 24.628 CPU seconds. The forecast uses only the matched partition.

## What changed the plans

| Holdout staged probe | C6 attempts | C6 CPU seconds per attempt | C8 attempts | C8 CPU seconds per attempt |
|---|---:|---:|---:|---:|
| TPC-DS | 12 | 4.976 | 20 | 2.267 |
| Parser budget | 8 | 0.800 | 10 | 0.632 |
| Generator memory | 0 | — | 5 | 0.673 |

The generator average includes three early failures. Its two passing full tests took 1.292 and 1.387 CPU seconds. TPC-DS token-corpus replays averaged 0.301 seconds, parse-corpus replays 0.518, query slices 2.604 and partial replays 8.234. Individual costs include overlay replay. The batch CPU also charges static selection, computing slice inputs, preloading third-party libraries, collection and scheduling.

TPC-DS selections fell from 45 to 38 on holdout. Six passing selections crossed candidate 6's minimum prefix-cost threshold. One had a different plan fingerprint and produced the correct result when executed. Parser selections fell from 13 to 11. Both additional drops came from full-suite fallbacks after successful pytest collection. The selector also removed two passing generator-memory selections. It retained all ten TPC-DS failures and the one parser failure. Tuning recovered two more TPC-DS and two more parser passes than candidate 6. The [candidate-6 keep breakdown](CANDIDATE7.md) accounts for all 45 and 13 original selections by cause and actual outcome.

The collector accepts existing package source edits that pass the eligibility checks and have a static collection-risk alternative. Actual pytest collection must finish and include every catalog node. The selector then starts with every catalog test required. Only individually verified heavy tests may leave that plan. A failed collection, unsupported source operation or registry hazard retains the full fallback.

The TPC-DS worker parses and round-trips every affected input, including skipped queries. Executed queries must retain their pinned DuckDB reference SQL. When the pin's execution path is affected, the worker compares actual result columns and ordered rows. Where execution may be skipped, it checks the parsed input, planner expression and step graph. A divergent plan can instead run through the actual Python executor and match its reference result. Extra execution has a cumulative CPU timer of 0.6 or 3 seconds depending on the replay mode. Coverage now includes lines observed only in the original spawned worker and DataFrame comparison. Query slices use cached pin tables only after the changed regions clear both setup traces.

The generator probe runs the complete pinned allocation assertion, including 100 warm-ups and 1000 measured constructions per dialect. It compares dialect lookup before and after clearing the registry. The test uses a fresh process. Other staged probes can fork after importing NumPy, pandas, DuckDB and pytest. No SQLGlot or test modules may be loaded in the prewarmed parent. Each child imports from its own patched overlay and must exit successfully with an explicit receipt.

After each accepted drop, the scheduler prices one through eight workers at two seconds per occupied worker, repacks the remaining requirements and reprices remaining probes. In the stress batch the original 36.817-second plan used four workers. After both drops, the cheapest 20.248-second plan used five.

## Validation and risk

| Generated target | Failed and kept | Failed and dropped | Passed and kept | Passed and dropped | Unknown and kept |
|---|---:|---:|---:|---:|---:|
| TPC-DS | 76 | 0 | 10 | 14 | 1 |
| Parser budget | 22 | 0 | 19 | 60 | 0 |
| Generator memory | 27 | 0 | 12 | 60 | 5 |

The 101 original mutations supplied 201 known TPC-DS and parser outcomes. An independent targeted pytest oracle supplied 99 known generator outcomes on those patches and three extra mutations, including retained generator allocations and constructor failure. Five generator targets failed to collect and were kept. The one unknown TPC-DS label is inherited from the earlier capped oracle. The forced checks ran in Docker with the two-core starter flags. All 16 inherited selector tests passed. A clean git-archive build produced the same runtime configuration and root filesystem layers as the measured image. The starter scorer accepted its default sample output.

Both deadline runs used 60 distinct overlays, the official catalog, the default entrypoint, a read-only root, 64 MiB tmpfs, no network, UID/GID 65534, two CPUs and 4 GiB with swap disabled. Both produced 60 valid plans. The passing both-heavy workload took **83.233 seconds** including container startup, completing 34 parser and 33 TPC-DS checks. The stalled workload took **83.077 seconds**. Every patch qualified for both probes and its TPC-DS parser loop could stall. Four TPC-DS children timed out and all 60 TPC-DS selections remained scheduled. Eight parser checks passed. An independently killed batch supervisor left the original complete static plan, and deliberate import failure, premature zero exit and timeout each failed closed. These timings leave 186.7 seconds of headroom on the slower run. External startup delays and arbitrary inputs prevent a universal wall-time guarantee.

There were zero observed additional missed patches in the 197 matched holdouts and the 15 newly audited holdouts. Under an independent representative sampling model, zero in 197 gives a one-sided 95% upper bound of 1.51% per patch. Repeated inspection of the benchmark and correlated mutation families limit its interpretation. The measured holdout gain breaks even at about 0.146% additional misses per patch at the 300-second penalty. The data cannot establish that rate for unseen kinds. Coverage in a fresh process can miss interactions with earlier pytest tests and global state. The registry guards, actual collection, restricted source edits, exact result checks and conservative fallbacks reduce that exposure.

## Rejected experiments and failed checks

A prewarmed interpreter initially exceeded its 1800 MiB address-space ceiling under the official two-core flags and kept passing tests. Setting MALLOC_ARENA_MAX=2 before interpreter startup restored the verified drops under those flags. Running the allocation test in the prewarmed process also raised its CPU cost because it collected a larger heap. A generator-only prototype improved holdout simulated time by 6.689 seconds over candidate 7 and consumed another 5.633 CPU seconds.

The measured maximum tuning saving per opportunity was 0.773 seconds for TPC-H executor and 1.768 for optimized TPC-H. The earlier 22-query replay cost 2.179 CPU seconds. Optimizer TPC-DS and TPC-H offered at most 2.232 and 0.355 seconds. A direct optimizer TPC-DS run was killed by its separate experimental memory cgroup, and a TPC-H run exceeded its break-even CPU timer. These targets retain their static selections. The candidate-6 official result was pending when this forecast was written.
