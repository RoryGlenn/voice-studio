"""Explicit MLX device setup, offline asset checks, and a shared GPU lease."""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import gc
import importlib.metadata
import importlib.util
import json
import logging
import os
import platform
import shutil
import sys
import threading
import tomllib
import traceback
import weakref
from collections.abc import Iterator
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

from voice_studio.project_paths import REPOSITORY, workspace_root

TOKENIZER_REVISION = "e0c9886f0e1c35ae85b1f27277416fb19fc72bec"
TOKENIZER_SHA256 = "928726bc1f206a613d36b8f49e297eae9c5593a21bf9b92ddfe2c23f85eb92cc"
_LOCK = threading.RLock()
_LOCAL = threading.local()
_MODELS: weakref.WeakSet[Any] = weakref.WeakSet()
F = TypeVar("F", bound=Callable[..., Any])


def settings() -> dict[str, Any]:
    """Return explicit device settings with conservative Linux defaults."""
    path = Path(
        os.environ.get("VOICE_STUDIO_CONFIG", REPOSITORY / "local.toml")
    ).expanduser()
    config = tomllib.loads(path.read_text()) if path.is_file() else {}
    values = config.get("runtime", {})
    backend = os.environ.get(
        "VOICE_STUDIO_BACKEND",
        values.get("backend", "cuda" if platform.system() == "Linux" else "metal"),
    )
    if backend not in {"cuda", "metal"}:
        raise ValueError(
            "runtime.backend must be cuda or metal; CPU fallback is not supported"
        )
    memory = int(
        os.environ.get("VOICE_STUDIO_MEMORY_MIB", values.get("memory_mib", 4096))
    )
    if memory <= 0:
        raise ValueError("runtime.memory_mib must be positive")
    conv_cache = int(
        os.environ.get(
            "VOICE_STUDIO_CUDA_CONV_CACHE_SIZE",
            values.get("cuda_conv_cache_size", 512),
        )
    )
    if conv_cache <= 0:
        raise ValueError("runtime.cuda_conv_cache_size must be positive")
    return {
        "backend": backend,
        "memory_mib": memory,
        "cuda_graphs": False,
        "cache_mib": 0,
        "cuda_conv_cache_size": conv_cache,
    }


def binary(name: str) -> str:
    """Resolve an executable from PATH and fail without platform-specific guesses."""
    path = shutil.which(name)
    if not path and platform.system() == "Darwin":
        path = next(
            (
                str(base / name)
                for base in (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
                if (base / name).is_file() and os.access(base / name, os.X_OK)
            ),
            None,
        )
    if not path:
        raise RuntimeError(
            f"{name} is missing from PATH. Install FFmpeg and ffprobe before rendering."
        )
    return path


def identity() -> dict[str, Any]:
    """Describe the runtime without importing or allocating GPU libraries."""
    versions: dict[str, str | None] = {}
    for name in (
        "mlx",
        "mlx-cuda-12",
        "nvidia-cuda-runtime-cu12",
        "nvidia-cuda-nvcc-cu12",
        "nvidia-cuda-cccl-cu12",
        "mlx-audio",
        "mlx-whisper",
        "numpy",
        "torch",
        "transformers",
    ):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "platform": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "settings": settings(),
        "packages": versions,
    }


def initialize(root: Path | None = None) -> Any:
    """Configure MLX before import, require the selected GPU, and set its limits."""
    root = root or workspace_root()
    options = settings()
    os.environ["HF_HOME"] = str(root / "work/hf-narration-cache")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    if options["backend"] == "cuda":
        os.environ["MLX_USE_CUDA_GRAPHS"] = "0"
        os.environ["MLX_CUDA_CONV_CACHE_SIZE"] = str(options["cuda_conv_cache_size"])
    try:
        import mlx.core as mx
    except ImportError as exc:
        raise RuntimeError(
            "MLX inference dependencies are missing. Install the native (Mac) or ubuntu extra."
        ) from exc
    device = getattr(mx, options["backend"], None)
    if device is None or not device.is_available():
        raise RuntimeError(
            f"MLX {options['backend']} is unavailable; refusing CPU fallback. Run python run.py doctor."
        )
    mx.set_default_device(mx.gpu)
    if options["backend"] == "cuda":
        mx.set_cache_limit(0)
        # MLX documents this as a soft allocator guideline, not a hard VRAM cap.
        mx.set_memory_limit(options["memory_mib"] * 1024**2)
    return mx


def tokenizer_asset(root: Path | None = None) -> Path:
    """Check the offline tokenizer layout and main reference without hashing weights."""
    root = root or workspace_root()
    cache = root / "work/hf-narration-cache/hub/models--mlx-community--S3TokenizerV2"
    path = cache / "snapshots" / TOKENIZER_REVISION / "model.safetensors"
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError(
            f"Missing offline S3TokenizerV2 asset at revision {TOKENIZER_REVISION}; install it in the configured Hugging Face cache."
        )
    main = cache / "refs/main"
    if not main.is_file() or main.read_text().strip() != TOKENIZER_REVISION:
        raise RuntimeError(
            "Offline S3TokenizerV2 main reference must resolve to the pinned revision"
        )
    return path


def check_tokenizer(root: Path | None = None) -> dict[str, str]:
    """Require the pinned offline tokenizer checksum even when upstream hides errors."""
    import hashlib

    path = tokenizer_asset(root)
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != TOKENIZER_SHA256:
        raise RuntimeError("Offline S3TokenizerV2 checksum mismatch")
    return {
        "repository": "mlx-community/S3TokenizerV2",
        "revision": TOKENIZER_REVISION,
        "sha256": actual,
    }


def prerequisites(root: Path) -> str | None:
    """Check native Python modules and ancillary assets without importing models."""
    try:
        for name in (
            "mlx",
            "mlx_audio",
            "mlx_whisper",
            "torch",
            "transformers",
            "safetensors",
            "tokenizers",
            "huggingface_hub",
        ):
            if importlib.util.find_spec(name) is None:
                return f"Native inference dependency {name.replace('_', '-')} is missing; install the native or ubuntu extra."
        tokenizer_asset(root)
    except (ImportError, ValueError, RuntimeError, OSError) as exc:
        return str(exc)
    return None


def gpu_lock_path() -> Path:
    """Return one user-level lock shared across independently configured workspaces."""
    override = os.environ.get("VOICE_STUDIO_GPU_LOCK")
    if override:
        return Path(override).expanduser().resolve()
    return (
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        / "voice-studio/gpu.lock"
    )


def clear_exception_frames(error: BaseException) -> None:
    """Release completed frames throughout chained or grouped inference failures."""
    pending = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        traceback.clear_frames(current.__traceback__)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)


@contextlib.contextmanager
def gpu_lease() -> Iterator[None]:
    """Hold the shared process lease; reject a competing worker without waiting."""
    with _LOCK:
        if getattr(_LOCAL, "depth", 0):
            _LOCAL.depth += 1
            try:
                yield
            finally:
                _LOCAL.depth -= 1
            return
        path = gpu_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BlockingIOError(
                    "The GPU is busy in another Voice Studio or audiobook process. Retry after it completes."
                ) from exc
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid()}))
            handle.flush()
            _LOCAL.depth = 1
            try:
                yield
            except BaseException as exc:
                clear_exception_frames(exc)
                raise
            finally:
                try:
                    release_models()
                finally:
                    _LOCAL.depth = 0
                    fcntl.flock(handle, fcntl.LOCK_UN)


def gpu_operation(function: F) -> F:
    """Serialize an inference entry point with all other Voice Studio processes."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with gpu_lease():
            return function(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


def register_model(model: Any) -> None:
    """Register a releasable engine for generation/recognition stage transitions."""
    _MODELS.add(model)


@lru_cache(maxsize=1)
def _cuda_runtime_library() -> Any:
    """Load the pinned CUDA 12 runtime supplied by the installed Ubuntu extra."""
    try:
        distribution = importlib.metadata.distribution("nvidia-cuda-runtime-cu12")
        path = Path(
            str(distribution.locate_file("nvidia/cuda_runtime/lib/libcudart.so.12"))
        )
        if not path.is_file():
            raise RuntimeError(
                "CUDA 12 runtime library is missing; reinstall the ubuntu extra"
            )
        library = ctypes.CDLL(str(path))
    except (importlib.metadata.PackageNotFoundError, OSError) as exc:
        raise RuntimeError(
            "Cannot load the installed CUDA 12 runtime; reinstall the ubuntu extra"
        ) from exc
    library.cudaDeviceSynchronize.argtypes = []
    library.cudaDeviceSynchronize.restype = ctypes.c_int
    library.cudaGetErrorString.argtypes = [ctypes.c_int]
    library.cudaGetErrorString.restype = ctypes.c_char_p
    return library


def cuda_device_synchronize() -> None:
    """Drain CUDA's internal free stream so idle pools return their VRAM to the OS."""
    if settings()["backend"] != "cuda":
        return
    library = _cuda_runtime_library()
    error = library.cudaDeviceSynchronize()
    if error:
        detail = library.cudaGetErrorString(error)
        message = (
            detail.decode("utf-8", errors="replace") if detail else "unknown CUDA error"
        )
        raise RuntimeError(f"CUDA device synchronization failed ({error}): {message}")


def synchronize_mlx() -> None:
    """Finish pending MLX callbacks without importing or initializing its backend."""
    mx = sys.modules.get("mlx.core")
    if mx is not None:
        mx.synchronize()


def clear_cache() -> None:
    """Complete pending frees and return unused native memory before releasing the GPU."""
    mx = sys.modules.get("mlx.core")
    if mx is None:
        gc.collect()
        return
    try:
        mx.synchronize()
    finally:
        gc.collect()
        try:
            mx.clear_cache()
        finally:
            # MLX uses an additional CUDA free stream. MLX synchronization alone
            # does not drain it or release its pool back to a competing process.
            cuda_device_synchronize()


def release_models() -> None:
    """Attempt every release and cache cleanup even if one engine cleanup fails."""
    errors = []
    try:
        synchronize_mlx()
    except Exception as exc:
        clear_exception_frames(exc)
        errors.append(f"MLX synchronization: {exc}")
    for model in list(_MODELS):
        try:
            model.release()
        except Exception as exc:
            clear_exception_frames(exc)
            errors.append(f"{type(exc).__name__}: {exc}")
    try:
        clear_cache()
    except Exception as exc:
        clear_exception_frames(exc)
        errors.append(f"cache cleanup: {exc}")
    if errors:
        raise RuntimeError("GPU cleanup failed: " + "; ".join(errors))


class ReloadableModel:
    """Keep historical generation callers compatible while unloading before ASR."""

    def __init__(self, path: Path, reference: Path, root: Path) -> None:
        self.path, self.reference, self.root = path, reference, root
        self.model: Any = None
        register_model(self)

    def prepare(self) -> None:
        """Load model and conditioning before the caller applies its recorded seed."""
        initialize(self.root)
        if self.model is None:
            check_tokenizer(self.root)
            self.model = load_checked_model(self.path)
            self.model.prepare_conditionals(str(self.reference))

    def generate(self, **kwargs: Any) -> Iterator[Any]:
        """Yield native results; seeded callers must call prepare before seeding."""
        self.prepare()
        try:
            yield from self.model.generate(**kwargs)
        finally:
            initialize(self.root).clear_cache()

    def release(self) -> None:
        """Drop native tensors before recognition or releasing the process lease."""
        if self.model is not None:
            try:
                synchronize_mlx()
            finally:
                self.model = None
                clear_cache()


def load_checked_model(path: Path) -> Any:
    """Fail closed if upstream suppresses text or speech tokenizer load errors."""
    from mlx_audio.tts.utils import load_model

    messages: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger("mlx_audio.tts.models.chatterbox_turbo.chatterbox_turbo")
    handler = Capture()
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        model = load_model(str(path))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    if (
        getattr(model, "tokenizer", None) is None
        and getattr(model, "mtl_tokenizer", None) is None
    ):
        raise RuntimeError("The model's text tokenizer failed to initialize")
    if hasattr(model, "_s3tokenizer"):
        if "Loaded S3 speech tokenizer weights" not in messages or any(
            "Could not load" in message or "Failed to load" in message
            for message in messages
        ):
            raise RuntimeError(
                "The model's speech tokenizer or conditioning failed to initialize: "
                + "; ".join(messages)
            )
    elif getattr(model, "_s3_tokenizer", None) is None:
        raise RuntimeError("The model's speech tokenizer failed to initialize")
    return model


def availability(root: Path) -> tuple[bool, str | None]:
    """Report selected native device readiness without loading speech models."""
    try:
        error = prerequisites(root)
        if error:
            return False, error
        initialize(root)
        return True, None
    except Exception as exc:
        return False, str(exc)
