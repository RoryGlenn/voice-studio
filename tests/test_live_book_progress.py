"""Progress totals and freshness must reflect persisted job state."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

from voice_studio.live_book_progress import ActivityMonitor, progress


class ProgressTests(unittest.TestCase):
    def test_breakdown_and_stale_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            (job / "status.json").write_text(
                json.dumps(
                    {
                        "total_segments": 10,
                        "counts": {
                            "pending": 3,
                            "rendered": 4,
                            "verified": 2,
                            "needs_review": 1,
                        },
                    }
                )
            )
            (job / "pipeline-status.json").write_text(
                json.dumps(
                    {
                        "state": "rendering",
                        "pid": os.getpid(),
                        "heartbeat": time.time() - 90,
                        "failures": 2,
                    }
                )
            )
            result = progress(job, job)
            self.assertEqual(result["state"], "stopped")
            self.assertEqual((result["generated"], result["remaining"]), (7, 3))
            self.assertEqual(
                (
                    result["verified"],
                    result["held"],
                    result["awaiting_check"],
                    result["unchecked"],
                ),
                (2, 1, 4, 7),
            )
            self.assertGreaterEqual(result["heartbeat_age_seconds"], 90)
            self.assertEqual(result["failures"], 2)
            self.assertFalse(result["output_ready"])

    def test_empty_job_and_missing_health(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            (job / "status.json").write_text(
                json.dumps(
                    {
                        "total_segments": 0,
                        "counts": dict.fromkeys(
                            ["pending", "rendered", "verified", "needs_review"], 0
                        ),
                    }
                )
            )
            result = progress(job, job)
            self.assertEqual(result["render_percent"], 0)
            self.assertIsNone(result["heartbeat_age_seconds"])
            self.assertEqual(result["state"], "stopped")


class ActivityTests(unittest.TestCase):
    def test_cpu_deltas_and_gpu_numbers(self):
        monitor = ActivityMonitor()
        result = subprocess.CompletedProcess([], 0, "Test GPU, 75, 4096, 8192, 60\n")
        with (
            patch(
                "voice_studio.live_book_progress.Path.read_text",
                side_effect=[
                    "cpu 100 0 100 800 0 0 0 0 50 0\ncpu0 100 0 100 800 0 0 0 0",
                    "cpu 150 0 100 850 0 0 0 0 70 0\ncpu0 150 0 100 850 0 0 0 0",
                ],
            ),
            patch(
                "voice_studio.live_book_progress.subprocess.run", return_value=result
            ),
        ):
            monitor.sample()
            self.assertIsNone(monitor.snapshot()["cpu_percent"])
            monitor.sample()
        sample = monitor.snapshot()
        self.assertEqual(sample["cpu_percent"], 50)
        self.assertEqual(sample["cores"], [{"name": "cpu0", "percent": 50}])
        self.assertEqual(sample["gpu"]["utilization"], 75)
        self.assertEqual(sample["gpu"]["memory_total"], 8192)

    def test_missing_gpu_and_proc_are_unavailable(self):
        monitor = ActivityMonitor()
        with (
            patch(
                "voice_studio.live_book_progress.Path.read_text", side_effect=OSError
            ),
            patch(
                "voice_studio.live_book_progress.subprocess.run",
                side_effect=subprocess.TimeoutExpired("nvidia-smi", 1),
            ),
        ):
            monitor.sample()
        self.assertIsNone(monitor.snapshot()["cpu_percent"])
        self.assertIsNone(monitor.snapshot()["gpu"])

    def test_unsupported_gpu_fields_are_not_zero(self):
        monitor = ActivityMonitor()
        result = subprocess.CompletedProcess(
            [], 0, "Test GPU, [N/A], 100, 8192, [N/A]\n"
        )
        with patch(
            "voice_studio.live_book_progress.subprocess.run", return_value=result
        ):
            monitor.sample()
        self.assertIsNone(monitor.snapshot()["gpu"]["utilization"])
        self.assertIsNone(monitor.snapshot()["gpu"]["temperature"])

    def test_memory_uses_available_not_free(self):
        with patch(
            "builtins.open",
            mock_open(
                read_data="MemTotal: 1000 kB\nMemAvailable: 600 kB\nMemFree: 100 kB\nSwapTotal: 200 kB\nSwapFree: 150 kB\n"
            ),
        ):
            memory = ActivityMonitor.read_memory()
        self.assertEqual(memory["percent"], 40)
        self.assertEqual(memory["swap_used"], 50 * 1024)

    def test_process_cpu_and_pid_reuse(self):
        monitor = ActivityMonitor()

        def stat(cpu, started):
            fields = ["0"] * 22
            fields[0], fields[11], fields[19], fields[21] = (
                "R",
                str(cpu),
                str(started),
                "10",
            )
            return "123 (test worker) " + " ".join(fields)

        with (
            patch("voice_studio.live_book_progress.os.listdir", return_value=["123"]),
            patch(
                "voice_studio.live_book_progress.os.sysconf",
                side_effect=lambda key: 100 if key == "SC_CLK_TCK" else 4096,
            ),
            patch(
                "voice_studio.live_book_progress.time.monotonic", side_effect=[1, 3, 5]
            ),
        ):
            with patch("builtins.open", mock_open(read_data=stat(100, 10))):
                self.assertIsNone(monitor.read_processes()[0]["cpu_percent"])
            with patch("builtins.open", mock_open(read_data=stat(400, 10))):
                row = monitor.read_processes()[0]
                self.assertEqual(row["cpu_percent"], 150)
                self.assertEqual(row["name"], "test worker")
                self.assertEqual(row["rss"], 40960)
            with patch("builtins.open", mock_open(read_data=stat(10, 20))):
                self.assertIsNone(monitor.read_processes()[0]["cpu_percent"])
