# Hybrid selector release audit

**Keep candidate 14 selected for the final. Use D2 for the next practice slot.** The hybrid failed the holdout total and per-patch CPU gates, so it has no release tag. The measured implementation is available at its immutable commit. [Hybrid source](https://github.com/JuanQuenga/buildathon-blast-radius/tree/62ecb7d22e47a1da2930b5c855c6a93207cc2811)

Candidate 15 used 1.05–1.06 CPU seconds in official practice and scored 173.0 seconds. Candidate 14 scored 174.6 with 150.503 simulated seconds. Their worker schedules differ by 21.454 seconds. D1 reduced C14's worker time by a further 20.020 seconds and covered every labelled failure on that batch. These aggregate receipts leave the identity, count and mechanism of the affected selections unknown.

## Probe admission

A complete dependency or frontend proof removes its test. Otherwise the hybrid compares the probability of clearance times the worker seconds recoverable on the current patch with the measured mean CPU cost. A test that would fit on an existing worker for free offers no recovery. A collection probe is priced against the actual alternative worker plan. Its successful receipt certifies collection and reconciles missing nodes, so its benefit can also depend on downstream tests. The table counts a whole bundle as cleared only when every test passed.

The estimates use C14 development and mutation receipts, including extra development patches that have a timing receipt but no failure oracle. Holdout labels and timings did not set the prices. The decision threshold in the last column is mean CPU divided by clearance probability. The comparison is strict. Below eight receipts, the selector retains C14 admission. C14's prior cost screen rejected an entire region of costly probes. There are zero C14 receipts there, so the hybrid retains that screen too.

| Inconclusive class | C14 attempts | Cleared | Mean CPU s | Probe when recoverable worker s exceed |
|---|---:|---:|---:|---:|
| TPC-DS parser corpus mismatch | 15 | 4 (26.7%) | 0.836 | 3.14 |
| TPC-DS execution or planning reached | 29 | 10 (34.5%) | 3.667 | 10.63 |
| TPC-DS incomplete query set | 9 | 2 (22.2%) | 0.893 | 4.02 |
| TPC-DS incomplete optimizer rule set | 20 | 7 (35.0%) | 2.271 | 6.49 |
| Generator memory, unresolved | 38 | 30 (78.9%) | 2.241 | 2.84 |
| Parser, scaled work | 10 | 5 (50.0%) | 0.596 | 1.19 |
| Parser, small assertions | 8 | 2 (25.0%) | 0.308 | 1.23 |
| Whole small worker | 90 | 27 (30.0%) | 0.427 | 1.42 |
| Full catalog awaiting collection | 34 | 27 (79.4%) | 1.695 | 2.13 |
| TPC-DS optimizer fingerprint mismatch | 4 | 1 (25.0%) | 0.236 | C14 |
| TPC-DS token mismatch | 4 | 0 | 0.120 | C14 |
| TPC-DS full frontend | 5 | 3 (60.0%) | 8.629 | C14 |
| TPC-DS partial execution | 4 | 1 (25.0%) | 16.626 | C14 |
| Generator dialect subset | 1 | 1 | 0.568 | C14 |

The original class mean without the C14 admissibility restriction cost 23.275 and 20.602 CPU seconds on two development patches where the baseline spent 0.820 and 0.487. The published probabilities describe the cases C14 actually attempted. Sampling new cases from the same broad class changed both their cost and clearance rate.

## How much CPU to allow

Among 43 successful C14 development and mutation TPC-DS receipts, the empirical 90th percentile was **11.772 CPU seconds**. A single 12.5-second cap would have retained 40/43 there and 12/13 on holdout. A five-second cap retained 35/43 and 11/13. These are retrospective counts on C14 receipts. CPU speed and earlier probes change the live outcomes. The 12.5-second hybrid lost 29.578 simulated seconds on matched holdout against the hybrid using C14's per-probe limits and failed the aggregate gate.

The final experiment caps an entire TPC-DS attempt at 23.454 seconds across frontend and execution. A result mismatch, parse exception or incomplete execution retains the test. Each query also has a CPU allowance of one second plus four times the pinned full-fixture execution cost. The existing shorter budgets still apply. Comparing row and column counts can reject a result before hashing it. Matching dimensions alone never clear the test.

A measured inconclusive optimizer edit required 100,000,000 row pairs in a single nested-loop join. The pinned executor visited at most 10,000 pairs in any such join across all 57 executed queries. The hybrid retains the test immediately if a nested loop would visit more than 100 times that pinned maximum. The check uses actual input row counts and applies to every full-data probe. A budget overrun records an incomplete probe even if patched code catches its exception. The optimizer replay keeps the original query order and shares the same tables across queries.

## Release measurements

Each cell gives simulated seconds / selector CPU seconds / missed commits. The 104 mutation cases have heavy-test labels. The last number there counts omitted known failing heavy targets among 125 observations. The 36 fixture oracles cover the relevant five executor and optimizer tests. Tuning includes the 30 sample patches.

| Partition | C14 | C15 | Hybrid |
|---|---:|---:|---:|
| Sample, 30 | 95.064 / 0.963 / 0 | 95.064 / 1.253 / 0 | 95.064 / 1.859 / 0 |
| Tuning, 161 | 1,302.610 / 128.205 / 0 | 1,585.622 / 19.367 / 0 | 1,313.887 / 77.281 / 0 |
| Holdout, 197 | 1,483.558 / 84.580 / 0 | 1,743.463 / 16.499 / 0 | 1,506.550 / 129.286 / 0 |
| Mutations, 104 | 2,660.839 / 72.396 / 0 | 2,723.441 / 23.828 / 0 | 2,660.839 / 96.469 / 0 |
| Fixture edits, 36 | 449.576 / 0.503 / 0 | 449.576 / 0.340 / 0 | 449.576 / 0.422 / 0 |

On tuning, the hybrid scored 1,391.168 against C14's archived 1,430.815 and its fresh 1,413.708. On holdout, the hybrid scored 1,635.836 against C14's archived 1,568.138 and its fresh 1,607.804. The fresh C14 runs used the same patch groups and Docker flags. Scores involving CPU depend on host load. A timeout can also change the simulated score.

The 60-patch dual-heavy stress run took 172.71 wall seconds and the stalled-probe run took 167.07, both within the 270-second limit. The representative 60-patch repeat produced equal plan bytes, zero misses and 250.279 simulated seconds on both runs. SHA-256 was `19981945bab1fda149ab3925fc7c53025b49cc6700edb8d8194b22650d3772ef`. The clean four-CPU, 8 GiB build and image load took 99 seconds for a 9,649,168-byte checkout.

The frozen hybrid omitted TPC-DS on **54 of the 56** development, holdout and mutation patches for which C14 had successful TPC-DS proof receipts. This is 96.4% live retention across both complete static proofs and dynamic replay. On the 161 matched tuning, 197 matched holdout and 104 mutation patches, the 28 successful dynamic TPC-DS probes averaged 3.327 CPU seconds. Complete static exclusions do not enter this timing average.

| Successful dynamic proof, terminal stage | Count | Mean CPU s |
|---|---:|---:|
| Token fingerprint | 1 | 0.603 |
| Parser corpus | 6 | 0.949 |
| Optimizer rule | 8 | 1.132 |
| Optimizer | 1 | 1.786 |
| Plan comparison | 7 | 5.511 |
| Full execution | 5 | 7.488 |

A constructed 40-patch batch with one C15 static proof and one inconclusive optimizer edit used 11.172 CPU seconds and 143.855 simulated seconds. The join guard retained the inconclusive test after 6.144 CPU seconds, compared with 11.464 on the historical C14 receipt. On the more relevant 40-patch reconstruction, C15 left both expensive choices unresolved. The hybrid and C14 scheduled 168.827 simulated seconds each, at 21.758 and 27.893 CPU seconds respectively. Both missed zero patches. An earlier run of the same workload cleared another worker. That timing sensitivity is large enough to affect a practice forecast.

**Practice forecast.** Conditional on reproducing C14's 150.503 simulated seconds and attempting both heavy probes, allow 18–24 CPU seconds, or about 168.5–174.5 total. If the costly successful proof times out and restores the 21.454 worker seconds seen in C15, that becomes about 190.0–196.0 total at the same CPU cost. The diagnostic receipts cannot identify which case applies.

D2 keeps the probe and changes the plan only for attempted, inconclusive TPC-DS checks. A D2 worker-time reduction matching D1's 20.020 seconds would put the saving on the attempted side. Little or no reduction points to D3's unattempted side. Repacking and multiple removals can produce an intermediate result. D2 missed a real sample failure, so its role is practice diagnosis. [D2 and D3 controls](PRACTICE-DIAGNOSTICS.md)

## Recall, repeated runs and remaining failures

The hybrid covered both real TPC-DS sample failures. It missed zero commits among 149 additional labelled development patches and 20 additional labelled holdouts. Outside the matched partitions, 350 cases lacked full-suite failure labels. The 36 fixture oracles and the three sequence, result-size and sparse-rule controls all retained their known failures. The final image passed all 16 unit tests and the failure, collection, exit and timeout controls. An earlier image failed a control requiring the broken collection probe to return a specific status within ten seconds. The final image passed that control with its original timeout. Re-recording the nested-loop maximum in the pinned environment reproduced the committed bytes.

### Per-patch CPU

Each cold trial ran the normal selector on a single patch and charged it and all its descendants with `wait4`. An isolated patch gets a different queue allowance from a patch inside a 40-patch batch. The batch measurements above use the original cohorts. Timings from separate runs can move in either direction.

| Cold input set | Patches | Hybrid used more CPU | Increase over 0.1 s | Largest increase s |
|---|---:|---:|---:|---:|
| Dev | 161 | 40 | 14 | 0.801 |
| Holdout | 197 | 15 | 6 | 0.593 |
| Generated | 104 | 16 | 6 | 1.818 |

The 36 fixture controls each used less CPU on the mean of two trials per image.

I reran the five largest tuning increases in C14, hybrid, hybrid, C14 order. All five hybrid means were higher by 0.023–0.242 CPU seconds. In the clearest repeat, C14 used 2.584 and 2.658 seconds, and the hybrid used 2.898 and 2.829. This subset was selected for large initial increases and does not estimate their frequency. It does independently confirm a counterexample to the per-patch CPU gate.

Compared with C14's actual worker plans, the hybrid restored six heavy selections on tuning and six on holdout. The held-out restorations included one TPC-DS test, two max-nodes tests and three generator-memory tests. It restored none on the 104 mutations or either 60-patch stress batch. Matched tuning/holdout/mutation restoration counts were 5/2/1 against C8 and 6/6/0 against C13. Historical heavy-drop monotonicity therefore also fails. The revised release gate allows these restorations only if the partition total remains competitive.

Matching reduced data was unsafe on the earlier 223-case study. Among full-test passes/failures, outcomes were 84/1 for matching reduced results, 0/90 for divergence or error, 18/26 ineligible and 3/1 for changed DuckDB reference SQL. The hybrid uses the full fixture. [Reduced-data controls and confusion matrix](PROBE-CPU.md)

## Final-entry projection

The proxy draws 48 patches at sample frequencies and 12 from the 47 matched unfamiliar holdouts. It assumes every labelled failure is covered.

| Entry | Simulated s | CPU s | Total s |
|---|---:|---:|---:|
| C14 | 292.09 | 5.00 | 297.09 |
| C15 | 307.42 | 3.93 | 311.35 |
| Hybrid | 292.66 | 8.48 | 301.13 |

Over 60 patches, each extra 0.1 percentage point of per-patch miss probability adds 18 seconds in expectation. Sixty missed patches would add 18,000 seconds for any candidate. Unknown final change types prevent a reliable estimate of the probability of a clean run. For this catalog, the previously derived conservative valid-plan ceiling is 20,209.02 seconds plus selector CPU. A run rejected for exceeding the wall deadline falls outside that bound. [Catalog bound](CANDIDATE15.md)
