"""CPU-only checks for device policy and interprocess GPU ownership."""

from __future__ import annotations

import ctypes
import gc
import os
import subprocess
import sys
import tempfile
import types
import unittest
import weakref
from pathlib import Path
from unittest.mock import Mock, patch

from voice_studio import runtime


class RuntimeChecks(unittest.TestCase):
    """Verify safety boundaries without requiring an installed GPU backend."""

    def setUp(self) -> None:
        self.enterContext(patch("voice_studio.runtime.cuda_device_synchronize"))

    def test_linux_defaults_and_no_cpu_fallback(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.dict(os.environ, {"VOICE_STUDIO_CONFIG": "/missing"}, clear=True),
        ):
            self.assertEqual(runtime.settings()["backend"], "cuda")
            self.assertEqual(runtime.settings()["memory_mib"], 4096)
            os.environ["VOICE_STUDIO_BACKEND"] = "cpu"
            with self.assertRaisesRegex(ValueError, "CPU fallback"):
                runtime.settings()

    def test_cuda_limits_and_device_requirement(self) -> None:
        mx = types.ModuleType("mlx.core")
        mx.cuda = types.SimpleNamespace(is_available=Mock(return_value=True))
        mx.gpu = "gpu"
        mx.set_default_device = Mock()
        mx.set_cache_limit = Mock()
        mx.set_memory_limit = Mock()
        mlx = types.ModuleType("mlx")
        mlx.core = mx
        with (
            patch.dict(sys.modules, {"mlx": mlx, "mlx.core": mx}),
            patch.dict(
                os.environ,
                {"VOICE_STUDIO_BACKEND": "cuda", "VOICE_STUDIO_MEMORY_MIB": "4096"},
            ),
        ):
            runtime.initialize(Path("/unused"))
            self.assertEqual(os.environ["MLX_USE_CUDA_GRAPHS"], "0")
            self.assertEqual(os.environ["MLX_CUDA_CONV_CACHE_SIZE"], "512")
            os.environ["VOICE_STUDIO_CUDA_CONV_CACHE_SIZE"] = "2048"
            runtime.initialize(Path("/unused"))
            self.assertEqual(os.environ["MLX_CUDA_CONV_CACHE_SIZE"], "2048")
            mx.set_default_device.assert_called_with("gpu")
            mx.set_cache_limit.assert_called_with(0)
            mx.set_memory_limit.assert_called_with(4096 * 1024**2)
            mx.cuda.is_available.return_value = False
            with self.assertRaisesRegex(RuntimeError, "refusing CPU fallback"):
                runtime.initialize(Path("/unused"))

    def test_cuda_convolution_cache_override(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VOICE_STUDIO_CONFIG": "/missing",
                "VOICE_STUDIO_CUDA_CONV_CACHE_SIZE": "2048",
            },
            clear=True,
        ):
            self.assertEqual(runtime.settings()["cuda_conv_cache_size"], 2048)
            self.assertEqual(
                runtime.identity()["settings"]["cuda_conv_cache_size"], 2048
            )
            os.environ["VOICE_STUDIO_CUDA_CONV_CACHE_SIZE"] = "0"
            with self.assertRaisesRegex(
                ValueError, "cuda_conv_cache_size must be positive"
            ):
                runtime.settings()

    def test_lease_excludes_other_process_and_releases_after_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                os.environ, {"VOICE_STUDIO_GPU_LOCK": str(Path(temp) / "gpu.lock")}
            ),
        ):
            command = [
                sys.executable,
                "-c",
                "from voice_studio import runtime;\nwith runtime.gpu_lease(): pass",
            ]
            with self.assertRaisesRegex(ValueError, "interrupted"):
                with runtime.gpu_lease(), runtime.gpu_lease():
                    child = subprocess.run(command, capture_output=True, text=True)
                    self.assertNotEqual(child.returncode, 0)
                    self.assertIn("GPU is busy", child.stderr)
                    raise ValueError("interrupted")
            subprocess.run(command, check=True, capture_output=True)

    def test_historical_cold_and_reloaded_generation_seeds_after_loading(self) -> None:
        import numpy as np

        from voice_studio import render_book

        events = []
        cursor = {"key": 0}

        def seed(value: int) -> None:
            events.append("seed")
            cursor["key"] = value

        mx = types.ModuleType("mlx.core")
        # The module state attribute is an opaque live sentinel, as in actual MLX.
        mx.random = types.SimpleNamespace(state=object(), seed=seed)
        mx.clear_cache = Mock()
        mx.synchronize = Mock()
        mlx = types.ModuleType("mlx")
        mlx.core = mx

        class Model:
            def prepare_conditionals(self, reference: str) -> None:
                events.append("condition")
                cursor["key"] = 123

            def generate(self, **kwargs: object) -> object:
                events.append("generate")
                wave = np.full(24000, cursor["key"] / 1e9, dtype=np.float32)
                yield types.SimpleNamespace(audio=wave, sample_rate=24000)

        def load(path: Path) -> Model:
            events.append("load")
            cursor["key"] = 999
            return Model()

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(sys.modules, {"mlx": mlx, "mlx.core": mx}),
            patch("voice_studio.runtime.initialize", return_value=mx),
            patch("voice_studio.runtime.check_tokenizer"),
            patch("voice_studio.runtime.load_checked_model", side_effect=load),
            patch(
                "voice_studio.render_book.recognize",
                return_value=("Test passage.", 0.0),
            ),
            patch("voice_studio.render_book.compare", return_value={"flagged": False}),
        ):
            model = runtime.ReloadableModel(
                Path("model"), Path("reference"), Path("root")
            )
            hashes = []
            for name in ("cold", "warm", "reloaded"):
                with patch("voice_studio.render_book.JOB", Path(temp) / name):
                    row = render_book.render_segment(
                        model,
                        {"track": 1},
                        {
                            "number": 1,
                            "text": "Test passage.",
                            "text_sha256": render_book.digest("Test passage."),
                        },
                        "identity",
                    )
                    hashes.append(row["audio_sha256"])
                if name == "warm":
                    model.release()
            self.assertEqual(len(set(hashes)), 1)
            self.assertEqual(
                events,
                [
                    "load",
                    "condition",
                    "seed",
                    "generate",
                    "seed",
                    "generate",
                    "load",
                    "condition",
                    "seed",
                    "generate",
                ],
            )
            model.release()

    def test_exception_traceback_does_not_retain_model_after_unlock(self) -> None:
        class Payload:
            pass

        reference = []

        @runtime.gpu_operation
        def failing() -> None:
            model = Payload()
            reference.append(weakref.ref(model))
            raise ValueError("inference failed")

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                os.environ, {"VOICE_STUDIO_GPU_LOCK": str(Path(temp) / "gpu.lock")}
            ),
        ):
            try:
                failing()
            except ValueError as exc:
                self.assertIn("inference failed", str(exc))
                gc.collect()
                self.assertIsNone(reference[0]())
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from voice_studio import runtime;\nwith runtime.gpu_lease(): pass",
                    ],
                    check=True,
                    capture_output=True,
                )

    def test_chained_exception_frames_are_released_before_unlock(self) -> None:
        class Payload:
            pass

        references = []

        def inference() -> None:
            model = Payload()
            references.append(weakref.ref(model))
            raise ValueError("device failure")

        @runtime.gpu_operation
        def wrapped() -> None:
            try:
                inference()
            except ValueError as cause:
                raise RuntimeError("generation failed") from cause

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                os.environ, {"VOICE_STUDIO_GPU_LOCK": str(Path(temp) / "gpu.lock")}
            ),
        ):
            try:
                wrapped()
            except RuntimeError as exc:
                self.assertIn("device failure", str(exc.__cause__))
                gc.collect()
                self.assertIsNone(references[0]())
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from voice_studio import runtime;\nwith runtime.gpu_lease(): pass",
                    ],
                    check=True,
                    capture_output=True,
                )
                # Exception chains can contain cycles; cleanup must remain bounded.
                exc.__cause__.__context__ = exc
                runtime.clear_exception_frames(exc)

    def test_cleanup_failure_does_not_bypass_next_process_lock(self) -> None:
        class Broken:
            def release(self) -> None:
                raise ValueError("release failure")

        good = types.SimpleNamespace(release=Mock())
        broken = Broken()
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                os.environ, {"VOICE_STUDIO_GPU_LOCK": str(Path(temp) / "gpu.lock")}
            ),
            patch("voice_studio.runtime._MODELS", [broken, good]),
        ):
            with self.assertRaisesRegex(RuntimeError, "release failure"):
                with runtime.gpu_lease():
                    pass
            good.release.assert_called_once()
            self.assertEqual(runtime._LOCAL.depth, 0)
            with patch("voice_studio.runtime._MODELS", []):
                with runtime.gpu_lease():
                    child = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "from voice_studio import runtime;\nwith runtime.gpu_lease(): pass",
                        ],
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(child.returncode, 0)
                    self.assertIn("GPU is busy", child.stderr)
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from voice_studio import runtime;\nwith runtime.gpu_lease(): pass",
                    ],
                    check=True,
                    capture_output=True,
                )

    def test_suppressed_tokenizer_loader_failure_is_fatal(self) -> None:
        import logging

        fake = types.SimpleNamespace(tokenizer=object(), _s3tokenizer=object())
        modules = {
            "mlx_audio": types.ModuleType("mlx_audio"),
            "mlx_audio.tts": types.ModuleType("mlx_audio.tts"),
            "mlx_audio.tts.utils": types.ModuleType("mlx_audio.tts.utils"),
        }
        loader = Mock(return_value=fake)
        modules["mlx_audio.tts.utils"].load_model = loader
        with patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, "speech tokenizer"):
                runtime.load_checked_model(Path("model"))

            def loaded(path: str) -> object:
                logging.getLogger(
                    "mlx_audio.tts.models.chatterbox_turbo.chatterbox_turbo"
                ).info("Loaded S3 speech tokenizer weights")
                return fake

            loader.side_effect = loaded
            self.assertIs(runtime.load_checked_model(Path("model")), fake)
            fake.tokenizer = None
            with self.assertRaisesRegex(RuntimeError, "text tokenizer"):
                runtime.load_checked_model(Path("model"))

    def test_availability_requires_native_audio_and_offline_tokenizer(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("voice_studio.runtime.initialize") as initialize,
        ):
            root = Path(temp)
            with patch(
                "importlib.util.find_spec",
                side_effect=lambda name: None if name == "mlx_audio" else object(),
            ):
                ready, error = runtime.availability(root)
                self.assertFalse(ready)
                self.assertIn("mlx-audio", error)
                initialize.assert_not_called()
            with patch("importlib.util.find_spec", return_value=object()):
                ready, error = runtime.availability(root)
                self.assertFalse(ready)
                self.assertIn("S3TokenizerV2", error)
                cache = (
                    root
                    / "work/hf-narration-cache/hub/models--mlx-community--S3TokenizerV2"
                )
                weights = (
                    cache
                    / "snapshots"
                    / runtime.TOKENIZER_REVISION
                    / "model.safetensors"
                )
                weights.parent.mkdir(parents=True)
                weights.write_bytes(b"only presence is checked by availability")
                (cache / "refs").mkdir()
                (cache / "refs/main").write_text("wrong revision")
                self.assertFalse(runtime.availability(root)[0])
                (cache / "refs/main").write_text(runtime.TOKENIZER_REVISION)
                self.assertTrue(runtime.availability(root)[0])
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    runtime.check_tokenizer(root)

    def test_missing_tokenizer_fails_before_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "Missing offline S3TokenizerV2"):
                runtime.check_tokenizer(Path(temp))

    def test_portable_binary_failure(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch("platform.system", return_value="Linux"),
        ):
            with self.assertRaisesRegex(RuntimeError, "missing from PATH"):
                runtime.binary("ffmpeg")


class CudaCleanupChecks(unittest.TestCase):
    """Check native cleanup ordering and CUDA failures without accessing a real GPU."""

    def test_cuda_library_is_pinned_cached_and_has_explicit_signatures(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "libcudart.so.12"
            path.write_bytes(b"library path fixture")
            distribution = Mock()
            distribution.locate_file.return_value = path
            library = Mock()
            runtime._cuda_runtime_library.cache_clear()
            try:
                with (
                    patch(
                        "importlib.metadata.distribution", return_value=distribution
                    ) as lookup,
                    patch("ctypes.CDLL", return_value=library) as load,
                ):
                    self.assertIs(runtime._cuda_runtime_library(), library)
                    self.assertIs(runtime._cuda_runtime_library(), library)
                lookup.assert_called_once_with("nvidia-cuda-runtime-cu12")
                distribution.locate_file.assert_called_once_with(
                    "nvidia/cuda_runtime/lib/libcudart.so.12"
                )
                load.assert_called_once_with(str(path))
                self.assertEqual(library.cudaDeviceSynchronize.argtypes, [])
                self.assertIs(library.cudaDeviceSynchronize.restype, ctypes.c_int)
                self.assertEqual(library.cudaGetErrorString.argtypes, [ctypes.c_int])
                self.assertIs(library.cudaGetErrorString.restype, ctypes.c_char_p)
            finally:
                runtime._cuda_runtime_library.cache_clear()

    def test_cuda_error_is_explicit_and_metal_never_loads_cuda(self) -> None:
        library = Mock()
        library.cudaDeviceSynchronize.return_value = 700
        library.cudaGetErrorString.return_value = b"illegal memory access"
        with (
            patch("voice_studio.runtime.settings", return_value={"backend": "cuda"}),
            patch("voice_studio.runtime._cuda_runtime_library", return_value=library),
        ):
            with self.assertRaisesRegex(
                RuntimeError, r"failed \(700\): illegal memory access"
            ):
                runtime.cuda_device_synchronize()
        with (
            patch("voice_studio.runtime.settings", return_value={"backend": "metal"}),
            patch("voice_studio.runtime._cuda_runtime_library") as load,
        ):
            runtime.cuda_device_synchronize()
            load.assert_not_called()

    def test_device_synchronization_runs_after_releases_before_unlock(self) -> None:
        events = []
        mx = types.SimpleNamespace(
            synchronize=lambda: events.append("mlx"),
            clear_cache=lambda: events.append("cache"),
        )
        model = types.SimpleNamespace(release=lambda: events.append("release"))

        def device_sync() -> None:
            events.append("cuda")
            self.assertEqual(runtime._LOCAL.depth, 1)
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from voice_studio import runtime;\nwith runtime.gpu_lease(): pass",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(child.returncode, 0)
            self.assertIn("GPU is busy", child.stderr)

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                os.environ, {"VOICE_STUDIO_GPU_LOCK": str(Path(temp) / "gpu.lock")}
            ),
            patch.dict(sys.modules, {"mlx.core": mx}),
            patch("voice_studio.runtime._MODELS", [model]),
            patch(
                "voice_studio.runtime.gc.collect",
                side_effect=lambda: events.append("gc"),
            ),
            patch(
                "voice_studio.runtime.cuda_device_synchronize", side_effect=device_sync
            ),
        ):
            with runtime.gpu_lease():
                pass
        self.assertEqual(events, ["mlx", "release", "mlx", "gc", "cache", "cuda"])
        self.assertEqual(runtime._LOCAL.depth, 0)

    def test_device_sync_failure_preserves_future_lock_exclusion(self) -> None:
        mx = types.SimpleNamespace(synchronize=Mock(), clear_cache=Mock())
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                os.environ, {"VOICE_STUDIO_GPU_LOCK": str(Path(temp) / "gpu.lock")}
            ),
            patch.dict(sys.modules, {"mlx.core": mx}),
            patch("voice_studio.runtime._MODELS", []),
        ):
            with patch(
                "voice_studio.runtime.cuda_device_synchronize",
                side_effect=RuntimeError("CUDA device synchronization failed"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "CUDA device synchronization failed"
                ):
                    with runtime.gpu_lease():
                        pass
            self.assertEqual(runtime._LOCAL.depth, 0)
            with patch("voice_studio.runtime.cuda_device_synchronize"):
                with runtime.gpu_lease():
                    child = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "from voice_studio import runtime;\nwith runtime.gpu_lease(): pass",
                        ],
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(child.returncode, 0)
                    self.assertIn("GPU is busy", child.stderr)


if __name__ == "__main__":
    unittest.main()
