# Candidate 7

The selector revision is be9751d. The forecast is **154.7 seconds**, conditional on an updated candidate-6 estimate of 164.2. Candidate 5 officially scored 177.8 seconds with 177.103 simulated, 0.67 CPU and zero misses in each run. The earlier 153.1 forecast used 176.2 for candidate 5. Candidate 6 has no official result yet. The measured holdout saving is 46.630 seconds per 197 patches, or 9.47 seconds per 40. The sample saved 0.230 seconds.

| Partition | Patches | Misses C6 / C7 | Simulated seconds C6 / C7 | Docker CPU seconds C6 / C7 |
|---|---:|---:|---:|---:|
| Sample | 30 | 0 / 0 | 115.613 / 115.613 | 2.081 / 1.851 |
| Tuning including sample | 161 | 0 / 0 | 1513.615 / 1497.046 | 83.875 / 66.153 |
| Registered holdout | 197 | 0 / 0 | 1831.459 / 1784.854 | 78.016 / 77.991 |

These are Docker measurements on frozen labels at 8cba26f, in identical batches of at most 40 and with the two-CPU starter flags. The baseline emitted extra diagnostics for classifying its keeps. Its simulated seconds match the original candidate-6 release exactly. Machine timing changed since the earlier 92.974-second holdout CPU measurement. The forecast uses the contemporaneous baseline.

## Probe cost and scheduling

On holdout, TPC-DS went from 12 staged attempts at 4.976 CPU seconds each to 20 at 3.016. Scaled parser attempts went from eight at 0.800 seconds to eight at 0.637. Six token-corpus replays averaged 0.300 seconds and two parse-corpus replays averaged 0.560. The extra TPC-DS attempts used almost all the CPU saved per attempt. All figures include the individual overlay and child startup. The partition CPU additionally includes static selection, classification and scheduling.

The build observes public parse calls, their concrete dialects, arguments and returned ASTs during all 99 pinned queries, including optimizer and execution work. The image stores 189 unique serialized parse inputs. A guarded parser-method change can replay those calls if the pin never entered the changed region outside that boundary. A literal Tokenizer.KEYWORDS edit can replay token streams, including JSON path tokenization, and check that all built-in dialects load. A separate pin trace distinguishes affected complete queries from those needing worker execution. The original full-scale parser coverage guards a three-group replay of the actual small assertions. Changes to lexical bindings cover the whole function and changes to generator status are ineligible for slicing.

Each overlay starts with freshly imported SQLGlot. A six-iteration Docker microbenchmark measured 0.631 CPU seconds for a fresh SQLGlot and executor-fixture import, 0.188 after forking with pandas and DuckDB preloaded, and 0.004 after forking with SQLGlot and the fixtures preloaded. The last figure measures the pristine pin. An overlay would also have to replace references retained by registries, dispatch tables and fixture globals. The release precompiles unchanged modules and caches verdicts by overlay contents within a batch.

The scheduler prices one through eight workers, including the two-second fee on each occupied worker. It rebuilds the plan after every accepted drop and prices remaining probes again. Dropping the parser can make a TPC-DS probe profitable on the same patch.

## Why candidate 6 kept heavy selections on holdout

Each cell is a count of real passes / real failures. Reasons are mutually exclusive. The economic split uses the estimated cost of the 99 prefixes and the extra affected executions on the original static plan.

| Keep reason | TPC-DS pass / fail | Parser pass / fail |
|---|---:|---:|
| Saving at or below prefix cost | 10 / 1 | 0 / 0 |
| Additional execution cost outweighed saving | 11 / 1 | 0 / 0 |
| Plan fingerprint changed | 4 / 0 | 0 / 0 |
| Executor exception | 0 / 2 | 0 / 0 |
| IndexError during replay | 0 / 1 | 0 / 0 |
| Fixture change | 2 / 3 | 2 / 0 |
| Full plan | 5 / 1 | 5 / 1 |
| Registry hazard | 3 / 0 | 2 / 0 |
| Other ineligible edit | 0 / 1 | 0 / 0 |
| Scale guard | 0 / 0 | 3 / 0 |
| Timeout | 0 / 0 | 0 / 0 |
| **Total** | **35 / 10** | **12 / 1** |

Candidate 7 recovered six passing TPC-DS selections from the economic keeps. It retained all 13 parser selections on holdout. Tuning recovered one additional TPC-DS and one parser selection.

## Resource and miss risk

The parent retains its static batch while a separate process verifies the patches. Both the verifier and its replay children have an 1800 MiB address-space limit. A batch supervisor enforces an elapsed deadline of 120 seconds for a 40-patch practice batch and 180 for a 60-patch final, including elapsed static analysis. Individual replay exceptions, timeouts, failed imports and missing receipts keep the previous plan. A supervisor crash or incomplete batch result leaves the static batch available.

All 60 distinct stress overlays required both heavy tests and qualified for both probes. Each changed the source independently to defeat verdict caching. Under the exact Docker flags, including two CPUs, the default entrypoint produced 60 valid plans in **83.293 seconds** from the host, including container startup. The selector reported 82.86 seconds. It completed 35 small parser checks and 34 TPC-DS corpus checks before exhausting the verification budget. The 270-second final limit leaves 186.7 seconds of measured headroom on this workload. Arbitrary input parsing and container startup can exceed this measurement.

There were no missed patches in 197 holdouts. Under independent, representative sampling the one-sided 95% upper bound is 1.51% per patch. The analogous bound on just the six additional omissions is 39.3%. The holdout advantage would disappear at an additional missed-patch rate of 0.079%. The experiment cannot establish a rate that low on unseen patch kinds. Differences in pytest order, cached state, dialect registration or an unobserved caller remain possible.


## Generated mutations and failure checks

Across 101 generated patches, TPC-DS retained 76 failures, retained 11 passes, omitted 13 passes and retained one unknown. Parser retained 22 failures, retained 38 passes and omitted 41 passes. Both mechanisms had zero false drops on the 201 known outcomes. Probe and classification CPU totaled 134.860 seconds for TPC-DS and 32.078 for parser, compared with 290.777 and 26.588 in the candidate-6 archive. The generated suite deliberately enriches failures and supplies no sampling probability for the final.

All 16 inherited tests passed. The separate negative controls passed for a failing spawned worker, parser assertion, timeout, failed collection import and premature zero exit. Early pin-trace builds exceeded a virtual-memory ceiling during DuckDB setup and attempted to pickle a dynamically generated tokenizer class. The release build used a bounded Docker build, tracked tokenizer factories including JSON path, and completed both profile stages. An intermediate opportunity-only version saved fewer tuning seconds and was superseded.
