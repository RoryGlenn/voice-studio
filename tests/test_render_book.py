"""Behavior checks for lossless book chunking and discrepancy detection."""

import json
import tempfile
import unittest
from pathlib import Path

from test_support import synthetic_book

from voice_studio.render_book import (
    build_plan,
    compare,
    composition_digest,
    lexical,
    save_checkpoint,
    save_json,
    split_long,
)


class RenderingChecks(unittest.TestCase):
    """Check the failure modes that can lose or silently alter narrated content."""

    def test_oversized_sentence_conserves_words(self) -> None:
        text = (
            "A long sentence, "
            + "precisely numbered ideas and examples " * 100
            + "ends here."
        )
        pieces = split_long(text)
        self.assertGreater(len(pieces), 1)
        self.assertEqual(lexical(" ".join(pieces)), lexical(text))
        self.assertTrue(all(len(p) <= 400 and len(p.split()) <= 60 for p in pieces))

    def test_missing_parenthetical_tail_is_flagged(self) -> None:
        self.assertTrue(
            compare(
                "Transfer nutrients into the bloodstream (another system), while discarding unusable wastes.",
                "Transfer nutrients into the bloodstream.",
            )["flagged"]
        )

    def test_repeated_phrase_is_flagged(self) -> None:
        self.assertTrue(
            compare(
                "The system stores water and releases it.",
                "The system stores water and releases it. The system stores water and releases it.",
            )["flagged"]
        )
        self.assertTrue(
            compare(
                "The system stores water and releases it.",
                "The system stores water and releases it. Luck.",
            )["flagged"]
        )
        self.assertTrue(
            compare(
                "The system stores water and releases it.",
                "The system stores water random words and releases it.",
            )["flagged"]
        )

    def test_number_change_is_flagged(self) -> None:
        self.assertTrue(
            compare(
                "The temperature is eighteen degrees Celsius.",
                "The temperature is eighty degrees Celsius.",
            )["flagged"]
        )
        self.assertTrue(
            compare(
                "The investment keeps up with depreciation.",
                "The investment keeps up with appreciation.",
            )["flagged"]
        )
        self.assertTrue(
            compare(
                "An increase in the stock lowers the outflow.",
                "An increase in the stock lowers the inflow.",
            )["flagged"]
        )
        self.assertTrue(
            compare(
                "A real population would decline.", "A real population would grow."
            )["flagged"]
        )

    def test_negation_short_heading_and_inserted_number_are_flagged(self) -> None:
        self.assertTrue(
            compare(
                "The reservoir does not release water when the gate is closed.",
                "The reservoir does release water when the gate is closed.",
            )["flagged"]
        )
        self.assertTrue(
            compare(
                "Feedback Loops. The system stores water and releases water throughout the year.",
                "The system stores water and releases water throughout the year.",
            )["flagged"]
        )
        self.assertTrue(
            compare(
                "The system stores water and releases water throughout the year.",
                "The system stores twenty water and releases water throughout the year.",
            )["flagged"]
        )

    def test_number_format_and_parentheses_do_not_drop_words(self) -> None:
        self.assertFalse(
            compare(
                "Twenty-five workers (another team) arrived.",
                "25 workers another team arrived.",
            )["flagged"]
        )
        self.assertFalse(
            compare("A ratio of 1:3.", "A ratio of one to three.")["flagged"]
        )
        self.assertTrue(
            compare("A ratio of 1:3.", "A ratio of one hundred thirty.")["flagged"]
        )

    def test_negative_one_keeps_its_sign_without_altering_date_ranges(self) -> None:
        expected = "The utility shared by the entire group is only a fraction of –1."
        self.assertFalse(
            compare(expected, expected.replace("–1", "negative one"))["flagged"]
        )
        self.assertFalse(
            compare(expected, expected.replace("–1", "minus one"))["flagged"]
        )
        self.assertTrue(
            compare(expected, expected.replace("–1", "one"))["numeric_discrepancy"]
        )
        self.assertTrue(
            compare(expected, expected.replace("–1", "positive one"))["flagged"]
        )
        self.assertFalse(compare("1941–2001", "1941 2001")["flagged"])

    def test_fraction_and_compound_spacing_preserve_spoken_values(self) -> None:
        expected = "In this nonlinear system, twice the push could produce one-sixth the response."
        self.assertFalse(
            compare(expected, expected.replace("one-sixth the", "1 6th"))["flagged"]
        )
        self.assertTrue(
            compare(expected, expected.replace("one-sixth", "one-sixteenth"))["flagged"]
        )
        for expected, actual in (
            (
                "The reservoir outflows are measured every day.",
                "The reservoir out flows are measured every day.",
            ),
            (
                "A onetime change affected the whole system.",
                "A one time change affected the whole system.",
            ),
            (
                "Every one of the readings is recorded.",
                "Everyone of the readings is recorded.",
            ),
        ):
            continuation = (
                " The measurements help explain the behavior of the system over time."
            )
            self.assertFalse(
                compare(expected + continuation, actual + continuation)["flagged"]
            )

    def test_ellipsis_is_not_treated_as_a_missing_spoken_word(self) -> None:
        self.assertFalse(
            compare(
                "Each thought he knew something, because he could feel a part....",
                "Each thought he knew something because he could feel a part.",
            )["flagged"]
        )
        self.assertTrue(
            compare("The delay is 1.5 years.", "The delay is 15 years.")["flagged"]
        )

    def test_cannot_and_cant_are_equivalent_but_can_is_not(self) -> None:
        self.assertFalse(
            compare("Forests cannot grow overnight.", "Forests can't grow overnight.")[
                "flagged"
            ]
        )
        self.assertTrue(
            compare("Forests cannot grow overnight.", "Forests can grow overnight.")[
                "flagged"
            ]
        )

    def test_spelled_initialisms_match_without_accepting_wrong_letters(self) -> None:
        self.assertFalse(
            compare("People with H I V and AIDS.", "People with HIV and AIDS.")[
                "flagged"
            ]
        )
        self.assertTrue(
            compare("People with H I V and AIDS.", "People with H I T and AIDS.")[
                "flagged"
            ]
        )

    def test_strict_review_retains_targeted_minor_word_corrections(self) -> None:
        self.assertTrue(
            compare(
                "The world population depends on these variables.",
                "The rogue population depends on these variables.",
                strict=True,
            )["flagged"]
        )
        self.assertFalse(
            compare(
                "The nonphysical feedback system operates consistently across different physical settings and scales.",
                "The non physical feedback system operates consistently across different physical settings and scales.",
                strict=True,
            )["flagged"]
        )

    def test_stale_helper_cannot_erase_new_review_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            original = {"status": "verified", "strict_review": False}
            new_hold = {"status": "needs_review", "strict_review": True}
            save_json(path, original)
            save_checkpoint(path, original, new_hold)
            with self.assertRaisesRegex(RuntimeError, "Checkpoint changed"):
                save_checkpoint(path, original, original)
            self.assertEqual(json.loads(path.read_text()), new_hold)

    def test_repair_hints_and_pauses_keep_source_word_order(self) -> None:
        import ast
        import re

        import spacy

        source = (
            Path(__file__).resolve().parents[1] / "tools/legacy/repair_book_passages.py"
        )
        function = next(
            node
            for node in ast.parse(source.read_text()).body
            if isinstance(node, ast.FunctionDef) and node.name == "repair_chunks"
        )
        namespace = {
            "re": re,
            "spacy": spacy,
            "compare": compare,
            "split_long": split_long,
        }
        exec(
            compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"),
            namespace,
        )
        samples = (
            "Paradigms explain why the group watches the same measurements every day.",
            "The village redraws ethnic boundaries, boundaries between public and private responsibility.",
            "Repeated payments make the rich richer and the poor poorer as time passes.",
            "Two observers record the water level, compare their notes, and revise their predictions.",
        )
        for text in samples:
            chunks = namespace["repair_chunks"](text, shorter=True)
            spelled = " ".join(chunks).replace("para-dimes", "paradigms")
            self.assertEqual(lexical(text), lexical(spelled))
        text = "Calvin: OK. Hobbes: Event-event analysis describes the pattern."
        for shorter in (False, True):
            spoken = " ".join(namespace["repair_chunks"](text, shorter=shorter))
            self.assertIn("OK", spoken)
            self.assertIn("event. Event", spoken)
            self.assertEqual(lexical(text), lexical(spoken))
            self.assertFalse(compare(text, spoken)["changes"])
        text = "Sylvia Nasar, author. Another of Jay Forrester's famous systems sayings follows."
        for shorter in (False, True):
            spoken = " ".join(namespace["repair_chunks"](text, shorter=shorter))
            self.assertIn("Sylvia Nasar.", spoken)
            self.assertIn("famous systems, sayings", spoken)
            self.assertEqual(lexical(text), lexical(spoken))
            self.assertFalse(compare(text, spoken)["changes"])

    def test_renderer_retry_preserves_strict_review(self) -> None:
        import ast
        from types import SimpleNamespace
        from unittest.mock import patch

        import numpy as np

        from voice_studio import render_book as rb

        expected = (
            "The cooling system holds hot water inside a separate insulated "
            "container for several hours before releasing it."
        )
        recognized = expected.replace(" hot", "")
        self.assertTrue(rb.compare(expected, recognized, strict=True)["flagged"])
        self.assertFalse(rb.compare(expected, recognized)["flagged"])
        original = {
            "identity": "same",
            "text_sha256": rb.digest(expected),
            "text": expected,
            "file": "/mock/original.wav",
            "status": "needs_review",
            "strict_review": True,
        }
        segment = {"number": 1, "text": expected, "text_sha256": rb.digest(expected)}
        function = next(
            node
            for node in ast.parse(Path(rb.__file__).read_text()).body
            if isinstance(node, ast.FunctionDef) and node.name == "render_segment"
        )
        # Exercise the real retry logic without loading a GPU runtime or model.
        function.body = [
            node
            for node in function.body
            if not isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        namespace = dict(vars(rb))
        namespace.update(
            {
                "JOB": Path("/mock"),
                "mx": SimpleNamespace(random=SimpleNamespace(seed=lambda seed: None)),
                "recognize": lambda *args: (recognized, 0.0),
                "save_json": lambda *args: None,
            }
        )
        exec(
            compile(ast.Module(body=[function], type_ignores=[]), rb.__file__, "exec"),
            namespace,
        )
        model = SimpleNamespace(
            generate=lambda **kwargs: iter(
                [
                    SimpleNamespace(
                        audio=np.ones(5 * 24000, dtype=np.float32), sample_rate=24000
                    )
                ]
            )
        )
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "mkdir"),
            patch.object(Path, "read_text", return_value=json.dumps(original)),
            patch.object(Path, "read_bytes", return_value=b"wave"),
            patch.object(rb.sf, "write"),
        ):
            result = namespace["render_segment"](model, {"track": 1}, segment, "same")
        self.assertTrue(result.get("strict_review"))
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue((result["strong_check"] or result["tiny_check"])["flagged"])

    def test_synthetic_book_plan_conserves_every_track(self) -> None:
        with synthetic_book():
            plan = build_plan()
        self.assertEqual([t["track"] for t in plan["tracks"]], [1, 2, 3])
        segments = [s for t in plan["tracks"] for s in t["segments"]]
        self.assertTrue(
            all(8 <= s["word_count"] <= 65 and len(s["text"]) <= 420 for s in segments)
        )
        self.assertEqual(plan["total_segments"], len(segments))
        self.assertGreater(len(segments), 3)

    def test_composition_detects_repaired_audio_order_and_pause_changes(self) -> None:
        track = {
            "segments": [
                {"text_sha256": "one", "pause_seconds": 0.4},
                {"text_sha256": "two", "pause_seconds": 0.16},
            ]
        }
        records = [{"audio_sha256": "original-one"}, {"audio_sha256": "original-two"}]
        before = composition_digest(track, records)
        self.assertNotEqual(before, composition_digest(track, list(reversed(records))))
        self.assertNotEqual(
            before,
            composition_digest(track, [{"audio_sha256": "repaired-one"}, records[1]]),
        )
        track["segments"][0]["pause_seconds"] = 0.16
        self.assertNotEqual(before, composition_digest(track, records))


if __name__ == "__main__":
    unittest.main()
