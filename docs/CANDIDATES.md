# Verified candidates

**Candidate-3 is the practice candidate for human review. Candidate-2 is the conservative fallback.** The candidate-1 and candidate-2 comparison below predates the operand profile. Both tags produce the same simulated totals and miss counts on the matched measurements below. Candidate-2 also covers compiler and discovery effects demonstrated on independent source edits. The choice reflects a judgment about unseen patches. The measurements do not establish a lower contest score.

| Tag | Selector commit | Official sample, exact Docker simulated s | Docker process CPU near exit, s | Misses | Full-suite fallbacks |
| --- | --- | ---: | ---: | ---: | ---: |
| candidate-1 | 60c75b8 | 165.854 | 0.66 | 0 | 0 |
| candidate-2 | 8664f8b | 165.854 | 0.90 | 0 | 0 |
| candidate-3 | 3759b53 | 115.613 | 0.93, 0.95 | 0 | 0 |

Each image was built in a fresh directory extracted from its commit with git archive. Each ran the default command under the starter Makefile's linux/amd64 flags and passed the official validator. The Docker CPU figure is the selector's process time at its last log statement. Only the scoreboard measures official container CPU and the median of three totals.

## Matched host measurements

The table uses eval/run.py, measured user plus system CPU per whole batch, and the selector's actual fallback counter. A plan can cover every test without entering fallback. Simulated time includes miss penalties. Both candidates used the same frozen labels. The selection and miss totals are identical, although individual plans differ.

| Partition | Labeled patches | Simulated s, both | CPU s, candidate-1 | CPU s, candidate-2 | Misses, both | Fallbacks, both |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Official sample | 30 | 165.854 | 0.455 | 0.729 | 0 | 0 |
| Exotic tuning | 50 | 484.580 | 0.808 | 0.851 | 0 | 5 |
| Exotic holdout | 22 | 209.575 | 0.663 | 0.666 | 0 | 1 |
| Independent mechanical holdout | 18 | 132.091 | 0.642 | 0.836 | 0 | 0 |

Zero misses in every represented kind. Sample counts were 16 code, 8 fixture, 3 multi and 3 comment. Exotic tuning contained 11 delete, 11 nonpy, 8 export, 7 rename, 7 newfile, 3 conftest, 2 syntax and 1 importcycle. Holdout reporting is aggregate. Exotic's nominal holdout was historically exposed. Five development duplicates were ineligible on the independent holdout. Dev (443 patches), revert (150) and new transfer (4) had zero audited labels at the freeze. Only 18 of the 155 mechanical holdout patches contributed.

The initial review recorded 73 raw exotic tuning labels. Bench excluded interrupted or unaudited sessions, leaving 50 tuning labels and 22 exotic holdout labels. A file-only collection experiment on the raw tuning labels saved 301.909 schedule seconds but incurred 15 misses. On the 50 audited tuning cases it saved zero seconds. Keep full collection fallback.

Candidate-2 cost 0.514 extra host CPU seconds over 120 measured patches. At 300 seconds per miss, preventing one additional miss per roughly 70,000 patches would cover this observed CPU difference, assuming unchanged schedule costs. Individual fallback decisions can cost up to 36.817 simulated seconds, so broadening from empty to full needs a miss-risk reduction above 12.27 percentage points on affected patches. A previously ignored, newly collected test file incurred that cost in tuning. A new inert conftest saved the same amount. The frequency of either change in the final is unknown.

Regression checks cover a compiler-rejected break, an unreachable yield that converts a function to a generator, an unreachable assignment that changes an earlier name lookup, new conftest modules, new pytest modules, and package shadowing. The 14 inherited tests passed, as did the two new regression tests in commit 159ce05. Static dependencies still miss some dynamically constructed attributes and cross-test side effects.

## Candidate-3 validation

The frozen selector is 3759b53809f4ca260be1ceb50d8aa2100da63500. The clean archive included only selector code and pinned maps in the image. Docker ran the default command twice under the starter flags and produced byte-identical output. The first Make invocation looked for the host validator in the archive and failed to locate score.py. Invoking the starter validator at its actual path passed. A second end-to-end invocation with the archive Makefile and the starter kit path passed. All 16 selector tests passed, as did five controls on operator direction, operands, additional changes and site identity. Profiling the clean SQLGlot pin passed 1,621 tests and 25,454 subtests with 1.8 GB peak memory. Five comparison sites qualified.

| Set | N | Simulated s | CPU s | Misses | Full | Rule triggers |
|---|---:|---:|---:|---:|---:|---:|
| Sample | 30 | 115.613 | 0.557 | 0 | 0 | 2 |
| Mechanical tuning | 1 | 25.454 | 0.145 | 0 | 0 | 0 |
| Exotic tuning | 50 | 484.580 | 0.616 | 0 | 5 | 0 |
| Revert tuning | 22 | 318.822 | 1.312 | 0 | 1 | 0 |
| Mechanical holdout | 150 | 1374.239 | 4.134 | 0 | 13 | 0 |
| Exotic holdout | 22 | 209.575 | 0.277 | 0 | 1 | 0 |
| Revert holdout | 7 | 22.899 | 0.503 | 0 | 0 | 0 |
| Transfer holdout | 4 | 40.901 | 0.207 | 0 | 1 | 0 |

Sample and tuning CPU are measured process-batch totals. Their full column counts selector fallbacks. Holdout figures come from one post-freeze invocation of the bench delegate's candidate_report.py, using its aggregate rows and writing the report outside the shared tree. Holdout CPU is proportional allocation of shuffled batch CPU. Its full column counts all-catalog plans. The separate request for the bench owner's report is in REQUESTS.md. The matched candidate-2 tuning schedules, misses and fallback counts were identical. Candidate-2 host CPU was 0.489 on sample and 2.168 across the three tuning sets. The rule has no independent trigger in this snapshot. Zero holdout misses therefore verifies baseline behavior on represented cases and supplies no estimate of conditional miss risk.

The official-sample bootstrap gives a 40-patch median of 150.8 seconds with a 106.7–207.0 tenth-to-ninetieth percentile range. At 20% unseen edits the median rises to 191.2 seconds. The mechanical holdout model gives 392.9 seconds using original kinds only. On the checked practice board the first three scores were 153.7, 156.8 and 165.5 seconds. Two high-cost passing comparison substitutions on 30 sample patches explain the entire measured schedule improvement. Repeating that rate on practice could compete for first. With one fewer such opportunity per 40 patches, add roughly 25 seconds under otherwise similar costs. A specific placement depends on the private patches. See [the cost attribution](GAP.md).
