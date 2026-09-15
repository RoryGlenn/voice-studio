"""Check approved model identity and isolation from earlier narration caches."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import higher_precision_book as edition
import render_book as book
from test_support import synthetic_book, synthetic_profile


class HigherPrecisionEditionChecks(unittest.TestCase):
    """Validate the new edition without loading a TTS model or rendering speech."""

    def test_plan_preserves_words_and_isolates_model_identity(self) -> None:
        """Require conserved synthetic passages and a hash-bound model identity."""
        with synthetic_book(edition):
            old = book.build_plan()
            edition.configure()
            plan = book.build_plan()
            self.assertNotEqual(plan["identity_sha256"], old["identity_sha256"])
            self.assertEqual(
                [[s["text"] for s in t["segments"]] for t in plan["tracks"]],
                [[s["text"] for s in t["segments"]] for t in old["tracks"]],
            )
            self.assertEqual(
                plan["identity"]["model_revision"], edition.MODEL["revision"]
            )
            self.assertEqual(
                plan["identity"]["model_files_sha256"], edition.MODEL_FILES
            )
            self.assertEqual(
                plan["identity"]["voice_reference_sha256"], edition.REFERENCE_SHA256
            )

    def test_changed_model_bytes_are_rejected_before_cache_access(self) -> None:
        """Fail closed if either pinned model file no longer matches."""
        for changed_name in edition.MODEL_FILES:
            with self.subTest(file=changed_name):

                def fingerprint(path: Path) -> str:
                    return (
                        "changed"
                        if path.name == changed_name
                        else edition.MODEL_FILES[path.name]
                    )

                with patch.object(edition, "file_digest", side_effect=fingerprint):
                    with self.assertRaisesRegex(
                        ValueError, "Approved model file changed"
                    ):
                        edition.configure()

    def test_old_voice_profile_is_rejected(self) -> None:
        """An older model cannot silently replace the human-approved selection."""
        profile = synthetic_profile()
        with self.assertRaisesRegex(ValueError, "Natural model differs"):
            edition.validate_profile(profile)

    def test_old_checkpoint_is_rejected_without_generation(self) -> None:
        """A matching passage filename is insufficient to reuse a 4-bit take."""
        with tempfile.TemporaryDirectory() as temporary:
            job = Path(temporary)
            folder = job / "segments/01"
            folder.mkdir(parents=True)
            segment = {"number": 1, "text_sha256": "a" * 64}
            record = {
                "file": str(folder / "old.wav"),
                "identity": "old-4bit-identity",
                "text_sha256": segment["text_sha256"],
            }
            (folder / "0001-aaaaaaaaaa.json").write_text(json.dumps(record))
            model = Mock()
            with patch.object(book, "JOB", job):
                with self.assertRaisesRegex(
                    ValueError, "Checkpoint provenance mismatch"
                ):
                    book.render_segment(
                        model, {"track": 1}, segment, "new-unquantized-identity"
                    )
            model.generate.assert_not_called()

    def test_repair_cannot_select_an_external_inventory(self) -> None:
        """Reject both assignment syntax and argparse abbreviations before dispatch."""
        variants = (
            ["--files", "/older/checkpoint.json"],
            ["--files=/older/checkpoint.json"],
            ["--file=/older/checkpoint.json"],
            ["--f", "/older/checkpoint.json"],
        )
        for arguments in variants:
            with self.subTest(arguments=arguments):
                with (
                    patch.object(
                        sys, "argv", ["higher_precision_book.py", "repair", *arguments]
                    ),
                    patch.object(edition, "configure"),
                    patch.object(edition.importlib, "import_module") as dispatch,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(SystemExit) as error:
                        edition.main()
                    self.assertEqual(error.exception.code, 2)
                    dispatch.assert_not_called()
        edition.validate_repair_args(["--shorter", "--seed-offset=500000"])


if __name__ == "__main__":
    unittest.main()
