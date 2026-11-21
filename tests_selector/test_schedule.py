import itertools
import random
import unittest

from selector.schedule import SETUP_MS, pack, plan_cost, schedule


def brute(req, dur):
    """Optimal cost by enumerating assignments (tiny n only)."""
    req = list(req)
    best = None
    for k in range(1, min(8, len(req)) + 1):
        for assign in itertools.product(range(k), repeat=len(req)):
            if assign[0] != 0:
                continue
            loads = [0] * k
            for t, b in zip(req, assign):
                loads[b] += dur[t]
            used = sum(1 for l in loads if l)
            c = max(loads) + SETUP_MS * used
            best = c if best is None else min(best, c)
    return best


class TestSchedule(unittest.TestCase):
    def test_random_small_optimal(self):
        rng = random.Random(1)
        for _ in range(300):
            n = rng.randint(1, 6)
            dur = {i: rng.choice([rng.randint(1, 50), rng.randint(500, 30000)]) for i in range(20)}
            req = set(rng.sample(range(20), n))
            pl = set(rng.sample(range(20), rng.randint(0, 6)))
            ws = schedule(req, pl, dur)
            flat = [t for w in ws for t in w]
            self.assertEqual(len(flat), len(set(flat)))
            self.assertTrue(req <= set(flat))
            self.assertTrue(all(ws))
            self.assertLessEqual(len(ws), 8)
            self.assertEqual(plan_cost(ws, dur), brute(req, dur))

    def test_empty(self):
        self.assertEqual(schedule(set(), {1, 2}, {1: 5, 2: 6}), [])

    def test_free_riders_dont_raise_cost(self):
        rng = random.Random(2)
        for _ in range(200):
            dur = {i: rng.randint(1, 3000) for i in range(200)}
            dur[0] = 28817
            req = set(rng.sample(range(200), rng.randint(1, 30)))
            a = schedule(req, set(), dur, free_riders=False)
            b = schedule(req, set(), dur, free_riders=True)
            self.assertEqual(plan_cost(a, dur), plan_cost(b, dur))
            self.assertTrue(req <= {t for w in b for t in w})

    def test_pack_exact(self):
        dur = {0: 5, 1: 5, 2: 4, 3: 3, 4: 3}  # LPT gives 11, optimum 10
        _, ms = pack(dur, 2, dur)
        self.assertEqual(ms, 10)


if __name__ == "__main__":
    unittest.main()
