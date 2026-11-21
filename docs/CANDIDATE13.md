# Candidate 13

## What the official scores establish

C5 and C11 both scheduled 177.103 simulated seconds on practice, with zero misses. Their CPU costs were 0.67 and 1.56 seconds. C8 scheduled 155.649 simulated seconds at approximately 19 CPU seconds, also with zero misses. The difference, 21.454 seconds, matches the cost reduction of eliminating an isolated TPC-DS selection on local patches. These totals do not identify the hidden edit or its execution path.

The C11 price gate alone cannot explain all the losses. One local 21.454-second slice had estimated pass probability 0.74 and estimated cost 2.916 CPU seconds. It cleared the 1.25 admission ratio, but C11 never attempted it in the diagnostic batch. C8 completed it in 5.165 CPU seconds. C11 capped all probes and their queue construction together at 2.85 CPU seconds for 40 patches. Even first place in an otherwise empty queue would not fund that measured runtime. On another local slice with the same saving, C11 estimated probability 0.32 and cost 8.616 seconds. Here the expected gain was 6.865 seconds, below the 10.770 required by the price gate. Across all local sets, nine distinct C8 TPC-DS receipts saved exactly 21.454 seconds. C11 lost all nine. Three failed the initial price ratio and six cleared it without a recorded attempt. C12 recovered eight of the nine. Separately, at a different schedule gain, C11 failed an intermediate optimizer-rule fingerprint although C8's query comparison passed.

C11's published evaluation kept aggregate holdout receipts. The counts below use its archived diagnostic plan stream for the matched holdout and fresh plans for the remaining comparisons. They describe those runs. Timing and queue exhaustion can change the result of another C11 run.

| Local inventory | Patches | C8 drops lost by C11 | C8 drops lost by C12 |
|---|---:|---:|---:|
| Development including 30 official sample | 659 | 62 | 29 |
| Registered holdout after development duplicate removal | 218 | 12 | 3 |
| Independent heavy mutation matrix | 104 | 4 | 0 |
| Dual-probe stress | 60 | 67 | 43 |
| Stalled-probe stress | 60 | 8 | 0 |

A patch can lose multiple heavy drops. The baseline includes the published C8 release's successful receipts as well as its fresh plans. C8 saved 18.365 seconds on development patch ahlw in the release. Its fresh replay lost that drop. C12 declined it at 0.76 probability and 16.519 estimated CPU seconds. Its prior forecast omitted this regression. The 30 sample patches lost no C8 heavy drops in either candidate.

Across the 659 development patches, C11 lost 28 drops at the price gate and 28 eligible drops without a recorded attempt before the budget ran out. Three generator proofs depended on a TPC-DS removal that never completed and would also have failed the next price gate. Two probes timed out and one failed a rule fingerprint. Across the 218 holdouts, four losses failed the price gate, six eligible probes had no attempt, one corpus timed out, and one parser drop depended on collection that did not complete. The four mutation losses comprised three unfunded probes and one initially price-rejected TPC-DS check whose preceding parser removal was also unfunded. On the 60-patch dual and stalled batches, C11 spent 4.404 and 3.944 CPU seconds constructing the queue. It ran zero probes in both batches.

C12 lost 30 generator drops on development and holdout combined. Each failed its 0.28 probability and 1.35 CPU-second price, including three opportunities repriced after another heavy drop. Its other two ordinary losses were the price-rejected TPC-DS partial replay and an admitted TPC-DS slice that never ran. On the dual-probe batch, the twelve-per-mode limit retained 43 heavy selections absent from C8's plans.

## Verification costs

Removing C8's gain-versus-cost filter and retaining its original queue, time limits and fail-closed checks cost 146.896 CPU seconds on the 161 matched tuning cases and 273.901 on the 197 matched holdouts. The experiment first parsed and pickle-round-tripped up to six active SQL inputs before expensive slice, front and partial checks. Tuning scored 1459.795 simulated seconds and completed 14 TPC-DS, three parser and 17 generator drops. Holdout scored 1712.384 and completed 15, ten and twelve respectively. Both had zero misses. These counts are successful verifier receipts. Static exclusions are additional. The fresh stock C8 replay measured 1468.768 + 76.276 and 1780.202 + 75.272 on the same batches. Removing the filter increased combined cost on both partitions.

C12's forecast of 15.6 CPU seconds was an extrapolation. The measured two-CPU runs spent most of their time on TPC-DS.

| CPU seconds | Matched tuning | Matched holdout |
|---|---:|---:|
| TPC-DS probes | 34.867 | 72.238 |
| Parser | 2.012 | 3.176 |
| Generator | 2.998 | 0.620 |
| Collection | 5.905 | 6.369 |
| Small-worker bundles | 7.410 | 9.692 |
| Queue construction | 3.498 | 3.079 |
| Other verifier overhead | 0.313 | 0.521 |
| Static selection and remaining process-tree cost | 8.007 | 8.028 |
| Total | 65.010 | 103.723 |

Late TPC-DS failures and timeouts, defined as attempts ending at optimization, planning or execution, accounted for 11.828 tuning and 1.579 holdout CPU seconds. Of holdout TPC-DS CPU, 69.485 seconds went to successful proofs. Charging every expensive probe to late failures would misdiagnose the cost.

## Admission and timeout handling

The heavy queue orders checks by the schedule time recoverable per estimated CPU second. Estimates below 0.9 seconds share one denominator so a second cheap check on an already-open patch can recover its newly exposed schedule saving. Ties reuse the active patch and then use stable commit order. Cheap positive-gain checks enter without a pass-probability cutoff. Expensive checks compare the full possible saving to deterministic pin work and retain the C8 price for query modes when that estimate is lower. Corpus and token mismatches may continue within the remaining budget and the schedule saving. An intermediate optimizer mismatch can continue to the affected query's actual result. The separate small-worker bundle policy still uses its observed pass rate.

The normal work allowance is 70 estimated CPU seconds on 40 patches and 96 on 60. Per-probe CPU timers, an outer probe CPU ceiling of 96 or 144 seconds respectively, and the inherited wall supervisor bound expensive or stalled children. Total selector CPU includes additional overlay, static and shutdown costs. A first ungated prototype spent 200.072 CPU seconds on tuning. The first bounded prototype restored the ordinary C8 drops but spent its dense-batch budget on parser checks before completing many TPC-DS proofs.

A forked child inherited the static selector's exception-raising SIGPROF handler. A parser catching exceptions could absorb a spent timer. A synthetic control actually returned a success receipt on the previous image. Resetting SIGPROF to its default action lets the parent identify expiry via waitpid and keep the test. This also prevents the corpus fallback from treating an expiry as a value mismatch and buying a second replay.

## Matched Docker measurements

Each cell gives simulated seconds and total process-tree CPU seconds. Every cell had zero misses. The tuning total includes the sample. C11 holdout used the existing diagnostic receipts; CPU cutoffs produced different plans from its release report. CPU is sensitive to host contention.

| Partition | C8 replay | C11 replay | C12 | C13 |
|---|---:|---:|---:|---:|
| Sample, 30 | 115.613 + 1.886 | 100.296 + 2.221 | 95.064 + 1.519 | 95.064 + 0.943 |
| Tuning, 161 | 1468.768 + 76.276 | 1595.807 + 16.744 | 1346.408 + 65.010 | 1302.610 + 81.409 |
| Registered holdout, 197 | 1780.202 + 75.272 | 1679.679 + 18.485 | 1495.894 + 103.723 | 1485.564 + 94.156 |

C13 charged 43.932 tuning and 60.726 holdout CPU seconds to TPC-DS. Of that, 11.630 and 3.065 seconds respectively went to attempts that failed or expired in optimization, planning or execution. Generator checks cost 17.693 and 6.288 seconds. Across the matched partitions, the new gate restores generator drops at a substantial tuning CPU cost.

## Practice scenarios before the C12 official result

The three official outcomes support two simulation anchors. With zero misses, retaining C8's only observed hidden saving gives **155.649 s** before new savings. Losing it gives **177.103 s** if the other schedules match. Using **3–25 CPU seconds** as a scenario allowance yields **158.6–180.6 s** in the first case and **180.1–202.1 s** in the second. If a new mechanism removes one more 21.454-second selection in addition to C8's, the range becomes **137.2–159.2 s**. No official result establishes that second opportunity. The bounds depend on the stated CPU and schedule assumptions. One miss adds 300 seconds.

The measured 40-patch sample-plus-ten control consumed 4.592 CPU seconds. Replacing one patch with a known optimizer opportunity raised that to 5.436, and replacing it with a known full-price TPC-DS opportunity took 10.040. The three simulated totals were 130.373, 133.621 and 130.589 seconds, all with zero misses. These are different local inputs from practice. C8 actually spent about 19 CPU seconds on practice. Scaling the measured C13 tuning and holdout CPU per patch to forty gives about 20.2 and 19.1 seconds. Different mixes can fall outside the scenario allowance.

## Negative controls and repeatability

The starter validator accepted the default entrypoint. Three sample runs produced the same JSONL, SHA-256 `b07684ac6a6724c33773a5121e726f0d66889028502d3d43f8a75c54b4992674`. Two 60-patch mixed runs scored 396.015 simulated seconds with zero misses and 24.753–25.735 CPU seconds. Their JSONL hash was `c22e7af65ac5533ceff041231882e47b0d691ce27c596cff904ae9e2c205ca81`.

The 104 generated mutation plans matched byte for byte across two runs. All 125 known failing heavy targets and all six explicitly uncertain targets were retained. The selector cleared 17 passing TPC-DS, 77 passing parser and 62 passing generator selections. Labels cover the heavy targets under test and do not support a full-schedule simulated score for this matrix.

All 16 inherited tests passed. Separate controls retained selections on early exit, timeout, failed imports and spawned-worker failure. Clean collection returned zero missing nodes, and a deleted or unimportable parser test module returned precisely its 81 catalog nodes. Two divergent queries reached execution and distinguished equal from different ordered results.

C13 restored all C8 exclusions across 659 development, 218 registered holdout, 104 heavy mutations and both 60-patch deadline controls. No safety waivers were used. The development and holdout schedule comparison included unlabelled diffs. Of those partitions, 310 and 217 patches had audited labels at revision `860e0bf`. They scored 2871.861 and 1804.059 simulated seconds, respectively, with zero misses. CPU across *all* inputs in those two partitions was 405.451 and 115.277 seconds. Six further development labels available in the shared checkout also had zero misses.

The corrected fork retained every stalled TPC-DS selection. On the 60 distinct dual-probe patches both heavy checks passed on all 60, including every C8 removal. Both deadline plan streams matched across the incremental and clean builds. The clean runs took 103.21 and 153.72 wall seconds for the dual and stalled batches. On the independent mutation batch the process tree took 78.417 CPU seconds across three containers. All runtime checks used the starter's two-CPU, read-only, network-disabled flags.

Runtime-source revision `6d30286` passed the no-cache four-CPU, 8 GiB clean build and image load in 38 seconds. Its checkout occupied 9,408,320 bytes. The release image contained the selector, pinned SQLGlot and pin recordings. Private plan streams and host labels stayed outside the image. The release check and its invocations are documented in [the tools guide](../tools/README.md).
