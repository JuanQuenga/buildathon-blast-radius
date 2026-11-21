# Candidate 6

## What a passing probe establishes

The image builds its reference data from the pinned SQLGlot source, fixture SQL and tables. It runs all 99 TPC-DS prefixes, executes the 57 cases selected by the fixture metadata, and checks their data frames against the original DuckDB references. It saves SHA-256 digests for the token values and source locations, parsed and pickle-round-tripped expressions, generated SQL, DuckDB reference SQL, optimized expression, planner expression and plan. The plan digest includes step class, state and dependencies in a deterministic traversal. A separate execution digest covers the result columns and each row in order. The selector reads these files from the image. The official sample, mutation labels, and development and holdout answers stay with the offline scorer.

The runtime child imports SQLGlot from a strict patch overlay. The 99 prefixes include skipped queries because those queries still parse in the original test. For an edit confined to an existing transformation method, the image's per-query coverage records whether the Python execution stage entered that method or a nested function. The selector requests execution for each matching query. For a value change to an existing parser dispatch table, it includes queries whose execution entered that parser module or used the table's name. Other eligible edits, including module-level changes, request all executions. Multiple changed files take the union. Identical prefix digests and all requested result digests are required to drop the test.

The execution trace records SQLGlot code objects entered during Python execution, their names, and modules imported while processing the query. Pin-derived setup coverage guards an optional table snapshot for parser or optimizer edits. The test, data, CSV fixtures, test infrastructure, file operations, import changes, dialect registration machinery and known shared-registry hazards keep the static selection. A changed module must also import successfully. The child has to finish with a successful exit and an explicit completion receipt. An exception, digest mismatch, timeout, failed import or missing receipt retains the plan.

The fast case relies on an isolated pin trace and a single interpreter replaying the serialized input. A state change in a changed method could reach execution through an alias or process initialization that the pin trace missed. A different sequence of earlier tests can change caches and dialect resolution. An isolated match therefore leaves some risk under a different pytest order or process history.

## Parser budget

The probe runs the pinned literal SELECT, the 500-column SELECT, the five-node failure and the subsequent unlimited parse. For the large statement it keeps the original depth of 100 equality operators and reduces the width from 10,000 to 100. Limits of 10,000 and 500 reproduce both error paths. The child compares the exception class and full message against the pin, including context and position. An unlimited parse of the same reduced SQL supplies a successful reference with 30,201 AST nodes and a digest of the tree.

Changing a limit or recurrence, adding work inside a loop or switching behavior only above the reduced width can make the full test fail while the reduced outcomes match. The selector retains the test for changes around lines covered by the full-sized expensive checks and for numeric and loop or limit constructs in the changed text. The full-coverage guard includes tokenizer and expression implementation files. These rules cannot recognize every indirect threshold or performance regression.

## Scheduling

Candidate 5's narrower verifier still runs first. Further work requires a required heavy test, an economically useful worker plan, and an eligible patch. The selector ranks the new probes by simulated seconds removable per estimated CPU second. The estimate uses pinned per-query execution costs. A free-riding test or a full-suite plan usually gives no feasible saving. The combined verifier has a 70-second wall budget and targets 70 aggregate CPU seconds, starting after static impact analysis. Work must also finish by 120 seconds from selector start. The child timeout is at most 30 seconds. Each replay process has a hard 1,800 MiB address-space limit, inherited by its spawned worker. A two-GiB allocation in a patched import failed while the parent selector remained alive. The selector preserves complete worker plans after every accepted result and checks the JSONL before output.

## Tuning and sample

At label revision 8cba26f, candidate 6 kept all eight true heavy-test failures among the 54 tuning selections inherited from candidate 5. It removed 13 of 35 passing TPC-DS selections and one of 11 passing parser selections. Candidate 5 took 1,712.618 simulated seconds and 6.117 CPU seconds. Candidate 6 took 1,513.615 simulated seconds and 105.972 CPU seconds. The net saving was 99.149 seconds across 161 patches, with zero total misses for either candidate. The public sample is included in tuning. Both sample TPC-DS selections truly failed. Both candidates took 115.613 simulated seconds, with CPU increasing from 0.364 to 2.522 seconds. The measured sample regression was 2.159 seconds.

The [development decision ledger](candidate6-development.csv) covers every selected heavy test in the 161 patches. A retained test can be a free rider, a full-suite selection, a hazard, a failed comparison or an unprofitable replay. The test-level CPU column contains logged checks. The Docker totals also include startup, classification, ranking and scheduling. Each candidate receives the same batches of up to 40, the starter flags and the two-core quota. CPU comes from waiting for the selector process inside the container. The post-run validator independently accepted the default entrypoint on the official sample.

A first implementation often tried all 57 executions. It kept every tuning selection and increased CPU by 103.450 seconds. The per-query coverage gate recovered useful drops. Early pin builds failed on a Python type stored in a plan field and on an unlimited node budget passed as None. The pinned parser expects -1. Both errors were fixed before evaluation. All 16 inherited regression tests and the child failure, timeout, import failure and premature-exit checks passed.

## Other TPC tests

The TPC-H executor uses different queries and tables. A standalone measurement using the same prefix checks on its 22 queries took 2.179 CPU seconds. On tuning, the largest marginal worker-plan saving for the TPC-H executor after removing TPC-DS was 1.679 simulated seconds. The optimized TPC-H executor comparison offered at most 1.102 seconds. The optimizer TPC-H and TPC-DS assertions use different rule arguments, pretty SQL and fixture expectations. Their largest marginal savings were 0.355 and 2.232 seconds. Candidate 6 keeps their static selections. Shared-process incremental TPC-H cost was not validated, and the TPC-DS plan hashes cannot establish those optimizer assertions.

## Resource incident during oracle labelling

One mutation changed SQL equality into inequality. The original TPC-DS test on this mutation expanded a join until its worker reached about 9 GiB RSS on the host. This exceeded the requested machine budget. The worker was killed, and its TPC-DS label remains unknown. The targeted parser oracle completed on that patch. Subsequent oracle runs used a 2,200 MiB memory cgroup with swap disabled and a one-core CPU quota. The incident occurred in ground-truth measurement. It also motivated the per-child address-space ceiling in the release image. The development and holdout fixtures were each replayed once on this hardened image. An earlier frozen image had already been measured before that ceiling was added.

## Registered holdout and forecast

The hardened image kept all 11 true heavy failures across 197 held-out patches. It dropped five of 40 passing TPC-DS selections and eight of 20 passing parser selections. Both candidates missed zero patches. Candidate 5 used 1,978.909 simulated seconds and 12.314 CPU seconds. Candidate 6 used 1,831.459 simulated seconds and 92.974 CPU seconds. The net saving was **66.790 seconds**. This is fewer than half of passing selections in either partition. Some static selections were free riders or belonged to full-suite plans, and the selector retained many passes.

Projecting that holdout gain to 40 patches saves 13.56 seconds against candidate 5's 176.2-second estimate, for **162.6 seconds expected official practice**. This assumes a comparable mix of patches and similar server CPU cost. The public sample instead lost 2.16 seconds. Projecting to a 60-patch final saves **20.34 seconds** on the holdout mix, or 36.95 seconds on the tuning mix, before any new miss. A single missed patch costs 300 seconds.

The observed additional miss frequency was zero in 197 held-out patches. Under a representative independent-patch model the one-sided 95% upper bound is 1.51% per patch. Looking only at the 13 omitted heavy selections gives an upper bound of 20.58% per omission under the analogous independent-selection assumption. The observed holdout advantage breaks even at about 0.113% additional missed patches. These measurements cannot establish that low a probability on unseen edit kinds. The generated stress tests exercise specific failure modes and have no defensible sampling probability for the final.

Two generated parser mutations specifically separated the scales. One raised at the 40,000th AST node. The other changed the diagnostic only for limits of at least 50,000. Both original full tests failed. Forcing the reduced probe to run with its eligibility guard disabled returned a match for both patches. The release guard kept both selections. Changing a tokenizer class flag and an unused keyword mapping produced matching results on both probes and both original tests. Reassigning the SELECT, FROM and comma tokens broke the original tests and the verifier retained them.

An additional development-only run applied the static full-suite and probe hazard guards, then tried every eligible selected heavy test regardless of cost or scheduling benefit. TPC-DS matched on 23 of 35 true passes (65.7%) and kept all six true failures. The scaled parser matched on one of 11 true passes and kept both true failures. Probe and eligibility CPU in this forced run totaled 284.894 and 0.974 seconds respectively. This explains the difference between recognizing a passing TPC-DS selection and saving enough worker time to justify recognizing it. The last three ledger columns contain these offline outcomes.

## Matched release scores

| Partition | Patches | Misses C5 / C6 | Simulated s C5 / C6 | CPU s C5 / C6 | Net s saved | C6 TPC-DS drop / keep | C6 parser drop / keep |
|---|---:|---:|---:|---:|---:|---:|---:|
| Sample | 30 | 0 / 0 | 115.613 / 115.613 | 0.364 / 2.522 | -2.159 | 0 / 2 | 0 / 0 |
| Tuning including sample | 161 | 0 / 0 | 1,712.618 / 1,513.615 | 6.117 / 105.972 | +99.149 | 13 / 28 | 1 / 12 |
| Registered holdout | 197 | 0 / 0 | 1,978.909 / 1,831.459 | 12.314 / 92.974 | +66.790 | 5 / 45 | 8 / 13 |

## Generated mutations

The independent oracle ran both original pytest targets on 101 patches. One TPC-DS outcome was unlabelled, leaving 100 fully labelled patches and 201 known test outcomes. The first 20 patches provide broad class, registry and expression mutations. Seven target scale or dispatch boundaries. The remaining 74 change return values or condition behavior on sites reached by a separate SQL workload. The mutations deliberately enrich failures and are correlated within modules. The counts below reflect the mechanism at every eligible site, independent of worker-plan economics. CPU covers eligibility and probe execution, excluding the pytest oracle.

| Probe | Fail kept | Fail dropped | Pass kept | Pass dropped | Unknown kept | Probe CPU s |
|---|---:|---:|---:|---:|---:|---:|
| TPC-DS, total | 76 | 0 | 11 | 13 | 1 | 290.777 |
| TPC-DS, partial | 43 | 0 | 1 | 4 | 0 | 161.224 |
| TPC-DS, static hazard | 8 | 0 | 7 | 0 | 0 | 0.000 |
| TPC-DS, front | 25 | 0 | 3 | 9 | 1 | 129.553 |
| Parser budget, total | 22 | 0 | 56 | 23 | 0 | 26.588 |
| Parser budget, scaled | 3 | 0 | 1 | 23 | 0 | 26.566 |
| Parser budget, static hazard | 19 | 0 | 55 | 0 | 0 | 0.022 |

The full records include the generated patches, original pytest reports and per-patch probe decisions in the local validation archive. The public selector image contains only the pinned checkout, dependencies, pin-derived data and runtime selector. A clean git-archive rebuild produced byte-identical staged fingerprints and table inputs.
