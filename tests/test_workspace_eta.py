"""Forecast accuracy, stability and unavailable states with synthetic batch history."""

import json
import tempfile
import unittest
from pathlib import Path

from voice_studio.workspace_eta import Estimator, fit_cost


class EstimateTests(unittest.TestCase):
    def test_learns_word_cost_and_passage_overhead(self):
        a, b = fit_cost([(50, w, 100 + 0.1 * w) for w in (500, 1000, 2000, 4000)])
        self.assertAlmostEqual(a, 2)
        self.assertAlmostEqual(b, 0.1)

    def test_history_survives_restart_and_does_not_drift_with_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            words = [10] * 50 + [40] * 50
            plan = {"tracks": [{"segments": [{"text": "word " * w} for w in words]}]}
            rows = []
            for i in range(8):
                w = sum(words[i * 10 : (i + 1) * 10])
                rows.append(
                    {
                        "stage": "rendering",
                        "exit_code": 0,
                        "seconds": 20 + 0.1 * w,
                        "finished_at": 1000 + i * 100,
                        "before": {"pending": 100 - i * 10},
                        "after": {"pending": 90 - i * 10},
                    }
                )
            (job / "worker-history.jsonl").write_text(
                "\n".join(map(json.dumps, rows)) + "\npartial"
            )
            data = {
                "state": "rendering",
                "remaining": 20,
                "total": 100,
                "checked": 0,
                "observed_at": 2000,
            }
            # Eight very short batches do not meet the minimum observation time.
            estimator = Estimator(job, plan)
            self.assertIsNone(estimator.forecast(data, 800)["estimate_seconds"])
            for row in rows:
                row["seconds"] *= 4
            (job / "worker-history.jsonl").write_text("\n".join(map(json.dumps, rows)))
            value = estimator.forecast(data, 800)
            self.assertAlmostEqual(value["estimate_seconds"], 480)
            self.assertLess(value["estimate_low_seconds"], 480)
            self.assertGreater(value["estimate_high_seconds"], 480)
            restarted = Estimator(job, plan).forecast(
                {**data, "observed_at": 9000}, 800
            )
            self.assertEqual(value, restarted)
            self.assertIsNone(
                estimator.forecast({**data, "state": "paused"}, 800)["estimate_seconds"]
            )

    def test_checking_and_failed_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            plan = {"tracks": [{"segments": [{"text": "word"} for _ in range(1000)]}]}
            rows = [
                {
                    "stage": "checking",
                    "exit_code": 0,
                    "seconds": 100,
                    "finished_at": 1000 + i * 100,
                    "before": {"verified": i * 50, "needs_review": 0},
                    "after": {"verified": (i + 1) * 50, "needs_review": 0},
                }
                for i in range(8)
            ]
            rows.append({**rows[-1], "exit_code": 1, "seconds": 100000})
            (job / "worker-history.jsonl").write_text("\n".join(map(json.dumps, rows)))
            value = Estimator(job, plan).forecast(
                {"state": "checking", "total": 1000, "checked": 400}, 0
            )
            self.assertEqual(value["estimate_batches"], 8)
            self.assertEqual(value["estimate_seconds"], 1200)
