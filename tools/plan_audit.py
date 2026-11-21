"""Aggregate scheduling and verifier costs outside the selector container."""
from collections import Counter
import itertools

import score_lib


def attempts(stderr):
    stats = Counter()
    for line in stderr.splitlines():
        fields = line.split()
        if fields[:1] == ["probe-cpu"]:
            stats["probe_cpu"] += float(fields[1])
        if len(fields) < 7 or fields[0] != "heavy":
            continue
        _, _, group, result, mode, cpu, gain, *extra = fields
        if result not in {"pass", "timeout", "failed"}:
            continue
        stage = extra[0] if extra else mode
        key = f"attempt/{group}/{mode}/{stage}/{result}"
        stats[key + "/n"] += 1
        stats[key + "/cpu"] += float(cpu)
        stats[key + "/saved_ms"] += int(gain)
        if len(extra) == 2:
            stats[key + "/expected_passes"] += float(extra[1])
    return stats


def schedule_stats(plan, failures, dur, names):
    cost = score_lib.commit_cost_ms(plan, dur)
    floor, upper = score_lib.oracle_bounds(list(failures), dur)
    fees = 2000 * len(plan)
    loads = [sum(dur[n] for n in w if n in failures) for w in plan]
    failed_load = max(loads, default=0)
    stats = Counter(oracle_ms=floor, oracle_uncertainty_ms=upper-floor,
                    fee_ms=fees, failed_load_ms=failed_load, one_worker=int(len(plan) == 1))
    passing = sorted((-dur[n], n, i) for i, worker in enumerate(plan)
                     for n in worker if n not in failures)
    previous = failed_load
    for size, n, i in passing:
        loads[i] -= size
        peak = max(loads)
        if peak != previous:
            name = names[n].rsplit("::", 1)[-1]
            if name in {"test_execute_tpcds", "test_max_nodes", "test_generator_memory"}:
                bucket = name
            elif "tpch" in name or "tpc_h" in name:
                bucket = "tpch"
            elif names[n].startswith("tests/test_optimizer.py"):
                bucket = "optimizer"
            else:
                bucket = "other"
            stats["passing_ms/" + bucket] += peak - previous
            previous = peak
    assert previous + fees == cost
    if sum(map(len, plan)) == len(dur):
        stats.update(full_plans=1, full_sim_ms=cost, full_excess_ms=cost-floor)
    for i, j in itertools.combinations(range(len(loads)), 2):
        new_peak = max(loads[i]+loads[j], *(loads[k] for k in range(len(loads)) if k not in {i,j}), 0)
        if new_peak + fees - 2000 < cost:
            stats["profitable_worker_merges"] += 1
    return stats
