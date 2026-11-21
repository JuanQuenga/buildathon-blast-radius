# Candidate 15

Candidate 15 replaces the full TPC-DS execution path with static analysis and complete, bounded frontend checks. An affected dependency outside the checked inputs keeps the test. Production does not accept a matching sample of queries. A separately frozen three-witness variant missed one of 197 registered holdouts. Its aggregate bounded-match class had one true failure in 37 labeled decisions. The replacement withdrew the entire sampled policy before its own holdout replay. Reuse of this partition means it is a regression check, not an independent estimate of final accuracy.

## What the selector checks

- **Predicate inputs.** On the pristine pin, all 99 queries run in fixture order. The recorder checks the ordered digest of every returned table. For positive SQL filters it eagerly visits the pure comparison leaves under both AND/OR operands, including leaves normally skipped by a short circuit. Unsupported expressions require an exact scalar result. The recorder checks that every call to the pinned membership predicate passed through the observer. A 1.4 KB domain records 51 distinct equality, NULL and consumer patterns over 18 queries. The selector interprets a restricted, side-effect-free predicate grammar over this domain. Other edited files must miss the query trace.
- **Invariant branches and explicit defaults.** The selector compares complete Python function trees after specializing observed pin branches. A marker retains each evaluated guard and its position. It rejects changed bindings and unmatched module structure. A separate recording identifies optimizer arguments supplied explicitly on every observed call, including reflection on defaults.
- **Dialect and generator options.** The pin record includes option reads during pytest imports, class initialization and the actual heavyweight tests. The selector checks literal boolean replacements against their owning classes and rejects other edits. Dynamically constructed metadata and suite-order effects remain limits of this evidence.
- **Frontend checks.** Optimizer-rule replay must cover all affected calls under a fixed estimated price. Parser/tokenizer replay compares the entire recorded corpus. A query slice is eligible only if its worker footprint is empty and it has at most four affected frontend inputs. The fork compares parsing, pickle round-trip and reference SQL wherever applicable. A difference, exception, expired limit or uncovered dependency retains TPC-DS.
- **Scheduling.** An omission must reduce the price of the replacement plan. The separate small-worker verifier can check up to two bundles of short unittests per batch. An isolated addition to the initial imports of a plain unittest module retains the affected file and known importers. Other collection hazards keep the broad fallback.

The published image contains the pin observations and reads patch bytes at runtime. Benchmark labels remain in the host-side evaluator.

## Reading the practice result

Candidate 14 recorded 150.503 simulated seconds, 24.1–24.6 CPU seconds and zero misses. D1 recorded 130.483 simulated seconds, 24.26–24.51 CPU seconds and zero misses. Its 20.020-second saving demonstrates a spare TPC-DS selection on practice. The diagnostic intentionally omitted inconclusive tests and missed the sample's serialization failure. C15 retains a large serialization footprint immediately and retains the optimizer comparison failure. Neither official aggregate identifies the class of the hidden patch.

On a 40-patch batch with a similar mix to the reconstructions, budget 0.8–2 CPU seconds. If C15 reproduces D1's schedule, expect roughly **131–133 total seconds**. If it reproduces C14's schedule, expect **151–153**. If the static plan retains both expensive selections at the original C11 schedule, expect **178–180**. Different packing or small-worker outcomes can move these endpoints. Each missed commit adds 300 seconds. Heavier local mutation batches take more CPU.

## Local outcomes

Times are simulated worker seconds / total selector CPU seconds, measured in Docker with the starter flags and a two-CPU quota. The sample is part of matched tuning. C13 and C14 measurements are archived comparable runs. Their probe cutoffs and host CPU measurements vary between runs.

| Partition | C13 | C14 | C15 | Misses |
|---|---:|---:|---:|---:|
| Official sample, 30 | 95.064 / 0.943 | 95.064 / 0.963 | 95.064 / 1.253 | 0 / 0 / 0 |
| Matched tuning, 161 | 1302.610 / 81.409 | 1302.610 / 128.205 | 1585.622 / 19.367 | 0 / 0 / 0 |
| Matched holdout, 197 | 1485.564 / 94.156 | 1483.558 / 84.580 | 1743.463 / 16.499 | 0 / 0 / 0 |
| Generated mutations, 104 | 2660.839 / 78.417 | 2660.839 / 72.396 | 2723.441 / 23.828 | 0 known failing heavy targets lost in each |
| Fixture edits, 36 | 916.344 / historical CPU unavailable here | 449.576 / 0.503 | 449.576 / 0.340 | 0 / 0 / 0 |

C15 retained all 125 known failing heavy targets on generated mutations. Unknown outcomes cannot establish whole-suite recall. The expanded replay covered all known failures on 310 labeled development patches and 217 labeled holdouts. The greater-than-512-row and cross-query state controls retained TPC-DS. A separate narrow optimizer mutation raising only on web_sales failed the actual multiprocessing pytest and stayed scheduled in the production policy.

| 40-patch reconstruction | Simulated s | Total CPU s | Missed |
|---|---:|---:|---:|
| Sample plus ten | 135.945 | 1.401 | 0 |
| Substitute passing optimizer-branch change | 139.193 | 1.573 | 0 |
| Substitute passing membership-predicate change | 136.161 | 1.551 | 0 |
| Two passing TPC-DS selections in the static plan | 147.198 | 1.511 | 0 |
| Same two plus generator and collection cases | 152.454 | 1.640 | 0 |

The two-passing reconstruction previously cost C13 **143.855 simulated + 20.064 CPU**. Its successful membership proof used 5.287 CPU seconds and the inconclusive configuration change used 11.478. C15 removes the membership worker statically and keeps the configuration worker. The augmented reconstruction previously cost C13 **148.881 + 32.377**. These are constructed workloads. None establishes the identity of the hidden selections. [Historical per-stage distribution and reduced-data confusion matrix](PROBE-CPU.md)

## Cost of a wrong decision

A missed commit costs 300 seconds. The break-even probability for saving a complete standalone TPC-DS worker is 23.454 / 300 = 7.82%. Saving 6 seconds lowers the threshold to 2%. The selector prices the actual repacked plan. A passing test in spare capacity has zero removal value. The table uses observed test failures as a conservative proxy for incremental missed commits.

The host-side labels comprise development (including the 30 official sample cases), 197 matched holdouts, 36 fixture edits and the generated mutations. Rows with missing or uncertain target labels are excluded. Related mutations share ancestry, so these fractions are descriptive and do not estimate independent hidden draws.

| TPC-DS evidence at decision | Labeled n | True failures | Observed rate | Production decision |
|---|---:|---:|---:|---|
| Static impact excludes | 409 | 0 | 0% | Omit |
| Complete static or frontend check | 14 | 0 | 0% | Omit if it saves time |
| Unresolved method or registry change | 62 | 15 | 24.2% | Schedule |
| Execution or worker planning reached | 56 | 41 | 73.2% | Schedule |
| Full catalog fallback | 33 | 15 | 45.5% | Schedule |
| Frontend difference or incomplete check | 22 | 16 | 72.7% | Schedule |
| Affected optimizer calls above price cap | 20 | 11 | 55.0% | Schedule |
| Structural or fixture change | 19 | 13 | 68.4% | Schedule |
| Affected query count above cap | 6 | 5 | 83.3% | Schedule |
| Analysis failed | 2 | 2 | 100% | Schedule |

A purely empirical one-sided 95% upper bound for 0/14 is 19.3%. The complete-check decision depends on its coverage and equality assumptions. The rejected sampled-match class had a 2.7% point estimate (1/37) and a confirmed false omission. At its 300-second price, the estimate requires over 8.1 seconds of marginal savings, before uncertainty or CPU. The production rule keeps these inconclusive classes.

For the 28.817-second parser and 6.686-second generator tests, the standalone break-even probabilities are 10.27% and 2.90%. The unresolved parser group failed in 25/45 cases and the full-catalog group in 9/33. The static parser exclusion group failed in 0/530. The generator failed in 25/71 unresolved cases, 16/33 collection fallbacks, 3/3 analysis errors and 29/50 zero-saving selections. Its static exclusions had 0/447 failures and its complete option checks had 0/2. C15 keeps every unresolved selection of either test.

## Final decision

Keep candidate 14 selected for final pending the official C15 result. C15 saved 68.081 CPU seconds on 197 matched holdouts but spent 259.905 additional simulated seconds. A practice improvement would measure the benefit of that particular mix. The final includes unfamiliar changes.

| Explicit 60-patch mixture, assuming zero misses | C14 simulated + CPU | C15 simulated + CPU |
|---|---:|---:|
| Sample frequencies | 192.1 s | 192.6 s |
| 48 sample-like, 12 drawn from the 47 matched exotic, revert and transfer holdouts | 297.1 s | 311.4 s |
| Draw all 60 at matched-holdout frequencies | 477.6 s | 536.0 s |

These plug-in expectations use the observed zero miss rates. An additional 0.1 percentage point of per-patch miss risk adds 18 seconds to the expected final score. The maximum miss penalty is **18,000 seconds** for either candidate. On the fixed catalog, four-worker longest-first packing has a 28.817-second maximum load for the full catalog. Removing jobs in the same order cannot increase the ordered greedy loads. The scheduler tests that packing (or uses fewer workers for fewer than four required tests) and fills only spare capacity. Thus a conservative valid-output ceiling for either entry is **20,209.02 seconds plus selector CPU** over 60 patches. This combines maxima that need not occur together. A run rejected for exceeding the wall deadline is outside that bound.

Two repeated 60-patch plans had identical bytes, zero misses and 256.009 simulated seconds. CPU was 1.776–1.982 seconds and wall time 2.36–2.58 seconds. The runtime checkout was 9,622,549 bytes; its clean four-CPU, 8 GiB build and image load took 58 seconds. All 16 inherited unit tests and the isolation, timeout, failed-import, collection, early-exit and worker controls passed. C15 retains more expensive workers than C8 and C13 on the broad local partitions. Heavy-drop monotonicity fails. This is a practice recommendation, and candidate 14 remains selected for final.
