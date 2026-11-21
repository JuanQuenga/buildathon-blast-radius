# Candidate 10

The practice forecast is **146.3 simulated + 5.7 CPU = 152.0 seconds**, conditional on zero misses.

## Matched Docker results

| Partition | Patches | Misses C8 / C10 | Simulated seconds C8 / C10 | CPU seconds C8 / C10 |
|---|---:|---:|---:|---:|
| Sample | 30 | 0 / 0 | 115.613 / 97.760 | 2.069 / 2.703 |
| Tuning including sample | 161 | 0 / 0 | 1468.768 / 1562.431 | 106.504 / 32.649 |
| Registered holdout | 197 | 0 / 0 | 1745.024 / 1785.542 | 114.560 / 25.264 |


The host scorer used label revision 8cba26f. Each two-CPU Docker container received only a catalog and patch files. CPU includes startup, the selector and its waited-for children. Each candidate had one aggregate holdout pass. The C8 remeasurement above ran under greater machine contention than the published C8 run of 71.002 tuning and 78.078 holdout CPU seconds. Wall-time cutoffs also changed its tuning schedule by 18.365 seconds.

## Forecast

Candidate 5 officially scored 177.103 + 0.67 = 177.773 seconds. Candidate 8 scored 155.649 + 18.99 = 174.639 at the median CPU. The simulated gain was 21.454 seconds and the extra CPU was 18.32. The earlier 146.7 forecast overstated how many holdout savings would transfer.

For simulation, split 40 effective patches between the public sample and the registered holdout. Fit the weights to the C5-to-C8 official change. C5 and C8 had the same sample simulation, while the 197 holdouts improved by 233.885 seconds. This gives 18.07 effective holdout patches and 21.93 sample patches. Add the weighted changes from C5 to its official simulated time. For CPU, apply the same weights to the changes from C5, then scale the weighted C8 increase to the observed 18.32 seconds. This reproduces both official anchors by construction. Two observations cannot validate transfer to a third selector. Losing the one observed TPC-DS saving moves the simulated score by 21.454 seconds before any other changes.

## Where the simulated time went

Milliseconds are attributed to the critical worker. Start with failing tests on their assigned workers, then add passes longest first and charge each increase in maximum load. Worker fees and these load categories sum to simulated time. The oracle and full-plan rows overlap those categories. The failure-only oracle has matching lower and feasible upper bounds on both partitions.

| Seconds unless marked | Tuning C8 | Tuning candidate | Holdout C8 | Holdout candidate |
|---|---:|---:|---:|---:|
| oracle | 565.366 | 565.366 | 665.624 | 665.624 |
| fee | 488.000 | 460.000 | 582.000 | 556.000 |
| failed load | 312.746 | 312.746 | 348.298 | 352.130 |
| Passing test execute tpcds | 253.487 | 358.091 | 396.770 | 432.307 |
| Passing test max nodes | 215.646 | 273.273 | 262.442 | 320.076 |
| Passing test generator memory | 39.423 | 52.876 | 44.930 | 26.412 |
| Passing tpch | 7.890 | 1.183 | 10.354 | 4.527 |
| Passing optimizer | 84.010 | 59.337 | 48.711 | 40.962 |
| Passing other | 67.566 | 44.925 | 51.519 | 53.128 |
| full plans (count) | 9 | 11 | 10 | 12 |
| full sim | 331.353 | 404.987 | 368.170 | 441.804 |
| full excess | 272.918 | 342.543 | 292.248 | 363.879 |
| one worker (count) | 72 | 66 | 104 | 102 |
| profitable worker merges (count) | 0 | 0 | 0 | 0 |


Full plans cover the entire catalog. A full plan can come from a fallback or from ordinary impact selection. The host checked every pair of occupied workers on every emitted plan for a cost-reducing merge.

## Probe outcomes

Each cell gives attempts / charged CPU seconds. The final stage is the last recorded stage before a return, mismatch or cutoff. Each attempt includes all earlier stages and overlay setup. Queue construction, static selection and batch startup are additional costs. Collection passes establish eligibility for a subsequent heavy check and count separately.

| Group / mode / last stage / outcome | Tuning | Holdout |
|---|---:|---:|
| bundle / bundle / bundle / failed | 5 / 1.490 | 10 / 2.481 |
| bundle / bundle / bundle / pass | 5 / 1.743 | 2 / 0.827 |
| collection / catalog / catalog / pass | 2 / 4.789 | 0 / 0.000 |
| ds / corpus / corpus / failed | 0 / 0.000 | 1 / 0.320 |
| ds / corpus / corpus / pass | 1 / 0.743 | 1 / 0.711 |
| ds / rules / rules / failed | 1 / 0.242 | 4 / 0.886 |
| ds / rules / rules / pass | 6 / 7.221 | 1 / 0.720 |
| ds / slice / execute / timeout | 1 / 2.200 | 0 / 0.000 |
| ds / slice / optimize / pass | 0 / 0.000 | 1 / 1.311 |
| ds / slice / optimize / timeout | 0 / 0.000 | 1 / 0.899 |
| ds / slice / parse / failed | 1 / 0.183 | 0 / 0.000 |
| ds / slice / plan / pass | 0 / 0.000 | 1 / 4.147 |
| ds / tokens / tokens / failed | 0 / 0.000 | 1 / 0.416 |
| generator / generator / generator / failed | 1 / 0.324 | 2 / 0.661 |
| generator / generator / generator / pass | 1 / 1.677 | 0 / 0.000 |
| parser / scaled / scaled / pass | 1 / 0.841 | 3 / 2.323 |
| parser / small / small / timeout | 1 / 0.812 | 0 / 0.000 |

C8 holdout averaged 3.092 CPU seconds across 25 successful drops and averaged 0.846 CPU seconds across 11 failed or timed-out attempts.

Candidate holdout averaged 1.115 CPU seconds across 9 successful drops and averaged 0.298 CPU seconds across 19 failed or timed-out attempts.

Candidate tuning averaged 0.873 CPU seconds across 14 successful drops and averaged 0.525 CPU seconds across 10 failed or timed-out attempts.

The verifier charged 18.305 CPU seconds across 197 holdouts, or 3.72 seconds per 40. The sample charged 2.285 seconds on 30 patches.

## Probe admission and calibration

Each option prices the change in the complete worker plan. It enters the queue if expected seconds saved are at least 1.25 times estimated CPU. Optimizer estimates use the pinned cost of the affected rule inputs. Candidates with zero schedule gain skip verification. The initial allowance for 40 patches is 2.3 CPU seconds, with up to four extra seconds on batches rich in cheap opportunities. Successful probes earn back their cost up to 40% of their verified saving. After two paid heavy drops, 35% of their savings becomes additional allowance. The probe loop caps its CPU allowance at 45 seconds and its wall time at 53 seconds.

| Mechanism | P used on attempts | Tuning passed / attempted | Holdout passed / attempted |
|---|---:|---:|---:|
| Optimizer rule inputs | .78 | 6/7 | 1/5 |
| TPC-DS query slice | .74 | 0/2 | 2/3 |
| Small worker | .25 | 5/10 | 2/12 |
| Generator | .79 | 1/2 | 0/2 |
| Parser corpus for TPC-DS | .86 | 1/1 | 1/2 |
| Scaled parser | .67 | 1/1 | 3/3 |
| Small parser | .75 | 0/1 | — |
| Tokens | .60 | — | 0/1 |
| Collection | .60 | 2/2 | — |

The optimizer rule and generator rates overpredicted holdout success. Four rule mismatches cost 0.886 CPU seconds combined. The expensive holdout query-slice success reached planning and cost 4.147 seconds. A query-slice attempt on tuning reached execution and timed out after 2.200 seconds. Timeouts and mismatches retain the pre-probe plan.

For qualifying method-local changes, the selector first tests the edited lines against pinned coverage. For intersecting changes it selects affected rule inputs or queries and checks their earliest applicable stage. A warm fork shares imports for eligible probes on a patch. Incompatible imports and heap-sensitive checks still use a clean process. Keyword changes use observations from pytest collection and 20 individual pinned tests, including spawned interpreters. The profile includes dictionary lookups for identifiers absent from the pin. The edit guard checks dictionary structure, token enum members and special-key consumers before applying it. Runtime selection uses the saved observations.

On tuning, 28 passing TPC-DS selections, 10 passing parser-budget selections and 26 passing generator-memory selections remained. Respectively 23, 7 and 21 occurred on patches with no verifier attempt of any kind. Method and import guards, predicted gain and exhausted allowance can all leave these tests in the plan. The aggregate holdout kept 32, 12 and 20 passing selections respectively. Its worker fees fell by 26.000 seconds, generator load by 18.518, TPC-H by 5.827 and other optimizer load by 7.749. Additional passing TPC-DS and parser load cost 93.171 seconds. Holdout retained only aggregate outcomes, so a per-patch holdout keep reason and a separate count of exception fallbacks are unavailable.

## Forty-patch checks

The first scenario uses the public sample and ten distinct development patches chosen by kind before scoring. The second replaces one development code change with a tuning optimizer revert previously known to admit a TPC-DS drop. This second scenario tests whether the budget preserves that opportunity. Its construction favors the tested mechanism.

| Scenario | Candidate | Simulated s | Total CPU s | Probe CPU s | TPC-DS kept | Misses |
|---|---|---:|---:|---:|---:|---:|
| Sample plus ten | C5 | 156.494 | 0.661 | — | 2 | 0 |
| Sample plus ten | C8 | 150.922 | 6.000 | — | 2 | 0 |
| Sample plus ten | C10 | 143.255 | 2.954 | 2.312 | 2 | 0 |
| One known opportunity | C5 | 179.682 | 0.502 | — | 3 | 0 |
| One known opportunity | C8 | 154.170 | 8.883 | — | 2 | 0 |
| One known opportunity | C10 | 141.889 | 4.086 | 3.530 | 2 | 0 |

The targeted batch recovered 12.281 more simulated seconds than C8 and exceeded the 3-second probe target by 0.530 seconds. The untargeted batch recovered 7.667 simulated seconds. On 103 additional disjoint tuning patches from revision 11a7998, C8 achieved 1660.102 simulated + 77.808 CPU and C10 achieved 1702.508 + 24.088, both with zero misses. The 23-patch final block consumed 15.408 probe CPU seconds after profitable checks expanded its allowance.

## Release checks

| Generated target | Failing kept / dropped | Passing kept / dropped | Unknown kept |
|---|---:|---:|---:|
| TPC-DS | 76 / 0 | 10 / 14 | 1 |
| Parser budget | 22 / 0 | 6 / 73 | 0 |
| Generator memory | 27 / 0 | 11 / 61 | 5 |

The end-to-end entrypoint processed 104 generated patches. The separately forced verifier retained all 125 known failing target selections and all six unknowns. It dropped 12 passing TPC-DS, 60 passing parser and 60 passing generator selections. Independent pytest controls confirmed an unused keyword left the optimizer and TPC-H tests passing, a used keyword broke the optimizer test, and rule replay distinguished reachable and unreachable exceptions. All 16 inherited tests passed, as did the timeout, collection, worker-failure and premature-exit controls. The two 60-patch stress cases each produced 60 valid plans in 22.520 and 20.216 wall seconds. A clean archive of selector implementation 11a5030 reproduced the tested image's filesystem layers and default command. The starter sample scorer measured 97.760 simulated seconds, zero misses and zero full-suite fallbacks.

The paired holdout total improved by 48.778 seconds despite the simulation regression. Against the previously published C8 holdout CPU, the improvement was 12.296 seconds. The paired tuning total regressed by 19.808 seconds. The 152.0-second official estimate depends on the sample gains transferring and the expensive hidden TPC-DS opportunity surviving. The two-anchor model forecasts 9.3 simulated seconds recovered against official C8, short of 12.
