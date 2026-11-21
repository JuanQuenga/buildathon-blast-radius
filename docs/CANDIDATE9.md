# Candidate 9

The practice forecast is **163.9 simulated + 5.9 CPU = 169.8 seconds**, conditional on zero misses.

## Matched Docker results

| Partition | Patches | Misses C8 / C9 | Simulated seconds C8 / C9 | CPU seconds C8 / C9 |
|---|---:|---:|---:|---:|
| Sample | 30 | 0 / 0 | 115.613 / 99.838 | 2.069 / 3.187 |
| Tuning including sample | 161 | 0 / 0 | 1468.768 / 1564.509 | 106.504 / 34.658 |
| Registered holdout | 197 | 0 / 0 | 1745.024 / 1961.045 | 114.560 / 22.915 |


The host scorer used label revision 8cba26f. Each two-CPU Docker container received only a catalog and patch files. CPU includes startup, the selector and its waited-for children. Each candidate had one aggregate holdout pass. The C8 remeasurement above ran under greater machine contention than the published C8 run of 71.002 tuning and 78.078 holdout CPU seconds. Wall-time cutoffs also changed its tuning schedule by 18.365 seconds.

## Forecast

Candidate 5 officially scored 177.103 + 0.67 = 177.773 seconds. Candidate 8 scored 155.649 + 18.99 = 174.639 at the median CPU. The simulated gain was 21.454 seconds and the extra CPU was 18.32. The earlier 146.7 forecast overstated how many holdout savings would transfer.

For simulation, split 40 effective patches between the public sample and the registered holdout. Fit the weights to the C5-to-C8 official change. C5 and C8 had the same sample simulation, while the 197 holdouts improved by 233.885 seconds. This gives 18.07 effective holdout patches and 21.93 sample patches. Add the weighted changes from C5 to its official simulated time. For CPU, apply the same weights to the changes from C5, then scale the weighted C8 increase to the observed 18.32 seconds. This reproduces both official anchors by construction. Two observations cannot validate transfer to a third selector. Losing the one observed TPC-DS saving moves the simulated score by 21.454 seconds before any other changes.

## Where the simulated time went

Milliseconds are attributed to the critical worker. Start with failing tests on their assigned workers, then add passes longest first and charge each increase in maximum load. Worker fees and these load categories sum to simulated time. The oracle and full-plan rows overlap those categories. The failure-only oracle has matching lower and feasible upper bounds on both partitions.

| Seconds unless marked | Tuning C8 | Tuning candidate | Holdout C8 | Holdout candidate |
|---|---:|---:|---:|---:|
| oracle | 565.366 | 565.366 | 665.624 | 665.624 |
| fee | 488.000 | 462.000 | 582.000 | 574.000 |
| failed load | 312.746 | 312.746 | 348.298 | 353.265 |
| Passing test execute tpcds | 253.487 | 358.091 | 396.770 | 453.761 |
| Passing test max nodes | 215.646 | 273.273 | 262.442 | 487.444 |
| Passing test generator memory | 39.423 | 52.876 | 44.930 | 26.412 |
| Passing tpch | 7.890 | 1.183 | 10.354 | 0.085 |
| Passing optimizer | 84.010 | 59.337 | 48.711 | 34.134 |
| Passing other | 67.566 | 45.003 | 51.519 | 31.944 |
| full plans (count) | 9 | 11 | 10 | 19 |
| full sim | 331.353 | 404.987 | 368.170 | 699.523 |
| full excess | 272.918 | 342.543 | 292.248 | 601.716 |
| one worker (count) | 72 | 67 | 104 | 103 |
| profitable worker merges (count) | 0 | 0 | 0 | 0 |


Full plans cover the entire catalog. A full plan can come from a fallback or from ordinary impact selection. The host checked every pair of occupied workers on every emitted plan for a cost-reducing merge.

## Probe outcomes

Each cell gives attempts / charged CPU seconds. The final stage is the last recorded stage before a return, mismatch or cutoff. Each attempt includes all earlier stages and overlay setup. Queue construction, static selection and batch startup are additional costs. Collection passes establish eligibility for a subsequent heavy check and count separately.

| Group / mode / last stage / outcome | Tuning | Holdout |
|---|---:|---:|
| bundle / bundle / bundle / failed | 5 / 1.617 | 1 / 0.307 |
| bundle / bundle / bundle / pass | 4 / 1.465 | 0 / 0.000 |
| bundle / bundle / bundle / timeout | 1 / 0.567 | 0 / 0.000 |
| collection / catalog / catalog / pass | 2 / 5.393 | 0 / 0.000 |
| ds / corpus / corpus / pass | 1 / 0.780 | 0 / 0.000 |
| ds / corpus / corpus / timeout | 0 / 0.000 | 1 / 0.679 |
| ds / rules / rules / failed | 1 / 0.239 | 4 / 0.955 |
| ds / rules / rules / pass | 6 / 6.523 | 1 / 0.770 |
| ds / slice / execute / failed | 1 / 2.279 | 0 / 0.000 |
| ds / slice / generate / timeout | 0 / 0.000 | 1 / 1.534 |
| ds / slice / parse / failed | 1 / 0.232 | 0 / 0.000 |
| ds / slice / parse / timeout | 0 / 0.000 | 1 / 1.019 |
| ds / tokens / tokens / failed | 0 / 0.000 | 1 / 0.570 |
| generator / generator / generator / failed | 1 / 0.478 | 2 / 0.708 |
| generator / generator / generator / pass | 1 / 1.936 | 0 / 0.000 |
| parser / scaled / scaled / pass | 1 / 1.099 | 2 / 2.052 |
| parser / scaled / scaled / timeout | 0 / 0.000 | 2 / 1.731 |
| parser / small / small / timeout | 1 / 1.068 | 0 / 0.000 |

C8 holdout averaged 3.092 CPU seconds across 25 successful drops and averaged 0.846 CPU seconds across 11 failed or timed-out attempts.

Candidate holdout averaged 0.940 CPU seconds across 3 successful drops and averaged 0.577 CPU seconds across 13 failed or timed-out attempts.

Candidate tuning averaged 0.908 CPU seconds across 13 successful drops and averaged 0.589 CPU seconds across 11 failed or timed-out attempts.

The verifier charged 14.423 CPU seconds across 197 holdouts, or 2.93 seconds per 40. The sample charged 2.610 seconds on 30 patches.

## Selection and validation

The selector requires expected simulated savings above 1.25 times estimated CPU. Rule replays use the measured pin CPU of the selected optimizer calls. Tuning supplies smoothed success estimates for each mechanism. The rule estimate is 0.78, compared with 0.86 for parser-corpus replays and 0.25 for small workers. The initial CPU allowance is 2.75 seconds for up to 40 patches. Two paid heavy drops unlock a further 35 percent of the measured savings. On holdout this withheld too many successful checks. Passing TPC-DS selections rose from 28 to 40, passing parser selections from 10 to 18, and full plans from 10 to 19. Coverage restrictions, rejected estimates and the batch cap all retain static selections.

For tokenizer configuration, the selector follows literal values into the pinned tokenizer core. Fresh pytest processes recorded which class and numeric escape key each reader used. For optimizer edits, it replays the affected pinned rule inputs and checks the input tree, returned tree, identity and keyword arguments. A qualifying method-local edit outside recorded paths can avoid execution. Small single-worker plans can recover the setup fee if the required tests and selected plausible tests all pass. A method-local overlay can share imports in a forked interpreter. Changes touching a preloaded pinned module require a clean import. Forked checks still require explicit completion receipts.

| Generated target | Fail kept | Fail dropped | Pass kept | Pass dropped | Unknown kept |
|---|---:|---:|---:|---:|---:|
| TPC-DS | 76 | 0 | 12 | 12 | 1 |
| Parser budget | 22 | 0 | 19 | 60 | 0 |
| Generator memory | 27 | 0 | 12 | 60 | 5 |

The forced matrix used independent pytest labels, a four-second wall cutoff per probe and two Docker CPUs. All 16 inherited tests passed, as did negative controls for import, premature exit, timeout, collection and worker failure. Both 60-patch stress batches produced 60 valid plans. The passing workload took 23.938 seconds and the stalling workload took 22.025 seconds. The broader C8 stress budget spent about 83 seconds on each.

The first rule-corpus build exceeded DuckDB's address-space allowance. Pinning recording to one CPU and bounding address space at 3.4 million KiB allowed it to complete. A cold-start preflight initially failed to import its phase recorder. The release entrypoint initializes the script path before checking ASTs. The published holdout regression exceeds the CPU saving. The lower forecast depends on the public-sample gains transferring to practice.
