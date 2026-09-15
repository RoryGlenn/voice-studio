"""Reject stale comparison inputs without running speech inference."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import precision_comparison as comparison


class InputChecks(unittest.TestCase):
    """Verify profile and model changes cannot silently reuse old auditions."""

    def test_lock_conflict_preserves_running_status(self) -> None:
        """A second invocation cannot overwrite the active owner's progress."""
        with tempfile.TemporaryDirectory() as temporary:
            job = Path(temporary)
            original = {"state": "rendering", "model": "B", "passage": 12}
            comparison.save(job / "status.json", original)
            with (
                patch.object(comparison, "JOB", job),
                patch.object(comparison, "render", side_effect=BlockingIOError),
                patch("sys.argv", ["comparison", "render", "--model", "A"]),
            ):
                with self.assertRaises(BlockingIOError):
                    comparison.main()
            self.assertEqual(json.loads((job / "status.json").read_text()), original)

    def test_changed_settings_and_weights_reject_reuse(self) -> None:
        """Exercise unchanged, modified-profile, and modified-weight cases."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, app, model = root / "job", root / "app", root / "model"
            for path in (job, app, model):
                path.mkdir()
            plan = {
                "models": {"A": {"path": "model"}},
                "settings": {"temperature": 0.8, "repetition_penalty": 1.2},
            }
            profile = {"synthesis": dict(plan["settings"])}
            comparison.save(job / "plan.json", plan)
            comparison.save(job / "voice_profile.json", profile)
            (app / "studio.py").write_text("original engine")
            (model / "config.json").write_text("config")
            (model / "model.safetensors").write_bytes(b"original weights")
            frozen = {
                "plan_sha256": comparison.digest(job / "plan.json"),
                "profile_sha256": comparison.digest(job / "voice_profile.json"),
                "studio_sha256": comparison.digest(app / "studio.py"),
                "models": {
                    "A": {
                        name: comparison.digest(model / name)
                        for name in ("config.json", "model.safetensors")
                    }
                },
            }
            comparison.save(job / "frozen_inputs.json", frozen)
            with patch.multiple(comparison, ROOT=root, JOB=job, APP=app):
                self.assertEqual(comparison.verify_inputs("A"), frozen["models"]["A"])
                profile["synthesis"]["temperature"] = 0.6
                comparison.save(job / "voice_profile.json", profile)
                with self.assertRaisesRegex(
                    ValueError, "Frozen comparison input changed"
                ):
                    comparison.verify_inputs("A")
                profile["synthesis"]["temperature"] = 0.8
                comparison.save(job / "voice_profile.json", profile)
                (model / "model.safetensors").write_bytes(b"different weights")
                with self.assertRaisesRegex(ValueError, "Frozen model file changed"):
                    comparison.verify_inputs("A")


if __name__ == "__main__":
    unittest.main()
