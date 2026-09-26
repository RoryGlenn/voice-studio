"""Exercise speaker switching, immutable job identity, and cache recovery."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mutagen.id3 import ID3

import studio
import test_studio as studio_tests
from test_support import synthetic_profile
from voice_profiles import engine_capabilities, load_profiles, profile_defaults


def write_profile(directory: Path, voice_id: str, name: str) -> dict:
    """Create a synthetic reference/profile pair for a private test installation."""
    profile = synthetic_profile()
    profile.update(name=name, reference=f"profile/{voice_id}.wav")
    reference = directory / profile["reference"]
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_bytes(f"synthetic reference {voice_id}".encode())
    profile["reference_sha256"] = studio.sha256(reference.read_bytes())
    path = directory / (
        "voice_profile.json" if voice_id == "default" else f"voices/{voice_id}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile))
    return profile


class RegistryChecks(unittest.TestCase):
    """Keep the original default intact and reject ambiguous voice identifiers."""

    def test_profile_defaults_validate_values_and_preserve_legacy_defaults(
        self,
    ) -> None:
        profile = synthetic_profile()
        self.assertEqual(
            profile_defaults(profile),
            {"default_engine": "natural", "default_expression": 0.5},
        )
        profile.update(default_engine="expressive", default_expression=0)
        self.assertEqual(
            profile_defaults(profile),
            {"default_engine": "expressive", "default_expression": 0.0},
        )
        for changes in (
            {"default_engine": "unknown"},
            {"default_engine": []},
            {"default_expression": True},
            {"default_expression": float("nan")},
            {"default_expression": 1.5},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                profile_defaults(dict(profile, **changes))

    def test_optional_profiles_preserve_default_and_hide_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            default = write_profile(directory, "default", "Original voice")
            before = (directory / "voice_profile.json").read_bytes()
            write_profile(directory, "deep-narrator", "Deep Narrator")
            profiles = load_profiles(directory)
            self.assertEqual(profiles["default"], default)
            self.assertEqual(list(profiles), ["default", "deep-narrator"])
            self.assertEqual((directory / "voice_profile.json").read_bytes(), before)
            caps = engine_capabilities(profiles["deep-narrator"], directory, directory)
            self.assertFalse(caps["natural"]["available"])
            self.assertNotIn("path", caps["natural"])
            (directory / "voices/default.json").write_text(json.dumps(default))
            with self.assertRaisesRegex(ValueError, "duplicate voice ID"):
                load_profiles(directory)

    def test_native_capabilities_require_primary_weights_and_text_tokenizer(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("runtime.prerequisites", return_value=None),
        ):
            root = Path(temp)
            profile = write_profile(root, "default", "Example")
            model = root / profile["engines"]["natural"]["path"]
            model.mkdir(parents=True)
            for name in (
                "config.json",
                "conds.safetensors",
                "vocab.json",
                "merges.txt",
                "tokenizer_config.json",
            ):
                (model / name).write_bytes(b"synthetic presence fixture")
            self.assertFalse(
                engine_capabilities(profile, root, root)["natural"]["available"]
            )
            (model / "model.safetensors").write_bytes(b"primary weights")
            self.assertTrue(
                engine_capabilities(profile, root, root)["natural"]["available"]
            )
            (model / "merges.txt").unlink()
            self.assertFalse(
                engine_capabilities(profile, root, root)["natural"]["available"]
            )
            (model / "tokenizer.json").write_text("{}")
            self.assertTrue(
                engine_capabilities(profile, root, root)["natural"]["available"]
            )
            with patch(
                "runtime.prerequisites", return_value="Missing offline S3TokenizerV2"
            ):
                self.assertFalse(
                    engine_capabilities(profile, root, root)["natural"]["available"]
                )


class NativeCacheChecks(unittest.TestCase):
    """Use mocked native modules to detect reuse of the wrong speaker conditioning."""

    def setUp(self) -> None:
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(self.temp)
        self.a = write_profile(self.directory, "default", "A")
        self.b = write_profile(self.directory, "deep-narrator", "B")
        self.b["synthesis"]["seed"] = 71
        self.enterContext(patch.dict(os.environ))
        self.mx = types.ModuleType("mlx.core")
        self.mx.clear_cache = Mock()
        self.mx.synchronize = Mock()
        self.enterContext(patch("runtime.cuda_device_synchronize"))
        self.mx.random = types.SimpleNamespace(seed=Mock())
        mlx = types.ModuleType("mlx")
        mlx.core = self.mx
        audio = types.ModuleType("mlx_audio")
        tts = types.ModuleType("mlx_audio.tts")
        utils = types.ModuleType("mlx_audio.tts.utils")
        self.models = []

        def load_model(path: str) -> Mock:
            model = Mock()
            model.prepare_conditionals.return_value = object()
            self.models.append(model)
            return model

        self.loader = Mock(side_effect=load_model)
        utils.load_model = self.loader
        audio.tts, tts.utils = tts, utils
        self.enterContext(
            patch.dict(
                sys.modules,
                {
                    "mlx": mlx,
                    "mlx.core": self.mx,
                    "mlx_audio": audio,
                    "mlx_audio.tts": tts,
                    "mlx_audio.tts.utils": utils,
                },
            )
        )
        self.enterContext(patch("runtime.initialize", return_value=self.mx))
        self.enterContext(patch("runtime.check_tokenizer", return_value={}))
        self.enterContext(
            patch(
                "runtime.load_checked_model",
                side_effect=lambda path: self.loader(str(path)),
            )
        )
        self.engine = studio.NativeEngine(self.a, self.directory, self.directory)

    def test_a_b_a_reconditions_same_engine_and_reuses_same_voice(self) -> None:
        self.engine.prepare("natural", 0.5)
        self.engine.prepare("natural", 0.5)
        self.assertEqual(self.loader.call_count, 1)
        self.engine.select_profile(self.b)
        self.engine.prepare("natural", 0.5)
        self.mx.random.seed.assert_called_with(71)
        self.engine.select_profile(self.a)
        self.engine.prepare("natural", 0.5)
        self.assertEqual(self.loader.call_count, 3)
        expected = [self.a["reference"], self.b["reference"], self.a["reference"]]
        self.assertEqual(
            [m.prepare_conditionals.call_args.args[0] for m in self.models],
            [str(self.directory / value) for value in expected],
        )

    def test_changed_reference_is_rejected_even_when_cached(self) -> None:
        self.engine.prepare("natural", 0.5)
        reference = self.directory / self.a["reference"]
        original = reference.read_bytes()
        reference.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "reference changed"):
            self.engine.prepare("natural", 0.5)
        self.assertIsNone(self.engine.cache_identity)
        self.assertIsNone(self.engine.model)
        reference.write_bytes(original)
        self.engine.prepare("natural", 0.5)
        self.assertEqual(self.loader.call_count, 2)

    def test_failed_conditioning_recovers_and_expression_reconditions(self) -> None:
        broken = Mock()
        broken.prepare_conditionals.side_effect = RuntimeError("conditioning failed")
        self.loader.side_effect = [broken, Mock(), Mock()]
        with self.assertRaisesRegex(RuntimeError, "conditioning failed"):
            self.engine.prepare("expressive", 0.2)
        self.assertIsNone(self.engine.key)
        self.assertIsNone(self.engine.cache_identity)
        self.engine.prepare("expressive", 0.2)
        self.engine.prepare("expressive", 0.2)
        self.engine.prepare("expressive", 0.7)
        self.assertEqual(self.loader.call_count, 3)

    def test_model_revision_changes_invalidate_conditioning(self) -> None:
        self.engine.prepare("natural", 0.5)
        changed = copy.deepcopy(self.a)
        changed["engines"]["natural"]["revision"] = "different-model"
        self.engine.select_profile(changed)
        self.engine.prepare("natural", 0.5)
        self.assertEqual(self.loader.call_count, 2)


class SelectedVoiceChecks(unittest.TestCase):
    """Exercise selected-profile exports with fake synthesis and real MP3 encoding."""

    tearDown = studio_tests.RenderTests.tearDown
    wait = studio_tests.RenderTests.wait

    def setUp(self) -> None:
        studio_tests.RenderTests.setUp(self)
        self.deep = write_profile(self.directory, "deep-narrator", "Deep Narrator")
        self.deep["engines"].pop("expressive")
        (self.directory / "voices/deep-narrator.json").write_text(json.dumps(self.deep))
        self.service.profiles["deep-narrator"] = copy.deepcopy(self.deep)
        self.service.workspace = self.directory
        for key in ("natural", "expressive"):
            folder = self.directory / f"models/{key}"
            folder.mkdir(parents=True)
            (folder / "config.json").write_text("{}")
            (folder / "model.safetensors").write_bytes(b"synthetic model")
        self.service.config = self.original_config

    def test_default_legacy_request_and_selected_voice_availability(self) -> None:
        self.assertEqual(self.service.config()["default_voice_id"], "default")
        first = self.wait(self.service.submit({"text": "Original voice sample."})["id"])
        self.assertEqual(first["voice_id"], "default")
        for request in (
            {"voice_id": "unknown"},
            {"voice_id": []},
            {"voice_id": "deep-narrator", "engine": "expressive"},
        ):
            with self.assertRaises(ValueError):
                self.service.submit({"text": "Invalid selection.", **request})
        (self.directory / self.deep["reference"]).unlink()
        with self.assertRaises(ValueError):
            self.service.submit(
                {"text": "Missing reference.", "voice_id": "deep-narrator"}
            )

    def test_configured_defaults_and_explicit_request_overrides(self) -> None:
        profile = self.service.profiles["deep-narrator"]
        profile["engines"]["expressive"] = copy.deepcopy(
            self.service.profile["engines"]["expressive"]
        )
        profile.update(default_engine="expressive", default_expression=0.25)
        config = self.service.config()
        self.assertEqual(config["default_engine"], "natural")
        self.assertEqual(
            config["voices"]["deep-narrator"]["default_engine"], "expressive"
        )
        first = self.wait(
            self.service.submit(
                {
                    "text": "Use the selected voice defaults.",
                    "voice_id": "deep-narrator",
                }
            )["id"]
        )
        self.assertEqual(first["status"], "completed", first)
        self.assertEqual(first["engine"], "expressive")
        self.assertEqual(first["settings"]["expression"], 0.25)
        override = self.wait(
            self.service.submit(
                {
                    "text": "An explicit engine choice wins.",
                    "voice_id": "deep-narrator",
                    "engine": "natural",
                }
            )["id"]
        )
        self.assertEqual(override["engine"], "natural")
        expression = self.wait(
            self.service.submit(
                {
                    "text": "An explicit expression choice wins.",
                    "voice_id": "deep-narrator",
                    "expression": 0.7,
                }
            )["id"]
        )
        self.assertEqual(expression["settings"]["expression"], 0.7)

    def test_unavailable_default_does_not_silently_fall_back(self) -> None:
        profile = self.service.profiles["deep-narrator"]
        profile["engines"]["expressive"] = copy.deepcopy(
            self.service.profile["engines"]["expressive"]
        )
        profile.update(default_engine="expressive", default_expression=0.25)
        (self.directory / "models/expressive/config.json").unlink()
        with self.assertRaisesRegex(ValueError, "engine is unavailable"):
            self.service.submit(
                {
                    "text": "Do not change the requested model.",
                    "voice_id": "deep-narrator",
                }
            )
        self.assertEqual(self.service.jobs, {})
        override = self.wait(
            self.service.submit(
                {
                    "text": "Use the explicit available alternative.",
                    "voice_id": "deep-narrator",
                    "engine": "natural",
                }
            )["id"]
        )
        self.assertEqual(override["status"], "completed", override)
        self.assertEqual(override["engine"], "natural")

    def test_job_snapshot_survives_mutation_and_restart(self) -> None:
        release = threading.Event()
        self.service.pool.submit(lambda: release.wait(5))
        original = copy.deepcopy(self.deep)
        try:
            job = self.service.submit(
                {
                    "text": "This recording belongs to the deep narrator.",
                    "voice_id": "deep-narrator",
                }
            )
            self.service.profiles["deep-narrator"]["name"] = "Changed later"
            self.service.profiles["deep-narrator"]["reference_sha256"] = "f" * 64
            self.service.profiles["deep-narrator"]["engines"]["natural"]["revision"] = (
                "changed"
            )
            release.set()
            finished = self.wait(job["id"])
            self.assertEqual(finished["status"], "completed", finished)
            self.assertEqual(finished["voice_name"], original["name"])
            self.assertEqual(finished["reference_sha256"], original["reference_sha256"])
            self.assertEqual(finished["model"], original["engines"]["natural"])
            tags = ID3(self.service.renders / job["id"] / "narration.mp3")
            self.assertEqual(str(tags["TPE1"]), "Deep Narrator (AI-generated voice)")
            reopened = studio.Studio(
                self.directory, studio_tests.FakeEngine, workspace=self.directory
            )
            try:
                self.assertEqual(
                    reopened.snapshot(job["id"])["voice_name"], "Deep Narrator"
                )
            finally:
                reopened.pool.shutdown()
        finally:
            release.set()
