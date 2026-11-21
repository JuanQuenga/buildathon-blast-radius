# The cost of a short TPC-DS probe

Keep candidate 14 as the final entry. No candidate 17 was tagged. The 2.56 CPU-second cutoff retained the same workers as candidate 14 on both strata of our final projection and increased selector CPU. The full D1 diagnostic missed one sample commit and three unfamiliar holdout commits.

## Where the original probe stops

The 462 inputs comprise 161 tuning cases (including the 30 official samples), 197 holdouts and 104 generated mutations. Four generated TPC-DS outcomes lack labels. Each row below gives the conditional TPC-DS failure count. A complete clearance means the particular admitted C14 proof finished, including cases settled by fingerprints. The CPU column charges only the TPC-DS attempt from the original C14 release receipt. The paired Docker replay identified exception types, the last completed stage and the reason for ending an attempt. All 462 admission/terminal statuses matched the original receipts at the pass/fail/timeout/unattempted level. Static exclusions are reported separately so they do not dilute a risky class.

| C14 terminal state | Inputs | TPC-DS fails | Unknown | Mean probe CPU s |
|---|---:|---:|---:|---:|
| Result digest changed, subsequently confirmed by the test's DataFrame assertion | 1 | 1 | 0 | 0.177 |
| Exception from patched SQLGlot or from DuckDB running changed SQL | 34 | 33 | 0 | 0.634 |
| Proof completed | 31 | 0 | 0 | 3.806 |
| Fingerprint changed, full continuation did not complete | 8 | 8 | 0 | 0.448 |
| Probe could not fingerprint a returned object | 1 | 1 | 0 | 0.148 |
| Original CPU limit reached | 3 | 0 | 0 | 11.203 |
| Wall timeout | 0 | — | 0 | — |
| Batch admission budget exhausted before execution | 17 | 17 | 0 | 0 |
| Cost screen rejected the attempt | 44 | 14 | 4 | 0 |
| Registry or import guard | 20 | 8 | 0 | 0 |
| Ineligible patch structure | 8 | 7 | 0 | 0 |
| Remaining unresolved eligibility decisions | 2 | 2 | 0 | 0 |
| Collection fallback | 1 | 1 | 0 | 0 |
| Already absent from plan | 292 | 0 | 0 | 0 |

Two independent instrumented replays supplied exception and admission details. An exception can be attributable to the patched tree and still be irrelevant to this particular test. One registry-related error occurred on a passing holdout. The four missing generated labels are excluded from failure-rate denominators.

## Time to first evidence

Only **one** of the 92 known TPC-DS failures reached the original DataFrame comparison and demonstrated inequality. On sample patch `2jrq`, the full diagnostic took 2.554 cgroup CPU seconds from probe admission to that assertion. Its empirical 10th, 50th and 90th percentiles are each 2.554 seconds. The conditional survival curve stays at 100% through 2.5 seconds and falls to zero at 2.554. There is no independent holdout assertion time. The other failures exited through different paths or never entered the probe. Their times to a hypothetical assertion are unknown.

For the 33 true failures that raised patch-attributable exceptions, the nearest-rank 10th, 50th and 90th percentiles were **0.191, 0.788 and 1.590** cgroup CPU seconds. The sample serialization error on `3utz` was observable in 0.021 s in the diagnostic. Child and descendant CPU are included in these timings.

The proposed cutoff, rounded up to 2.56 seconds, stopped even `2jrq` on a fresh Docker run. In training (tuning plus generated mutations), two of nine short-cap exits really failed, or 22.2%. Six additional holdout cap exits passed. The measured 2/15 across the combined sets still exceeds the 7.8% threshold. The 0/3 result for the original, much longer timeout cannot calibrate a shorter cutoff. Its one-sided 95% binomial upper bound is 63.2%.

## Applying the 7.8% rule

The measurement harness ran C15's static and complete frontend proofs first, then C14-style probe admission and the D1 assertion-level comparison. It capped TPC-DS CPU across overlay preparation, phases and continuations using a per-container cgroup clock. A host-side policy chose between the exact retained and omitted worker plans exported by the selector. Direct test and source-fixture protections were preserved. Positive assertions, attributable exceptions and unseen states schedule. The remaining decisions used **training labels only**.

| Capped state | Training failures / known outcomes | Decision |
|---|---:|---|
| Static exclusion or complete frontend proof | 0/141 | Omit |
| Completed dynamic proof | 0/10 | Omit |
| Short cap | 2/9 | Schedule |
| Attributable exception | 28/28 | Schedule |
| Fingerprint mismatch or unusable result | 8/8 | Schedule |
| Batch admission budget | 18/18 | Schedule |
| Cost screen | 14/25 | Schedule |
| Registry/import guard | 8/18 | Schedule |
| Structural ineligibility | 3/3 | Schedule |
| Other eligibility decision | 1/1 | Schedule |

## Final projection

Each hypothetical final draws 48 patches with replacement from the 30 official sample cases and 12 from the 47 matched unfamiliar holdouts. The 47 comprise exotic, revert and transfer cases. Score includes one 300-second penalty for each commit missing any known failure. Expected score and probability of zero misses are computed directly. The under-200 probability uses 200,000 paired resamples, seed 20261121. Batch CPU is distributed using each patch's recorded probe costs and an equal share of unassigned startup and bookkeeping CPU. The capped prototype includes instrumentation overhead.

| Policy | Simulated s | CPU s | Expected misses | Penalty s | Expected total s | P(zero misses) | P(total <200) |
|---|---:|---:|---:|---:|---:|---:|---:|
| C14 | 292.087 | 5.001 | 0 | 0 | **297.088** | 100% | 4.27% |
| D1 | 240.308 | 11.485 | 2.366 | 709.787 | **961.580** | 8.90% | 2.40% |
| Calibrated, 2.56 s cap | 292.087 | 12.781 | 0 | 0 | **304.868** | 100% | 3.45% |

The 100% entries describe empirical resampling of cases with no observed misses. Novel change kinds, correlated patches and different final-batch probe admission can change these probabilities. The model provides no estimate for another team's private selector. Allocating all CPU uniformly within each stratum instead gives under-200 probabilities of 4.14%, 2.29% and 2.92%, respectively.

| Partition | C14 sim / CPU / missed commits | D1 | Calibrated |
|---|---:|---:|---:|
| Sample 30 (included in tuning) | 95.064 / 0.963 / 0 | 80.116 / 4.097 / 1 | 95.064 / 3.985 / 0 |
| Tuning 161 | 1302.610 / 128.205 / 0 | 1090.760 / 102.160 / 5 | 1326.831 / 83.713 / 0 |
| Holdout 197 | 1483.558 / 84.580 / 0 | 1161.308 / 126.599 / 10 | 1581.645 / 78.971 / 0 |
| Unfamiliar 47 (included in holdout) | 548.273 / 13.553 / 0 | 439.148 / 19.306 / 3 | 548.273 / 25.091 / 0 |
| Mutations 104, known heavy failures only | 2660.839 / 72.396 / 0 | 1656.939 / 120.469 / 76 | 2682.406 / 87.766 / 0 |

The calibrated and C14 policies agree on the aggregate simulated cost of the two projection strata. The capped policy took an additional 98.087 simulated seconds across the entire holdout set. Candidate 14 remains selected under receipt BB-34D93935.
