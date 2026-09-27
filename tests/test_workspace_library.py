"""Library isolation, completion evidence, and transcript difference coverage."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from voice_studio.audiobook_workspace import Workspace
from voice_studio.live_book_progress import progress
from voice_studio.workspace_http import make_handler
from voice_studio.workspace_insights import insights, wording_diff
from voice_studio.workspace_library import Library


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.monitor = type("Monitor", (), {"snapshot": lambda _: {}})()
        for name in ["first", "second"]:
            job = self.root / name
            (job / "segments/0001").mkdir(parents=True)
            (job / "plan.json").write_text(
                json.dumps(
                    {
                        "title": name,
                        "author": "Test",
                        "tracks": [
                            {
                                "track": 1,
                                "title": "One",
                                "segments": [{"number": 1, "speech_text": "Hello"}],
                            }
                        ],
                    }
                )
            )
            (job / "status.json").write_text(
                json.dumps(
                    {
                        "total_segments": 1,
                        "counts": {
                            "pending": 0,
                            "rendered": 1,
                            "verified": 0,
                            "needs_review": 0,
                        },
                    }
                )
            )
            audio = job / "segments/0001/audio.wav"
            audio.write_bytes(name.encode())
            (job / "segments/0001/0001.json").write_text(
                json.dumps({"status": "rendered", "raw_file": str(audio)})
            )
        self.primary = Workspace(
            self.root / "first", self.root / "output", progress, self.monitor
        )
        self.primary.refresh()
        self.library = Library(self.primary)

    def test_discovery_selection_and_view_only(self):
        (self.root / "alias").symlink_to(self.root / "second", target_is_directory=True)
        self.assertEqual([r["id"] for r in self.library.listing()], ["first", "second"])
        with patch("voice_studio.workspace_library.threading.Thread"):
            second = self.library.get("second")
        self.assertFalse(second.controls_enabled)
        self.assertEqual(second.safe_audio("1/1").read_bytes(), b"second")
        with self.assertRaises(ValueError):
            self.library.get("../second")
        with self.assertRaises(ValueError):
            second.request({"action": "pause"})
        self.assertFalse((second.job / "paused").exists())

    def test_http_routes_keep_job_audio_and_state_separate(self):
        with patch("voice_studio.workspace_library.threading.Thread"):
            self.library.get("second")
        server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: None)
        server.RequestHandlerClass = make_handler(
            self.primary, server.server_port, self.library
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        for route, expected in [
            ("/audio?id=1/1", b"first"),
            ("/jobs/second/audio?id=1/1", b"second"),
        ]:
            with urllib.request.urlopen(base + route) as response:
                self.assertEqual(response.read(), expected)
        with urllib.request.urlopen(base + "/jobs/second/api/workspace") as response:
            self.assertEqual(json.load(response)["book"]["title"], "second")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/jobs/%2E%2E/api/workspace")

    def test_completion_and_alerts_do_not_claim_unvalidated_download(self):
        state = {
            "state": "complete",
            "generated": 1,
            "checked": 1,
            "total": 1,
            "failures": 2,
        }
        disk = type("Disk", (), {"free": 1024})()
        with patch(
            "voice_studio.workspace_insights.shutil.disk_usage", return_value=disk
        ):
            result = insights(self.root, self.root / "output", state, False)
        self.assertIn("validation remains", result["completion"])
        self.assertEqual(len(result["alerts"]), 3)
        self.assertEqual(result["batches"], [])

    def test_word_diff_preserves_text_and_locates_omissions(self):
        left, right = "Use local disks, not cloud.", "Use disks, not clouds."
        result = wording_diff(left, right)
        self.assertEqual("".join(s["text"] for s in result["expected"]), left)
        self.assertEqual("".join(s["text"] for s in result["recognized"]), right)
        self.assertEqual(
            [s["text"] for s in result["expected"] if s["changed"]], ["local", "cloud"]
        )
        self.assertEqual(
            [s["text"] for s in result["recognized"] if s["changed"]], ["clouds"]
        )
        self.assertFalse(wording_diff("Hello world.", "hello world.")["has_changes"])
        self.assertTrue(wording_diff("Hello.", "")["has_changes"])
        self.assertEqual(
            "".join(s["text"] for s in wording_diff("<script>", "")["expected"]),
            "<script>",
        )
