# TPC-DS probe CPU

Candidate 13 officially spent about 24 CPU seconds on practice. The organizer reports a batch total. Local reconstructions can reproduce its size, but cannot attribute that total to particular hidden patches.

### Candidate 13 receipts

These are local process-tree CPU seconds across the expanded development, holdout and generated partitions. A terminal stage includes all work in that attempt up to the receipt. Attributing the entire row to the last stage measures the cost of getting a verdict there.

| Terminal stage and receipt | Attempts | Total CPU s | Median CPU s | Maximum CPU s |
|---|---:|---:|---:|---:|
| Full execution, pass | 16 | 134.206 | 7.392 | 19.539 |
| Full execution, failed probe | 14 | 68.562 | 1.318 | 48.847 |
| Full execution, timeout | 5 | 73.250 | 11.458 | 35.074 |
| Plan comparison, pass | 13 | 56.007 | 4.233 | 8.638 |
| Optimization, pass / failed | 6 / 19 | 5.347 / 7.099 | 0.786 / 0.260 | 1.315 / 1.054 |
| Rule replay, pass / failed | 14 / 1 | 9.120 / 0.186 | 0.627 / 0.186 | 1.233 / 0.186 |
| Query corpus, pass / failed | 6 / 6 | 3.351 / 3.323 | 0.564 / 0.551 | 0.618 / 0.565 |
| Parsing, pass / failed | 1 / 10 | 0.150 / 3.808 | 0.150 / 0.349 | 0.150 / 0.870 |
| SQL generation, failed | 6 | 6.642 | 1.086 | 1.225 |
| Tokens, pass / failed | 1 / 5 | 0.363 / 0.856 | 0.363 / 0.158 | 0.363 / 0.323 |

TPC-DS took 372.268 of 599.145 total CPU seconds. Collection took 63.668, generator checks 60.719, small-worker checks 47.415, and parser checks 12.290. Among patches with an attempted TPC-DS probe, 70 used at most one CPU second, 23 used more than one through three, 12 more than three through five, seven more than five through ten, and 11 more than ten.

Development examples include a successful 15.469-second full execution on `ahlw`, a failed 48.847-second execution on `2zr6`, and a 35.074-second expiry on `q9w5`. On the generated mutations, `m026` and `m084` expired after 12.613 and 9.562 seconds. The hidden sample could contain an expensive pass, an expensive failure, or both.

### Which passing selections remain?

In the labeled local sets, candidate 13 still selected TPC-DS on 65 patches where it passed. Of those, 43 had no TPC-DS attempt receipt and an eligible replay mechanism, 18 were ineligible, three timed out, and one had a failed probe. A missing receipt includes admission price, exhausted budget and selections placed in spare capacity. A saved selection has a positive worker-time benefit only if it changes worker count or makespan. This local base rate favors checking unattempted classes when the practice diagnostics indicate savings. It does not establish a hidden cause.

### Reduced-data test

Every labeled local patch where candidate 13 selected TPC-DS was included. Additional patches with successful historical probes brought the main study to 221. The sample includes first and last rows, per-column extrema, NULLs, duplicate values and evenly spaced rows. These choices preserve some edge values but do not guarantee join multiplicities or empty groups.

| Reduced outcome | Real full test passed | Real full test failed |
|---|---:|---:|
| Matched pin results | 84 | **1** |
| Diverged or raised during execution | 0 | 89 |
| Ineligible for replay | 18 | 26 |
| DuckDB reference SQL changed | 3 | 1 |

The table includes one extra generated executor control. The original 221 had zero false clearances. The extra control raises when an input table exceeds 512 rows. No reduced table triggered it, but the real pinned TPC-DS test on full data failed in 5.93 seconds. A separate rerun using the host-mounted study tool reproduced the false clearance. The reference file is the optional reduced fixture emitted by the CPU experiment’s offline recorder. Run the study tool inside the pin image with that file and a directory of patches, then compare its JSONL to full-test labels on the host. [Reduced-data study tool](../tools/study_reduced_tpcds.py)

The production proof therefore uses full fixture data. A matching reduced run alone would have incurred a 300-second penalty on this control.

### Forty-patch reconstruction

The sample-plus-ten batch includes the 30 official sample diffs and ten distinct labeled tuning diffs. The two-opportunity batch replaces two tuning diffs with passing TPC-DS cases: `2qp2`, which candidate 13 clears, and `hjpw`, which candidate 13 keeps. The two actual TPC-DS failures in the sample remain in both batches.

| Two-opportunity batch | C13 CPU s | CPU prototype s |
|---|---:|---:|
| Successful TPC-DS proof on 2qp2 | 5.287 | 0.811 |
| Inconclusive proof on hjpw | 11.478 | 1.016 |
| All probes | 19.594 | 4.279 |
| Total selector | 20.064 | 4.715 |
| Worker seconds / misses | 143.855 / 0 | 143.855 / 0 |

The prototype's other 40-patch runs used 3.474, 4.179 and 4.875 probe CPU seconds, with totals of 3.927, 4.652 and 5.351. Their worker times matched candidate 13 at 130.373, 133.621 and 130.589 seconds. Each had zero misses. The cohorts are constructed examples. Their wall-clock and CPU cutoffs can yield different plans under contention.

The prototype reuses pinned full-data inputs when execution-prefix coverage permits it, checks affected scalar calls and their lazy argument order, and resumes changed optimizer rules until state rejoins the pinned execution. For optimizer-only edits it compares executable plans including CTEs and omits token source locations. It reuses a pinned DuckDB frame only on exact serialized equality. A pinned run verified equality on all 57 reference frames. The verifier still uses candidate 13's forked interpreter and per-patch overlay. Eagerly loading third-party imports before every eligible batch increased the two-opportunity probe cost to 17.390 seconds and lost a generator drop in one run. That change was discarded.

A second pin run recorded which optimizer keyword arguments were supplied explicitly at every observed call, auditing reads of function defaults and counting all 99 worker entries. An edit confined to an overridden boolean default can then skip replay. The proof for the `quote_identifiers` default took 0.002 seconds versus 6.683 for full plan comparison on the same control. Another signature edit in the same batch still went through plan comparison.
