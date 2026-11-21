# Order-preserving CPU experiment

The public tag `experimental-cpu-ordered` (`3b40401`) failed the historical heavy-drop release gate. Keep candidate 14 as the provisional final entry. This experiment is separate from D1. [Tagged source](https://github.com/JuanQuenga/buildathon-blast-radius/tree/experimental-cpu-ordered)

## Matched two-CPU Docker results

| Partition | Candidate 13 worker / CPU s | Experiment worker / CPU s | Misses |
|---|---:|---:|---|
| Official sample, 30 | 95.064 / 0.943 | 95.064 / 1.056 | 0 in both |
| Tuning, 161 including sample | 1302.610 / 81.409 | 1302.610 / 77.241 | 0 in both |
| Registered holdout, 197 | 1485.564 / 94.156 | 1500.213 / 146.827 | 0 in both |
| Generated, 104 | 2660.839 / 78.417 | 2660.839 / 90.895 | 0 known failing heavy targets lost in either |

The generated worker times are raw schedule costs. Labels cover the three heavy tests, so they do not establish whole-suite miss counts. Both selectors retained all 125 known failing heavy targets and all six uncertain targets. The experiment covered every observed failure on the 36 independent fixture controls and both additional executor controls. Its expanded replay missed zero of 310 labeled development patches and zero of 217 labeled holdouts.

The experiment retained one TPC-DS selection that C13 previously cleared on holdout. This increased matched worker time by 14.649 seconds. The single aggregate holdout run also used 52.671 more CPU seconds than C13. Historical C13 and experiment CPU measurements were made on the same machine at different times. CPU cutoffs can affect plans under contention.

## Where the CPU went

A constructed 40-patch batch includes two passing TPC-DS selections, one historically cleared and one retained. Worker cost was 143.855 seconds with zero misses for both selectors.

| Operation | C13 CPU s | Experiment CPU s |
|---|---:|---:|
| Successful full-data proof, `2qp2` | 5.287 | 1.833 |
| Inconclusive configuration edit, `hjpw` | 11.478 | 1.026 |
| All probes | 19.594 | 6.228 |
| Whole selector | 20.064 | 6.774 |

Three other 40-patch batches used 3.556, 4.453 and 5.365 probe CPU seconds with the experiment. Their respective total CPU times were 4.058, 4.912 and 5.813 with zero misses and unchanged worker costs. The ordered implementation exceeded the five-second probe target on two of four constructed batches.

Across the expanded development, holdout and generated traces, 56 successful TPC-DS proofs averaged **3.283 seconds**, with a **1.095-second median**. The 1.5-second mean target remains unmet. Twenty-five of 123 attempted patches exceeded three seconds, compared with 30 for C13. TPC-DS attempts consumed 310.210 CPU seconds versus 372.268 historically. A 34.475-second optimizer timeout and a 25.572-second successful execution remained expensive.

| Terminal proof | Successful receipts | Mean CPU s | Median CPU s |
|---|---:|---:|---:|
| Full execution | 14 | 6.880 | 4.339 |
| Plan comparison | 12 | 5.304 | 5.504 |
| Optimizer rule | 15 | 0.823 | 0.689 |
| Optimizer | 6 | 0.978 | 0.767 |
| Query corpus | 6 | 0.850 | 0.842 |
| Parse / tokens / overridden default | 1 each | 0.201 / 0.337 / 0.001 | Same as mean |

These are the entire attempts' costs through the stated terminal stage. The full reconstruction, local kept-selection classes and reduced-data confusion matrix are in the [CPU study](PROBE-CPU.md).

## Correctness and build

The tagged checkout measured **10,257,513 bytes**. A no-cache four-CPU, 8 GiB build and load took **40 seconds**. Two sample runs produced byte-identical output. The selector kept the true failure on the greater-than-512-row control that matched every reduced-data result. It also kept the real cross-query failure that the preliminary reordered verifier cleared. Cached plans now execute in original fixture order with the same tables as uncached plans. Failure-path controls covered early process exit, import or collection failure, timeout and worker failure. The normalizer accepted a source-location-only change at the plan stage and detected a different query result at execution. [Order regression](https://github.com/JuanQuenga/buildathon-blast-radius/blob/experimental-cpu-ordered/tools/check_executor_sequence.py)

## Conditional hidden forecast

With candidate 14's unchanged 150.503 simulated seconds and 5–10 CPU seconds, this experiment would score **155.5–160.5 seconds**. If hidden work still costs 18–26 CPU seconds, that rises to **168.5–176.5**. If a historical drop is lost as on holdout, adding 14.649 worker seconds puts the two scenarios at **170.2–175.2** or **183.2–191.2**. A new missed commit adds 300 seconds. The measured 40-patch reconstruction provides no evidence of an additional hidden worker removal.
