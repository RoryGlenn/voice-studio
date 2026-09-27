"""Exercise generic source mapping, resumability, evidence gates, and real packaging."""

from __future__ import annotations

import errno
import importlib.metadata
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

import audiobook
import book_package
import book_prepare
import draft_audiobook
import runtime
from book_pacing import RATE, dash_insertions, insert_pauses
from doctor import file_hash
from render_book import digest
from test_support import synthetic_profile


class ToneEngine:
    """Produce deterministic signal fixtures without a GPU or a saved real voice."""

    calls = 0

    def __init__(self, *args: object) -> None:
        pass

    def prepare(self, *args: object) -> float:
        return 0.0

    def generate(self, text: str, settings: dict) -> object:
        type(self).calls += 1
        wave = np.concatenate(
            [np.zeros(2400), np.sin(np.arange(24000) * 0.03) * 0.2, np.zeros(2400)]
        ).astype(np.float32)
        yield wave, RATE

    def release(self) -> None:
        pass


class AudiobookChecks(unittest.TestCase):
    """Use an original two-chapter EPUB and actual FFmpeg codecs."""

    def setUp(self) -> None:
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.job = self.root / "job"
        self.enterContext(
            patch.dict(
                os.environ, {"VOICE_STUDIO_GPU_LOCK": str(self.root / "gpu.lock")}
            )
        )
        profile = synthetic_profile()
        profile["reference"] = "reference.wav"
        profile["reference_sha256"] = digest(b"synthetic voice")
        self.directory = self.root / "studio"
        self.directory.mkdir()
        (self.directory / "reference.wav").write_bytes(b"synthetic voice")
        provenance = self.directory / profile["reference_provenance"]
        provenance.parent.mkdir(parents=True)
        provenance.write_text('{"origin":"original synthetic test voice"}')
        (self.directory / "voice_profile.json").write_text(json.dumps(profile))
        for name in ("models/natural", "models/asr"):
            path = self.root / name
            path.mkdir(parents=True)
            (path / "config.json").write_text("{}")
            (path / "model.safetensors").write_bytes(b"synthetic model")
        self.epub = self.root / "source.epub"
        image = io.BytesIO()
        Image.new("RGB", (160, 200), "#5889ba").save(image, "PNG")
        with zipfile.ZipFile(self.epub, "w") as archive:
            archive.writestr(
                "META-INF/container.xml",
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
            )
            archive.writestr(
                "OPS/book.opf",
                '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Water Experiments</dc:title><dc:creator>Example Author</dc:creator></metadata><manifest><item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/><item id="one" href="one.xhtml" media-type="application/xhtml+xml"/><item id="two" href="two.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>',
            )
            archive.writestr("OPS/cover.png", image.getvalue())
            archive.writestr(
                "OPS/one.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml"><body><section><h1>First Experiment</h1><p>A tap fills the <em>small</em> tank. We record twenty four measurements.</p><pre>print("water")</pre><figcaption>The tank has a visible scale.</figcaption></section></body></html>',
            )
            archive.writestr(
                "OPS/two.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Second Experiment</h1><p>The drain does not overflow.</p><table><tr><td>Starting level</td><td>Five liters</td></tr></table></body></html>',
            )
        self.plan = book_prepare.prepare(
            self.epub,
            self.job,
            self.root,
            self.directory,
            "default",
            None,
            "omit",
            Path("models/asr"),
        )
        ToneEngine.calls = 0

    def recognize(self, path: Path, model: Path, workspace: Path) -> dict:
        track = int(path.parent.parent.name)
        number = int(path.stem.split("-")[0])
        return {
            "text": self.plan["tracks"][track - 1]["segments"][number - 1]["text"],
            "segments": [],
        }

    def render_all(self) -> None:
        audiobook.render(self.job, self.plan, engine_factory=ToneEngine)
        audiobook.check(self.job, self.plan, self.recognize)

    def test_cover_strip_has_headphone_icon_and_preserves_artwork(self) -> None:
        original = Image.new("RGB", (320, 480), "#b7526f")
        data = io.BytesIO()
        original.save(data, "PNG")
        target = self.root / "headphone-cover.png"
        evidence = book_prepare.cover_strip(data.getvalue(), target)
        with Image.open(target) as result:
            self.assertEqual(result.size, (320, 520))
            self.assertEqual(
                result.crop((0, 0, 320, 480)).tobytes(), original.tobytes()
            )
            strip = np.asarray(result)[480:]
        white = np.all(strip > 180, axis=2)
        columns = np.flatnonzero(white.any(axis=0))
        self.assertLessEqual(abs(int(columns[0]) + int(columns[-1]) - 319), 3)
        # The first component is the approximately 21 px headphone mark, then
        # a clear gap before the AUDIOBOOK label. Ear pads extend below its arch.
        split = int(np.flatnonzero(np.diff(columns) > 3)[0])
        icon_left, icon_right = int(columns[0]), int(columns[split])
        self.assertGreaterEqual(icon_right - icon_left, 18)
        self.assertLessEqual(icon_right - icon_left, 23)
        self.assertGreaterEqual(int(columns[split + 1]) - icon_right, 5)
        self.assertTrue(white[20:31, icon_left : icon_left + 5].any())
        self.assertTrue(white[20:31, icon_right - 4 : icon_right + 1].any())
        self.assertTrue(evidence["headphone_icon"])

    def test_recover_broken_checkpoint_preserves_existing_audio(self) -> None:
        audiobook.render(self.job, self.plan, maximum=1, engine_factory=ToneEngine)
        track = self.plan["tracks"][0]
        path = audiobook.record_path(track, track["segments"][0], self.job)
        original = json.loads(path.read_text())
        path.write_text("")
        audiobook.recover_checkpoints(self.job, self.plan)
        self.assertEqual(json.loads(path.read_text()), original)
        self.assertTrue(list((self.job / "recovery").rglob(path.name)))

    def test_recover_broken_attempt_regenerates_only_damaged_passage(self) -> None:
        audiobook.render(self.job, self.plan, maximum=2, engine_factory=ToneEngine)
        track = self.plan["tracks"][0]
        first = audiobook.record_path(track, track["segments"][0], self.job)
        second = audiobook.record_path(track, track["segments"][1], self.job)
        unchanged = second.read_bytes()
        row = json.loads(first.read_text())
        Path(row["raw_file"]).with_suffix(".json").write_text("")
        audiobook.recover_checkpoints(self.job, self.plan)
        self.assertFalse(first.exists())
        audiobook.render(self.job, self.plan, maximum=1, engine_factory=ToneEngine)
        self.assertEqual(second.read_bytes(), unchanged)
        self.assertTrue(Path(row["raw_file"]).exists())
        self.assertEqual(json.loads(first.read_text())["attempt"], row["attempt"] + 1)

    def test_mapping_resume_check_and_real_m4b(self) -> None:
        self.assertEqual(self.plan["title"], "Water Experiments")
        self.assertEqual(len(self.plan["tracks"]), 2)
        self.assertEqual(self.plan["omissions"][0]["reason"], "standalone code policy")
        self.assertIn("small tank", self.plan["tracks"][0]["segments"][1]["text"])
        self.assertEqual(book_prepare.PAUSES["paragraph_to_heading"], 1.0)
        audiobook.render(self.job, self.plan, maximum=1, engine_factory=ToneEngine)
        first = audiobook.record_path(
            self.plan["tracks"][0], self.plan["tracks"][0]["segments"][0], self.job
        )
        original = first.read_bytes()
        self.render_all()
        self.assertEqual(ToneEngine.calls, self.plan["total_segments"])
        self.assertNotEqual(first.read_bytes(), original)
        with patch(
            "runtime.initialize",
            side_effect=AssertionError("Packaging must not allocate a GPU"),
        ):
            report = book_package.finish(self.plan, self.job, self.root / "exports")
        self.assertEqual(report["chapters"], 2)
        self.assertEqual(report["full_audio_decode"], "pass")
        self.assertTrue(report["quicktime_chapters_verified"])
        self.assertEqual(report["checked_passages"], self.plan["total_segments"])
        boundary = report["tracks"][0]["boundaries"][-1]
        self.assertEqual(boundary["kind"], "paragraph_to_heading")
        self.assertAlmostEqual(boundary["result_seconds"], 1.0)

    def test_long_ascii_and_unicode_chapter_names_keep_full_metadata(self) -> None:
        titles = ["An extended section title " * 16 + "ASCII", "界観測章節" * 24]
        long_epub = self.root / "long-headings.epub"
        with (
            zipfile.ZipFile(self.epub) as source,
            zipfile.ZipFile(long_epub, "w") as target,
        ):
            for item in source.infolist():
                content = source.read(item.filename)
                if item.filename == "OPS/one.xhtml":
                    content = content.replace(b"First Experiment", titles[0].encode())
                elif item.filename == "OPS/two.xhtml":
                    content = content.replace(b"Second Experiment", titles[1].encode())
                target.writestr(item, content)
        self.job = self.root / "long-heading-job"
        self.plan = book_prepare.prepare(
            long_epub,
            self.job,
            self.root,
            self.directory,
            "default",
            None,
            "omit",
            Path("models/asr"),
        )
        self.render_all()
        report = book_package.finish(
            self.plan, self.job, self.root / "long-heading-exports"
        )
        names = [
            Path(row[kind]).name for row in report["tracks"] for kind in ("wav", "mp3")
        ]
        self.assertEqual(len(set(names)), 4)
        self.assertTrue(all(len(name.encode("utf-8")) <= 255 for name in names))
        for number, row in enumerate(report["tracks"], 1):
            stem = Path(row["wav"]).stem
            self.assertTrue(stem.startswith(f"{number:02d} - "))
            self.assertRegex(stem, r"-[a-f0-9]{12}$")
            self.assertLessEqual(
                len(("." + stem + ".partial.wav").encode("utf-8")), 255
            )
        probe = json.loads(
            subprocess.check_output(
                [
                    runtime.binary("ffprobe"),
                    "-v",
                    "error",
                    "-show_chapters",
                    "-of",
                    "json",
                    report["m4b"],
                ]
            )
        )
        self.assertEqual(
            [chapter["tags"]["title"] for chapter in probe["chapters"]], titles
        )
        from mutagen.id3 import ID3

        self.assertEqual(
            [str(ID3(row["mp3"])["TIT2"]) for row in report["tracks"]], titles
        )
        self.assertEqual(
            book_package.chapter_stem(1, "First Experiment"), "01 - First Experiment"
        )
        self.assertNotEqual(
            book_package.chapter_stem(1, titles[0] + "A"),
            book_package.chapter_stem(1, titles[0] + "B"),
        )
        self.assertEqual(
            book_package.chapter_stem(2, titles[1]),
            book_package.chapter_stem(2, titles[1]),
        )

    def test_packaging_resolves_tools_with_restricted_path(self) -> None:
        self.render_all()
        tools = {name: runtime.binary(name) for name in ("ffmpeg", "ffprobe")}
        empty_path = self.root / "empty-path"
        empty_path.mkdir()
        with (
            patch.dict(os.environ, {"PATH": str(empty_path)}),
            patch(
                "book_package.runtime.binary", side_effect=tools.__getitem__
            ) as resolve,
        ):
            report = book_package.finish(
                self.plan, self.job, self.root / "restricted-path-exports"
            )
        self.assertEqual(report["full_audio_decode"], "pass")
        self.assertEqual(
            {call.args[0] for call in resolve.call_args_list}, {"ffmpeg", "ffprobe"}
        )

    def test_cuda_package_changes_invalidate_frozen_runtime_identity(self) -> None:
        packages = (
            "nvidia-cuda-runtime-cu12",
            "nvidia-cuda-nvcc-cu12",
            "nvidia-cuda-cccl-cu12",
        )
        version = importlib.metadata.version
        frozen = self.plan["identity"]["runtime"]
        original_plan = (self.job / "plan.json").read_bytes()
        for package in packages:
            with self.subTest(package=package):
                self.assertIn(package, frozen["packages"])
                with patch(
                    "importlib.metadata.version",
                    side_effect=lambda name: (
                        "changed-version" if name == package else version(name)
                    ),
                ):
                    self.assertNotEqual(runtime.identity(), frozen)
                    with self.assertRaisesRegex(ValueError, "Runtime or model changed"):
                        audiobook.load_plan(self.job)
                self.assertEqual((self.job / "plan.json").read_bytes(), original_plan)

        def absent(name: str) -> str:
            if name in packages:
                raise importlib.metadata.PackageNotFoundError(name)
            return version(name)

        with patch("importlib.metadata.version", side_effect=absent):
            self.assertTrue(
                all(runtime.identity()["packages"][name] is None for name in packages)
            )

    def test_paused_or_completed_jobs_do_not_prepare_a_model(self) -> None:
        (self.job / "paused").write_text("user paused")
        with patch.object(
            ToneEngine, "prepare", side_effect=AssertionError("must not load")
        ):
            audiobook.render(self.job, self.plan, engine_factory=ToneEngine)
        (self.job / "paused").unlink()
        self.render_all()
        with patch.object(
            ToneEngine, "prepare", side_effect=AssertionError("must not load")
        ):
            audiobook.render(self.job, self.plan, engine_factory=ToneEngine)

    def test_pause_and_orphan_checkpoint_recovery(self) -> None:
        (self.job / "paused").write_text("user paused")
        audiobook.render(self.job, self.plan, engine_factory=ToneEngine)
        self.assertEqual(ToneEngine.calls, 0)
        (self.job / "paused").unlink()
        audiobook.render(self.job, self.plan, maximum=1, engine_factory=ToneEngine)
        first = audiobook.record_path(
            self.plan["tracks"][0], self.plan["tracks"][0]["segments"][0], self.job
        )
        saved = json.loads(first.read_text())
        first.unlink()  # Simulate interruption after immutable evidence but before checkpoint.
        audiobook.render(self.job, self.plan, maximum=1, engine_factory=ToneEngine)
        self.assertEqual(
            json.loads(first.read_text())["raw_sha256"], saved["raw_sha256"]
        )
        self.assertEqual(ToneEngine.calls, 2)

    def test_held_wording_blocks_finish_and_resume_preserves_it(self) -> None:
        audiobook.render(self.job, self.plan, engine_factory=ToneEngine)
        audiobook.check(
            self.job,
            self.plan,
            lambda *args: {"text": "Incorrect words", "segments": []},
        )
        count = ToneEngine.calls
        audiobook.render(self.job, self.plan, engine_factory=ToneEngine)
        self.assertEqual(ToneEngine.calls, count)
        with self.assertRaisesRegex(ValueError, "Unresolved wording"):
            book_package.finish(self.plan, self.job, self.root / "exports")

    def test_draft_export_keeps_held_checkpoint_and_decodes(self) -> None:
        audiobook.render(self.job, self.plan, engine_factory=ToneEngine)
        first = self.plan["tracks"][0]["segments"][0]

        def recognize(path: Path, model: Path, workspace: Path) -> dict:
            result = self.recognize(path, model, workspace)
            if path.parent.parent.name == "0001" and path.stem.startswith("0001-"):
                result["text"] = "Incorrect words"
            return result

        audiobook.check(self.job, self.plan, recognize)
        first_path = audiobook.record_path(self.plan["tracks"][0], first, self.job)
        saved = first_path.read_bytes()
        report = draft_audiobook.finish_draft(self.job, self.root / "draft")
        self.assertEqual(report["state"], "draft")
        self.assertEqual(report["held_passages"], ["1/1"])
        self.assertEqual(report["full_audio_decode"], "pass")
        self.assertEqual(report["chapters"], 2)
        self.assertEqual(first_path.read_bytes(), saved)
        self.assertEqual(json.loads(saved)["status"], "needs_review")

    def test_changed_source_runtime_or_audio_cannot_be_reused(self) -> None:
        self.render_all()
        first = audiobook.record_path(
            self.plan["tracks"][0], self.plan["tracks"][0]["segments"][0], self.job
        )
        row = json.loads(first.read_text())
        raw = Path(row["raw_file"])
        raw.write_bytes(raw.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "audio checksum"):
            audiobook.validate_record(
                row,
                self.plan["tracks"][0],
                self.plan["tracks"][0]["segments"][0],
                self.plan,
            )
        (self.job / "source.epub").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "source"):
            audiobook.load_plan(self.job)

    def test_repair_and_review_preserve_raw_evidence(self) -> None:
        audiobook.render(self.job, self.plan, maximum=1, engine_factory=ToneEngine)
        audiobook.check(
            self.job, self.plan, lambda *args: {"text": "wrong", "segments": []}
        )
        track, segment = self.plan["tracks"][0], self.plan["tracks"][0]["segments"][0]
        path = audiobook.record_path(track, segment, self.job)
        previous = json.loads(path.read_text())
        audiobook.render(
            self.job,
            self.plan,
            selected="1/1",
            seed_offset=100,
            engine_factory=ToneEngine,
        )
        audiobook.check(self.job, self.plan, self.recognize)
        current = json.loads(path.read_text())
        self.assertEqual(current["status"], "verified")
        self.assertNotEqual(current["raw_file"], previous["raw_file"])
        self.assertEqual(file_hash(Path(previous["raw_file"])), previous["raw_sha256"])
        self.assertTrue(list((path.parent / "history").glob("*.json")))

    def test_plan_metadata_tampering_is_rejected(self) -> None:
        plan = json.loads((self.job / "plan.json").read_text())
        plan["title"] = "Changed"
        (self.job / "plan.json").write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "plan was modified"):
            audiobook.load_plan(self.job)

    def test_inline_flow_preserves_paragraphs_and_block_boundaries(self) -> None:
        html = b"<html><body><div>A single <em>important</em> paragraph <span>continues<br/>with a line break</span>.</div><div>Before <span>nested <em>emphasis</em></span>.<p>A real block.</p>After <b>the block</b>.</div></body></html>"
        rows, omitted = book_prepare.units(html, "chapter.xhtml", "include")
        self.assertEqual(
            [row["text"] for row in rows],
            [
                "A single important paragraph continues with a line break.",
                "Before nested emphasis.",
                "A real block.",
                "After the block.",
            ],
        )
        self.assertEqual(omitted, [])

    def test_long_paragraph_splits_without_separating_dash_anchors(self) -> None:
        for prefix in range(75, 110):
            text = (
                "Opening words. "
                + "measured " * prefix
                + "left — right "
                + "continues " * 90
                + "until the end."
            )
            pieces = book_prepare.split_unit(text)
            self.assertGreater(len(pieces), 1)
            self.assertEqual(" ".join(pieces), text.strip())
            self.assertTrue(
                all(len(piece) <= 700 and len(piece.split()) <= 100 for piece in pieces)
            )
            self.assertEqual(
                sum(len(book_prepare.sentence_dashes(piece)) for piece in pieces), 1
            )
            self.assertTrue(any("left — right" in piece for piece in pieces))
        segment = {
            "text": "Pages 10—20 show a pause – then a change - and another.",
            "sentence_dashes": [24, 40],
        }
        offsets = book_prepare.sentence_dashes(segment["text"])
        self.assertEqual(len(offsets), 2)
        segment["sentence_dashes"] = offsets
        self.assertEqual(book_prepare.pacing_text(segment).count("—"), 2)

    def test_ambiguous_join_split_is_held_but_numbers_normalize(self) -> None:
        expected = "After travelling for several hours and carefully checking the instructions, we are now here at the correct entrance to the building."
        result = audiobook.wording_check(
            expected, expected.replace("now here", "nowhere")
        )
        self.assertTrue(result["flagged"])
        self.assertTrue(result["changes"])
        self.assertFalse(
            audiobook.wording_check(
                "There were twenty four observations.", "There were 24 observations."
            )["flagged"]
        )

    def test_zero_prefixed_identifiers_preserve_each_spoken_digit(self) -> None:
        self.assertTrue(
            audiobook.wording_check("Code 007 appears here.", "Code 7 appears here.")[
                "flagged"
            ]
        )
        self.assertFalse(
            audiobook.wording_check(
                "Code 007 appears here.", "Code zero zero seven appears here."
            )["flagged"]
        )
        self.assertFalse(
            audiobook.wording_check(
                "The value is 1.05.", "The value is one point zero five."
            )["flagged"]
        )

    def test_reference_provenance_is_frozen_and_checked(self) -> None:
        self.assertEqual(
            self.plan["profile"]["reference_provenance"], "reference_provenance.json"
        )
        path = self.job / "reference_provenance.json"
        self.assertEqual(
            file_hash(path), self.plan["profile"]["reference_provenance_sha256"]
        )
        path.write_text("changed")
        with self.assertRaisesRegex(ValueError, "voice provenance"):
            audiobook.load_plan(self.job)

    def test_status_can_read_during_active_job_lease(self) -> None:
        with audiobook.job_lease(self.job):
            result = subprocess.run(
                [sys.executable, "audiobook.py", "status", "--job", str(self.job)],
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertEqual(
            json.loads(result.stdout)["counts"]["pending"], self.plan["total_segments"]
        )

    def test_packaging_uses_destination_filesystem_and_preserves_unowned_files(
        self,
    ) -> None:
        self.render_all()
        output = self.root / "separate-device"
        output.mkdir()
        unrelated = output / "audiobook.m4b"
        unrelated.write_bytes(b"another book")
        with self.assertRaisesRegex(ValueError, "unrelated files"):
            book_package.finish(self.plan, self.job, output)
        self.assertEqual(unrelated.read_bytes(), b"another book")
        destination = self.root / "new-output-device"
        original_replace = Path.replace

        def simulated_devices(source: Path, target: Path) -> Path:
            # Only cross-device renames fail; temp publication beside the destination works.
            target = Path(target)
            if source.is_relative_to(self.job) != target.is_relative_to(self.job):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return original_replace(source, target)

        with patch.object(Path, "replace", simulated_devices):
            report = book_package.finish(self.plan, self.job, destination)
        self.assertTrue(Path(report["m4b"]).is_file())

    def test_dash_requires_confident_quiet_gap_and_preserves_samples(self) -> None:
        wave = np.concatenate(
            [np.ones(2400) * 0.1, np.zeros(2400), np.ones(2400) * 0.1]
        ).astype(np.float32)
        evidence = {
            "segments": [
                {
                    "words": [
                        {"word": "left", "end": 0.1, "probability": 0.99},
                        {"word": "right", "start": 0.2, "probability": 0.99},
                    ]
                }
            ]
        }
        points = dash_insertions("left — right", evidence, wave)
        out = insert_pauses(wave, points)
        self.assertEqual(len(out), len(wave) + round(0.25 * RATE))
        self.assertTrue(np.array_equal(out[:2400], wave[:2400]))
        evidence["segments"][0]["words"][0]["probability"] = 0.1
        with self.assertRaisesRegex(ValueError, "Low-confidence"):
            dash_insertions("left — right", evidence, wave)


if __name__ == "__main__":
    unittest.main()
