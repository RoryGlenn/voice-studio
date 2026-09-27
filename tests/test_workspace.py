"""Workspace HTTP controls and media access use only the selected local job."""

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
from voice_studio.workspace_http import make_handler


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.job = Path(self.temp)
        self.output = self.job / "output"
        self.output.mkdir()
        (self.job / "plan.json").write_text(
            json.dumps(
                {
                    "title": "Test Book",
                    "author": "Author",
                    "identity_sha256": "identity",
                    "tracks": [
                        {
                            "track": 1,
                            "title": "Chapter One",
                            "segments": [{"number": 1, "speech_text": "Hello world."}],
                        }
                    ],
                }
            )
        )
        folder = self.job / "segments/0001"
        folder.mkdir(parents=True)
        self.audio = folder / "sample.wav"
        self.audio.write_bytes(b"0123456789")
        self.row = {
            "status": "needs_review",
            "raw_file": str(self.audio),
            "raw_sha256": "fingerprint",
            "duration_seconds": 1,
            "attempt": 0,
            "wording_check": {"flagged": True, "recognized": "Hello."},
        }
        (folder / "0001.json").write_text(json.dumps(self.row))
        self.state = {
            "state": "paused",
            "generated": 1,
            "checked": 1,
            "remaining": 0,
            "total": 1,
        }
        monitor = type(
            "Monitor", (), {"snapshot": lambda self: {"cpu_percent": 10, "gpu": None}}
        )()
        self.workspace = Workspace(
            self.job,
            self.output,
            lambda *_: dict(self.state),
            monitor,
            "voice-studio-test.service",
        )
        self.workspace.refresh()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: None)
        self.port = self.server.server_port
        self.server.RequestHandlerClass = make_handler(self.workspace, self.port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.port}"

    def test_chapters_reviews_and_audio_range(self):
        with urllib.request.urlopen(self.base + "/api/workspace") as response:
            d = json.load(response)
        self.assertEqual(d["chapters"][0]["generated"], 1)
        self.assertEqual(d["review"][0]["expected"], "Hello world.")
        with urllib.request.urlopen(
            urllib.request.Request(
                self.base + "/audio?id=1%2F1", headers={"Range": "bytes=2-5"}
            )
        ) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"2345")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/audio?id=../../etc/passwd")
        self.row["raw_file"] = "/etc/passwd"
        (self.job / "segments/0001/0001.json").write_text(json.dumps(self.row))
        with self.assertRaises(ValueError):
            self.workspace.safe_audio("1/1")

    def test_csrf_protection_and_pause(self):
        import re

        with urllib.request.urlopen(self.base) as response:
            html = response.read().decode()
        token = re.search('name="workspace-token" content="([^"]+)"', html)[1]
        body = json.dumps({"action": "pause"}).encode()
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(
                urllib.request.Request(self.base + "/api/action", data=body)
            )
        self.assertEqual(error.exception.code, 403)
        request = urllib.request.Request(
            self.base + "/api/action",
            data=body,
            headers={"X-Workspace-Token": token, "Origin": self.base},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 202)
        self.assertTrue((self.job / "paused").exists())
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(
                urllib.request.Request(
                    self.base + "/api/action",
                    data=body,
                    headers={
                        "X-Workspace-Token": token,
                        "Origin": "https://example.com",
                    },
                )
            )

    def test_resume_requires_idle_and_restores_pause_on_failure(self):
        (self.job / "paused").touch()
        with patch.object(self.workspace, "idle", return_value=False):
            with self.assertRaises(ValueError):
                self.workspace.request({"action": "resume"})
        with patch(
            "voice_studio.audiobook_workspace.subprocess.run",
            side_effect=OSError("service unavailable"),
        ):
            with self.assertRaises(OSError):
                self.workspace.request({"action": "resume"})
        self.assertTrue((self.job / "paused").exists())

    def test_review_rejects_stale_evidence_and_missing_note(self):
        (self.job / "paused").touch()
        with self.assertRaises(ValueError):
            self.workspace.request(
                {"action": "approve", "id": "1/1", "fingerprint": "old"}
            )
        with self.assertRaises(ValueError):
            self.workspace.request(
                {"action": "approve", "id": "1/1", "fingerprint": "fingerprint"}
            )

    def test_download_requires_matching_validated_report(self):
        import hashlib

        final = self.output / "audiobook.m4b"
        final.write_bytes(b"final")
        report = self.output / "draft_report.json"
        report.write_text(
            json.dumps(
                {
                    "identity": "identity",
                    "full_audio_decode": "pass",
                    "m4b_sha256": hashlib.sha256(b"final").hexdigest(),
                }
            )
        )
        self.assertFalse(self.workspace.final_ready("packaging"))
        self.assertTrue(self.workspace.final_ready("complete"))
        final.write_bytes(b"changed")
        self.assertFalse(self.workspace.final_ready("complete"))

    def test_history_survives_new_workspace(self):
        restored = Workspace(
            self.job, self.output, lambda *_: dict(self.state), self.workspace.monitor
        )
        self.assertEqual(restored.history[-1]["generated"], 1)
