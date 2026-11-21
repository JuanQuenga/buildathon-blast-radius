# Benchmark review at 60c75b8

Keep the 30 official patches for calibration, the whole-suite oracle for labelling, and the historical reverts for transfer testing. The generated pool is a stress test. Its raw mean is a poor forecast of either contest batch.

| Set | Patches | Changed lines, median (range) |
|---|---:|---:|
| Official | 30 | 2 (1–5) |
| Dev | 443 | 2 (1–6) |
| Holdout | 155 | 2 (1–6) |
| Exotic | 100 | 2 (0–7617) |
| Revert | 150 | 6 (1–40) |

Changed lines count additions and removals, excluding file headers. Pure rename diffs can have zero. The official mix has 16 code, 8 fixture, 3 multi-file and 3 comment changes. Twelve cause no numbered failure. Small substitutions can have distant effects. One official change fails 37 tests. Dev reproduces the small hunk sizes, but includes 48 test edits, 55 comment edits and repeated operators. Exotic includes whole modules disappearing and deliberately broken imports. Reverts exercise real fixes, conditional on the old production hunk still applying. Kind-weighted draws assume independent cases within these biased pools. A 20% unseen share and batch sizes 40/60 are scenarios. No hidden labels establish those assumptions.

The recorded 30/30 sample agreement supports the mapping from failed pytest reports, including subtests, to catalog numbers. Continuing after collection errors lets other modules run and attributes a failed import to its own module. The official materials do not specify how to treat renamed or deleted catalog tests, global conftest startup failure, or the timeout threshold. Missing reports alone cannot distinguish disappearance from a killed process. Exclude incomplete executions from primary measurements and disclose the convention for missing catalog IDs. A repeated load-induced timeout may still be environmental. Local SQLite also differs from the pinned image.

Five holdout patches are byte-identical to earlier sets. A new seed reuses mutation families and modules. Git history explicitly names a development exotic case in a selector fix (6d4efc7). This establishes development exposure, not deliberate hardcoding. I cannot establish historical blindness from a shared checkout. Exclude development duplicates from held-out estimates, disclose repeated aggregate evaluation and reserve genuinely new cases for transfer testing. Share holdout results only in aggregate, including the holdout partitions of exotic and revert.

The inherited cache version stayed at v1 across changes to pytest collection and timeouts. Treat old labels with collection errors, crashes or timeout evidence as suspect. The inherited harness calls a feasible LPT schedule a lower bound. Use the minimum across worker counts of setup plus the larger of the longest item and rounded-up average load as a mathematical lower bound. Equality with a feasible schedule certifies optimality.

The starter validator and score use integer milliseconds. Full-suite LPT with four workers costs 36.817 seconds per patch. The three-worker starter costs 40.349. Local selector CPU and scenario quantiles require separate reporting from exact simulated seconds. The host harness lacks the image resource limits.

Sources include the [track](https://www.joinbuilders.org/buildathon/tracks/blast-radius), its locally archived HTML and sample kit, and the [scoring contract](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/SCORING.md).

### Split audit

Across all splits, five held-out patches have exact development copies. The mechanical holdout shares 59 of its 79 family names with dev. The new transfer set adds encoding declarations, docstring escapes, SQL fixture metadata and whitespace in expected SQL output. It contributes four small cases and cannot establish a population frequency for any of them.

The selector team recorded one aggregate evaluation of candidate-2 on 18 mechanical and 22 audited exotic holdout patches. Future candidate comparisons therefore reuse a validation set. The remaining blind evidence comes from newly labelled cases and the separately frozen transfer set. [Team request log](../docs/REQUESTS.md)

The downloaded scoring rules (including the inline version) and starter archive matched the archived local bytes during this review. On the 30 official patches, full-suite LPT scored 1,104.510 seconds in both the local harness and the organizer's scorer. The oracle lower bound of 94.351 seconds is attainable on every official patch.

I compared every string literal in the Python selector sources at candidate-1 and candidate-2 against all 882 patch stems. Neither tree contains a matching ID or a literal containing a catalog class node ID (`::Test`). This check covers direct source literals only. The selector uses compressed maps and has been tuned using development failures. Evidence for transfer still comes from its measured performance on new cases.

Across three fresh host runs per candidate on the official batch, output was byte-identical within each candidate. The official scorer measured 165.854 simulated seconds and zero misses in all six runs. Median local CPU was 0.427 seconds for candidate-1 and 0.571 seconds for candidate-2. Both medians are specific to this host and Python environment.

### Historical exposure discovered during candidate-3 review

Archived batch manifests show that a 20:20 ET run predating this review evaluated all 100 exotic patches, including every one of the 27 assigned to holdout. Multiple earlier runs combined the public sample with 45 exotic cases, 15 of them nominally held out. The old verification notes identified one of those cases and discussed its failure in isolation versus the full session. I therefore cannot call the exotic holdout blind. Matched reports retain the registered split for comparison and provide a second holdout total excluding exotic. Scenario draws for unseen changes now exclude exotic as well. The mechanical identifier found in the development gap report belongs to one of the five byte-identical development duplicates that measurements already exclude.

Candidate-3 profiles identity comparisons across the clean pin and records five source locations. On the initial 80-patch tuning cohort its two changed selections saved 50.241 simulated seconds. Neither the selection nor the worker assignment changed on the 176 measured registered holdouts. The measured gain is concentrated on previously examined changes. This snapshot supplies no independent positive example of that optimization transferring. The source profiler derives its observations from clean runs. [Selector profiler](../tools/build_identity_map.py)

### Non-advancing executor iterator

Development patch `2zr6` changes the row iterator increment from one to zero. Its ordinary whole-suite session recorded a 300-second timeout and grew past 3 GB before I stopped it. A short-timeout retry also exhausted its memory budget. Neither partial session supplied a label.

Two complete sessions with an iterator watchdog at 2,048 and 16,384 repeated reads produced the same 49 failing catalog IDs. Only executor tests reached either watchdog. I then ran all 60 executor unittest methods separately on the original patch and the clean pin, with matching memory limits for each pair and a 305-second outer deadline. The 60 controls passed. The 60 patched outcomes agreed with both ordered sessions, including a method that catches the executor exception. The compact label records `iterator_crosschecked`. Primary reports exclude this qualified result because the original whole-suite session did not complete under the shared resource budget. [Reproduction tool](check_iterator.py)

### Release-note exposure and replacement reserve

The selector team's candidate-11 and candidate-12 notes describe individual held-out plans and probe outcomes. Across those notes and the earlier gap and verification notes, 39 distinct held-out cases are named after removing development duplicates. They include 26 mechanical cases, ten historical reverts, two exotic cases and one transfer case. This establishes exposure beyond the previously identified exotic runs. It does not establish that every reference changed a selector rule.

Registered totals remain matched measurements. The additional cohort excludes the entire exotic set and all documented individual exposures. This leaves 154 historical, mechanical and transfer patches when all available labels are included. Other exposure can have occurred outside the tracked notes, and repeated aggregate feedback can influence candidate choice. The same exclusions apply to held-out bootstrap draws.

I froze a 24-patch reserve after candidate-12 and before scoring it or running the oracle. Nineteen patches use the four official kind labels and five exercise additional mechanisms. All 24 apply to the pin and have distinct changed-file and changed-line signatures from the earlier sets. The encrypted archive contains its generator, seed, metadata and patches. The manifest commits to its plaintext digest. Only aggregate scores will be published. The evaluator keeps the decryption material outside the shared repository. The agents share an operating-system account, so this also relies on respecting the evaluator's private workspace. [Freeze manifest](sealed/manifest.json)

Candidate-12 saved 182.006 local seconds including CPU against candidate-11 on the 217-case registered holdout. The saving was 32.490 seconds on the 154-case cohort that excludes the historical exotic runs and named examples. Each cohort was replayed separately, so allocating the difference to specific omitted patches would misstate the effect of batch composition.

I checked the Python string literals in the verified candidate-8 through candidate-12 images. None exactly matched a benchmark patch identifier. Each named three catalog test nodes explicitly. Candidate-12's application tree contained no evaluation directory, answer filename, or byte-identical copy of any of the 882 public benchmark patches or six compact answer files. These checks address direct artifact leakage. The published per-case analysis independently establishes tuning exposure.
