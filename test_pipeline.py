"""Exercise native abort recovery using real child processes, without a GPU."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audiobook_pipeline import Pipeline
from live_book_progress import progress
from render_book import save_json


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.job = Path(self.temp)
        self.output = self.job / "output"
        self.output.mkdir()
        save_json(
            self.job / "status.json",
            {
                "total_segments": 2,
                "counts": {
                    "pending": 2,
                    "rendered": 0,
                    "verified": 0,
                    "needs_review": 0,
                },
            },
        )

    def test_real_crash_then_resume_check_and_package(self):
        worker = self.job / "worker.py"
        worker.write_text("""import json,os,sys
from pathlib import Path
p=Path(sys.argv[1]); stage=sys.argv[2]
s=json.loads((p/'status.json').read_text())
if stage=='rendering':
 if not (p/'crashed').exists():
  (p/'crashed').touch()
  s['counts']['pending']=1; s['counts']['rendered']=1
  (p/'status.json').write_text(json.dumps(s))
  print('illegal memory access',flush=True)
  os.kill(os.getpid(),9)
 s['counts']['pending']=0; s['counts']['rendered']=2
elif stage=='checking':
 s['counts']['rendered']=0; s['counts']['verified']=2
elif stage=='packaging':
 (p/'output/audiobook.m4b').write_bytes(b'fixture')
(p/'status.json').write_text(json.dumps(s))
""")
        pipeline = Pipeline(self.job, self.output, backoff=0)
        run_worker = pipeline.run_worker
        with patch.object(
            pipeline,
            "run_worker",
            side_effect=lambda stage, command: run_worker(
                stage, [sys.executable, str(worker), str(self.job), stage]
            ),
        ):
            self.assertEqual(pipeline.execute(), 0)
        self.assertEqual(pipeline.state["state"], "complete")
        self.assertEqual(
            len(list((self.job / "worker-logs").glob("*-rendering.log"))), 2
        )
        self.assertEqual(progress(self.job, self.output)["render_percent"], 100)

    def test_retry_limit_persists_across_supervisor_restart(self):
        pipeline = Pipeline(self.job, self.output, retry_limit=2, backoff=0)
        with patch.object(
            pipeline,
            "run_worker",
            side_effect=[
                (0, ""),
                (-6, "illegal memory access"),
                (-6, "illegal memory access"),
            ],
        ) as worker:
            self.assertEqual(pipeline.execute(), 2)
            self.assertEqual(worker.call_count, 3)
        restarted = Pipeline(self.job, self.output, retry_limit=2)
        with patch.object(restarted, "run_worker") as worker:
            self.assertEqual(restarted.execute(), 2)
            worker.assert_not_called()

    def test_unknown_error_not_retried(self):
        pipeline = Pipeline(self.job, self.output, backoff=0)
        with patch.object(
            pipeline, "run_worker", side_effect=[(0, ""), (1, "identity mismatch")]
        ) as worker:
            self.assertEqual(pipeline.execute(), 2)
            self.assertEqual(worker.call_count, 2)
        self.assertEqual(progress(self.job, self.output)["state"], "failed")

    def test_dead_or_stale_worker_never_reported_running(self):
        for pid, heartbeat in [(99999999, __import__("time").time()), (os.getpid(), 1)]:
            save_json(
                self.job / "pipeline-status.json",
                {"state": "rendering", "pid": pid, "heartbeat": heartbeat},
            )
            self.assertEqual(progress(self.job, self.output)["state"], "stopped")

    def test_timeout_kills_worker_and_preserves_diagnostic(self):
        pipeline = Pipeline(self.job, self.output, timeout=0.01)
        code, detail = pipeline.run_worker(
            "rendering", [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        self.assertNotEqual(code, 0)
        self.assertIn("progress timeout", detail)

    def test_failed_replace_keeps_previous_checkpoint(self):
        p = self.job / "durable.json"
        save_json(p, {"before": True})
        with patch.object(Path, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                save_json(p, {"after": True})
        self.assertEqual(json.loads(p.read_text()), {"before": True})

    def test_busy_gpu_waits_then_admits_worker(self):
        pipeline = Pipeline(self.job, self.output)
        from subprocess import CompletedProcess

        with (
            patch.dict(os.environ, {"VOICE_STUDIO_MIN_FREE_GPU_MIB": "6000"}),
            patch(
                "audiobook_pipeline.subprocess.run",
                side_effect=[
                    CompletedProcess([], 0, "2500, 100"),
                    CompletedProcess([], 0, "7200, 5"),
                ],
            ) as query,
            patch("audiobook_pipeline.time.sleep"),
        ):
            self.assertTrue(pipeline.wait_for_gpu())
            self.assertEqual(query.call_count, 2)
        self.assertEqual(pipeline.state["state"], "waiting")

    def test_completed_job_not_repackaged_on_restart(self):
        (self.output / "audiobook.m4b").write_bytes(b"fixture")
        save_json(self.job / "pipeline-status.json", {"state": "complete"})
        pipeline = Pipeline(self.job, self.output)
        with patch.object(pipeline, "run_worker") as worker:
            self.assertEqual(pipeline.execute(), 0)
            worker.assert_not_called()
