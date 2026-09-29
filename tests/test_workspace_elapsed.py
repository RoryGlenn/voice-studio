"""Elapsed time retains wall-clock gaps without inventing missing job history."""

import json
import tempfile
import unittest
from pathlib import Path

from voice_studio.workspace_insights import elapsed_time


class ElapsedTimeTests(unittest.TestCase):
    def test_restart_gap_and_completed_time(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            (job / "worker-history.jsonl").write_text(
                "broken\n" + json.dumps({"started_at": 100, "finished_at": 150}) + "\n"
            )
            rows = [{"started_at": 300, "finished_at": 350}]
            for state in ("rendering", "paused", "stopped"):
                result = elapsed_time(job, {"state": state, "observed_at": 400}, rows)
                self.assertEqual(result["elapsed_seconds"], 300)
            for now in (400, 900):
                result = elapsed_time(
                    job, {"state": "complete", "observed_at": now}, rows
                )
                self.assertEqual(result["elapsed_seconds"], 250)

    def test_first_unfinished_batch_and_unknown_history(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            data = {"state": "rendering", "observed_at": 1790719400}
            self.assertIsNone(elapsed_time(job, data, [])["elapsed_seconds"])
            (job / "worker-logs").mkdir()
            (job / "worker-logs/1790719300000000000-rendering.log").touch()
            self.assertEqual(elapsed_time(job, data, [])["elapsed_seconds"], 100)
            self.assertIsNone(
                elapsed_time(job, {**data, "state": "complete"}, [])["elapsed_seconds"]
            )
