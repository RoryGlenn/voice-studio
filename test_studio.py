"""Behavior tests with a clearly fake speech backend and real audio encoding."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import soundfile as sf

import studio
from test_support import synthetic_profile


class FakeEngine:
    """Generate test tones, with gates for checking busy and cancellation states."""

    def __init__(self, profile: dict[str, Any]) -> None:
        self.texts: list[str] = []
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()
        self.invalid = False

    def prepare(self, key: str, expression: float) -> float:
        """Record entry and optionally wait for a controlled test release."""
        self.started.set()
        if not self.gate.wait(5):
            raise RuntimeError("Fake engine test gate timed out.")
        return 0.0

    def generate(
        self, text: str, settings: dict[str, Any]
    ) -> Iterator[tuple[np.ndarray, int]]:
        """Yield one second of a tone, or invalid audio when requested by a test."""
        self.texts.append(text)
        if self.invalid:
            yield np.array([np.nan], dtype=np.float32), studio.RATE
        else:
            wave = 0.2 * np.sin(
                np.arange(studio.RATE) * (2 * np.pi * 220 / studio.RATE)
            )
            yield wave.astype(np.float32), studio.RATE


class PlanningTests(unittest.TestCase):
    """Check lossless passage boundaries and honest control validation."""

    def test_conserves_long_unicode_numbers_and_paragraphs(self) -> None:
        text = (
            "Dr. Smith says, “Don’t lose 3.14, $1,250, or café.” " * 65
            + "\n\nSecond paragraph: 10–20%, A/B, and negative −5.\n\n"
            + "Yes. " * 90
        )
        chunks = studio.split_text(text)
        self.assertEqual(" ".join(c["text"] for c in chunks).split(), text.split())
        self.assertEqual(sum(c["paragraph_end"] for c in chunks), 3)
        self.assertTrue(
            all(len(c["text"]) <= 400 and len(c["text"].split()) <= 60 for c in chunks)
        )

    def test_word_at_character_limit(self) -> None:
        text = "z" * 400 + " more words."
        self.assertEqual(
            [c["text"] for c in studio.split_text(text)], ["z" * 400, "more words."]
        )

    def test_rejects_unbreakable_word(self) -> None:
        with self.assertRaises(ValueError):
            studio.split_text("z" * 401)

    def test_large_document(self) -> None:
        text = ("A sentence about a garden. " * 19000)[:490000].rsplit(" ", 1)[0]
        chunks = studio.split_text(text)
        self.assertEqual(" ".join(c["text"] for c in chunks).split(), text.split())

    def test_preview_caps_words_and_retains_paragraphs(self) -> None:
        text = "A word.\n\n" + "another " * 200
        settings = studio.validate_request(
            {"text": text, "mode": "preview"}, {"natural": {"available": True}}
        )
        self.assertEqual(len(settings["rendered_text"].split()), 120)
        self.assertIn("\n\n", settings["rendered_text"])
        self.assertEqual(len(settings["text"].split()), 202)

    def test_rejects_unknown_and_unsupported_controls(self) -> None:
        engines = {"natural": {"available": True}, "expressive": {"available": True}}
        for changes in (
            {"speed": float("nan")},
            {"speed": True},
            {"speed": 2},
            {"expression": 0.5},
            {"tone": "deep"},
            {"mode": "other"},
            {"engine": "unknown"},
            {"text": "\x00"},
            {"text": ""},
            {"title": "bad\nname"},
            {"paragraph_pause_ms": -1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                studio.validate_request({"text": "Some words.", **changes}, engines)
        self.assertEqual(
            studio.validate_request(
                {"text": "Some words.", "engine": "expressive", "expression": 0.75},
                engines,
            )["expression"],
            0.75,
        )


class RenderTests(unittest.TestCase):
    """Test real exports and local API behavior without running a speech model."""

    def setUp(self) -> None:
        """Create an isolated studio around a fake engine."""
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        profile = synthetic_profile()
        (self.directory / "voice_profile.json").write_text(json.dumps(profile))
        self.service = studio.Studio(self.directory, FakeEngine)
        self.original_config = self.service.config
        self.service.config = lambda: {
            "engines": {
                "natural": {"available": True},
                "expressive": {"available": True},
            }
        }
        self.server: studio.Server | None = None

    def tearDown(self) -> None:
        """Release blocked tests, stop HTTP threads, and remove temporary data."""
        self.service.cancel_event.set()
        self.service.engine.gate.set()
        self.service.pool.shutdown(wait=True)
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.temp.cleanup()

    def wait(self, job_id: str) -> dict[str, Any]:
        """Wait briefly for an isolated fake render to finish."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.service.snapshot(job_id)
            if job["status"] in studio.TERMINAL and self.service.active is None:
                return job
            time.sleep(0.02)
        self.fail("Render did not finish before test timeout.")

    def test_real_exports_order_and_exact_paragraph_gap(self) -> None:
        job = self.service.submit(
            {"text": "First paragraph.\n\nSecond paragraph.", "paragraph_pause_ms": 600}
        )
        result = self.wait(job["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(
            self.service.engine.texts, ["First paragraph.", "Second paragraph."]
        )
        folder = self.service.renders / job["id"]
        audio, rate = sf.read(folder / "narration.wav")
        self.assertEqual(rate, studio.RATE)
        self.assertEqual(len(audio), round(2.6 * rate))
        self.assertTrue(np.all(audio[rate : round(1.6 * rate)] == 0))
        self.assertEqual(
            studio.file_hash(folder / "narration.mp3"), result["files"]["mp3"]["sha256"]
        )
        self.assertEqual(
            (folder / "spoken.txt").read_text(), "First paragraph.\n\nSecond paragraph."
        )

    def test_speed_changes_speech_but_preserves_gap(self) -> None:
        job = self.service.submit(
            {"text": "First.\n\nSecond.", "speed": 0.8, "paragraph_pause_ms": 700}
        )
        result = self.wait(job["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertAlmostEqual(result["audio_seconds"], 3.2, delta=0.06)
        evidence = json.loads(
            (self.service.renders / job["id"] / "passage_evidence.json").read_text()
        )
        self.assertEqual(evidence[0]["pause_frames"], round(0.7 * studio.RATE))

    def test_busy_cancel_and_recovery(self) -> None:
        self.service.engine.gate.clear()
        job = self.service.submit({"text": "First."})
        self.assertTrue(self.service.engine.started.wait(2))
        with self.assertRaises(RuntimeError):
            self.service.submit({"text": "Concurrent request."})
        self.service.cancel(job["id"])
        self.service.engine.gate.set()
        result = self.wait(job["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse((self.service.renders / job["id"] / "narration.mp3").exists())
        next_job = self.service.submit({"text": "Try again."})
        self.assertEqual(self.wait(next_job["id"])["status"], "completed")

    def test_invalid_audio_fails_without_export(self) -> None:
        self.service.engine.invalid = True
        job = self.service.submit({"text": "Invalid waveform."})
        result = self.wait(job["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("invalid audio", result["error"])
        self.assertFalse(list((self.service.renders / job["id"]).glob("*.wav")))

    def test_cancel_during_encoder_terminates_child_and_releases_worker(self) -> None:
        """Cancel a real sleeping subprocess at the encoding stage, not at load."""
        marker = self.directory / "encoder.pid"
        encoder = self.directory / "slow_encoder.py"
        encoder.write_text(
            f"#!{sys.executable}\n"
            "import os, time\n"
            f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(30)\n"
        )
        encoder.chmod(0o700)
        self.service.ffmpeg = str(encoder)
        job = self.service.submit({"text": "Cancel this export."})
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists(), "The encoding subprocess did not start.")
        self.assertEqual(self.service.snapshot(job["id"])["status"], "encoding")
        started = time.monotonic()
        self.service.cancel(job["id"])
        self.assertEqual(self.wait(job["id"])["status"], "cancelled")
        self.assertLess(time.monotonic() - started, 2)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
        self.assertFalse(list((self.service.renders / job["id"]).glob("*.wav")))

    def test_history_survives_restart_and_interrupted_jobs_fail(self) -> None:
        job = self.service.submit({"text": "A saved narration."})
        self.assertEqual(self.wait(job["id"])["status"], "completed")
        manifest = self.service.renders / job["id"] / "manifest.json"
        interrupted = copy.deepcopy(self.service.snapshot(job["id"]))
        interrupted["status"] = "rendering"
        studio.save_json(manifest, interrupted)
        reopened = studio.Studio(self.directory, FakeEngine)
        try:
            self.assertEqual(reopened.snapshot(job["id"])["status"], "failed")
        finally:
            reopened.pool.shutdown()

    def start_http(self) -> str:
        """Run an isolated local HTTP server on an available port."""
        self.server = studio.Server(0, self.service, "test-session-token")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def test_external_runtime_uses_repository_static_files(self) -> None:
        """Serve the real web assets even though voice data is elsewhere."""
        base = self.start_http()
        self.assertFalse((self.directory / "web").exists())
        with urlopen(base + "/", timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Voice Studio", response.read())
        with urlopen(base + "/app.js", timeout=3) as response:
            self.assertEqual(response.read(), (studio.APP / "web/app.js").read_bytes())

    def test_config_and_native_engine_share_external_roots(self) -> None:
        """Resolve availability and reference paths from supplied data locations."""
        workspace = self.directory / "external-models"
        model = workspace / "models/natural"
        model.mkdir(parents=True)
        (model / "config.json").write_text("{}")
        (model / "test.safetensors").write_bytes(b"test")
        self.service.workspace = workspace
        self.assertTrue(self.original_config()["engines"]["natural"]["available"])
        native = studio.NativeEngine(self.service.profile, workspace, self.directory)
        self.assertEqual(native.root, workspace)
        self.assertEqual(native.reference_dir, self.directory)

    def test_http_auth_origin_host_types_and_traversal(self) -> None:
        base = self.start_http()
        cases = [
            ("/api/jobs", {}, None, 401),
            (
                "/api/jobs",
                {
                    "Authorization": "Bearer test-session-token",
                    "Origin": "https://evil.example",
                },
                None,
                403,
            ),
            (
                "/api/jobs",
                {"Authorization": "Bearer test-session-token", "Host": "evil.example"},
                None,
                403,
            ),
            (
                "/api/jobs",
                {
                    "Authorization": "Bearer test-session-token",
                    "Content-Type": "text/plain",
                },
                b"{}",
                415,
            ),
            (
                "/audio/../../voice_profile.json",
                {"Authorization": "Bearer test-session-token"},
                None,
                404,
            ),
        ]
        for path, headers, data, status in cases:
            with (
                self.subTest(path=path, headers=headers),
                self.assertRaises(HTTPError) as ctx,
            ):
                urlopen(Request(base + path, headers=headers, data=data), timeout=3)
            self.assertEqual(ctx.exception.code, status)

    def test_http_media_range_download_and_incomplete_guard(self) -> None:
        base = self.start_http()
        self.service.engine.gate.clear()
        job = self.service.submit({"text": "One recording."})
        path = f"{base}/audio/{job['id']}/mp3?token=test-session-token"
        with self.assertRaises(HTTPError) as ctx:
            urlopen(path, timeout=3)
        self.assertEqual(ctx.exception.code, 409)
        self.service.engine.gate.set()
        self.assertEqual(self.wait(job["id"])["status"], "completed")
        with urlopen(
            Request(path, headers={"Range": "bytes=0-99"}), timeout=3
        ) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(len(response.read()), 100)
            self.assertTrue(response.headers["Content-Range"].startswith("bytes 0-99/"))
        with urlopen(path + "&download=1", timeout=3) as response:
            self.assertIn("attachment;", response.headers["Content-Disposition"])

    def test_public_favicon_does_not_require_auth(self) -> None:
        """Allow browser icon requests while keeping private configuration protected."""
        base = self.start_http()
        with urlopen(base + "/favicon.ico", timeout=3) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.read(), b"")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(base + "/api/config", timeout=3)
        self.assertEqual(ctx.exception.code, 401)

    def test_quit_rejects_active_render_then_stops_idle_server(self) -> None:
        """Keep active work intact and allow an authenticated idle shutdown."""
        base = self.start_http()
        headers = {
            "Authorization": "Bearer test-session-token",
            "Content-Type": "application/json",
        }
        self.service.engine.gate.clear()
        job = self.service.submit({"text": "Do not abandon this render."})
        with self.assertRaises(HTTPError) as ctx:
            urlopen(Request(base + "/api/quit", data=b"{}", headers=headers), timeout=3)
        self.assertEqual(ctx.exception.code, 409)
        self.service.engine.gate.set()
        self.assertEqual(self.wait(job["id"])["status"], "completed")
        with urlopen(
            Request(base + "/api/quit", data=b"{}", headers=headers), timeout=3
        ) as response:
            self.assertEqual(json.load(response)["status"], "stopping")
        with self.assertRaises(RuntimeError):
            self.service.submit({"text": "Too late for this instance."})


if __name__ == "__main__":
    unittest.main()
