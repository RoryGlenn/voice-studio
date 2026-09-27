"""Verify edition isolation and the Natural demo's audio processing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
from mutagen.id3 import ID3
from test_support import synthetic_book

from tools.legacy import new_b_book
from voice_studio import render_book as book


class NaturalEditionChecks(unittest.TestCase):
    """Exercise the real plan and encoding path without running GPU inference."""

    def test_new_voice_cannot_reuse_old_plan_identity(self) -> None:
        """Freeze a synthetic new voice while conserving all original passages."""
        with synthetic_book(new_b_book):
            old = book.build_plan()
            new_b_book.configure()
            plan = book.build_plan()
            self.assertEqual(
                [[s["text"] for s in t["segments"]] for t in plan["tracks"]],
                [[s["text"] for s in t["segments"]] for t in old["tracks"]],
            )
            self.assertNotEqual(plan["identity_sha256"], old["identity_sha256"])
            self.assertEqual(
                plan["identity"]["voice_reference_sha256"], new_b_book.REFERENCE_SHA256
            )
            self.assertNotEqual(book.JOB, book.ROOT / "work/own_voice/book")
            self.assertEqual(
                {s["pause_seconds"] for t in plan["tracks"] for s in t["segments"]},
                {0.6, 0.12},
            )

    def test_natural_encoding_only_attenuates_loud_passages(self) -> None:
        """Preserve quiet samples and limit high peaks without changing timing."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            samples = np.sin(
                np.arange(2400, dtype=np.float32) * (2 * np.pi * 440 / 24000)
            )
            waves = [samples * 0.25, samples * 1.2]
            records, segments = [], []
            for index, wave in enumerate(waves):
                path = root / f"input-{index}.wav"
                sf.write(path, wave, 24000, subtype="FLOAT")
                records.append(
                    {
                        "file": str(path),
                        "identity": "test",
                        "audio_sha256": book.digest(path.read_bytes()),
                    }
                )
                segments.append({"text_sha256": str(index), "pause_seconds": 0.12})
            track = {
                "track": 1,
                "disc": 1,
                "folder": "chapter",
                "title": "Audio processing check",
                "included_sections": ["test"],
                "segments": segments,
            }
            with patch.multiple(
                book,
                JOB=root,
                OUTPUT=root / "out",
                PEAK_ONLY=True,
                ALBUM="Natural New B test",
            ):
                row = book.assemble_track(track, records)
            joined, rate = sf.read(root / "track-01.wav", dtype="float32")
            self.assertEqual(rate, 24000)
            self.assertEqual(len(joined), 2 * (2400 + 2880))
            np.testing.assert_array_equal(joined[:2400], waves[0])
            np.testing.assert_allclose(
                joined[5280:7680],
                waves[1] * (0.95 / float(np.max(np.abs(waves[1])))),
                atol=1e-7,
            )
            self.assertEqual(
                str(ID3(root / "out" / row["file"])["TALB"]), "Natural New B test"
            )
            self.assertEqual(row["full_decode"], "pass")


if __name__ == "__main__":
    unittest.main()
