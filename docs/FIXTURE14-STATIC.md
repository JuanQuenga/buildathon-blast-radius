# Candidate 14 static variant

This variant starts at candidate 5 and adds the candidate-14 fixture reader map. Its default entry point disables runtime probes. The Docker build uses the locked candidate-13 wheels and omits build-time test recording.

On the 30-patch sample it scored 115.613 simulated seconds and 0.252 process-tree CPU seconds with zero misses. On 161 matched tuning patches, including the sample, it scored 1,712.618 simulated plus 5.494 CPU seconds. On 197 matched holdout patches it scored 1,991.767 plus 4.831 CPU seconds. Both partitions had zero misses. All 36 independently labeled SQL fixture regressions passed, including four pair-boundary fallback controls. Their plans cost 449.576 simulated seconds in aggregate.

Using candidate 5's official practice schedule as an anchor, one productive TPC fixture edit and 0.5–1.0 CPU seconds imply 156.4–160.5 total. No productive edit gives 177.6–178.1 total. The main candidate-14 release retains the candidate-13 probes and is the recommended practice submission.
