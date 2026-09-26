"""An on-demand local text-to-speech studio using a saved voice reference."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import copy
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import numpy as np
import soundfile as sf

import runtime
from project_paths import studio_data_dir, workspace_root
from voice_profiles import (
    DEFAULT_VOICE_ID,
    engine_capabilities,
    load_profiles,
    profile_defaults,
    profile_identity,
)

APP = Path(__file__).resolve().parent
WORKSPACE = workspace_root()
RATE = 24000
MAX_TEXT = 500_000
MAX_BODY = 8_000_000
TERMINAL = {"completed", "failed", "cancelled"}


def sha256(data: bytes) -> str:
    """Return the hexadecimal SHA-256 of a byte sequence."""
    return hashlib.sha256(data).hexdigest()


def save_json(path: Path, value: Any) -> None:
    """Atomically replace a local JSON file with UTF-8 data."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def split_text(text: str) -> list[dict[str, Any]]:
    """Plan bounded passages while preserving all non-whitespace text in order.

    Parameters
    ----------
    text : str
        Plain English text, with blank lines separating paragraphs.

    Returns
    -------
    list of dict
        Passage text and whether it ends a paragraph. Every passage is at most
        400 characters and 60 whitespace-delimited words.
    """
    chunks: list[dict[str, Any]] = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        remaining = " ".join(paragraph.split())
        if any(len(word) > 400 for word in remaining.split()):
            raise ValueError(
                "A word or URL exceeds 400 characters. Shorten it before rendering."
            )
        while remaining:
            words = list(re.finditer(r"\S+", remaining[:401]))
            if len(remaining) <= 400 and len(words) <= 60:
                chunks.append({"text": remaining, "paragraph_end": True})
                break
            limit = min(400, words[59].end() if len(words) >= 60 else len(remaining))
            boundaries = [
                m.start() for m in re.finditer(r"\s+", remaining[: limit + 1])
            ]
            if not boundaries:
                raise ValueError("Cannot safely split this passage.")
            cut = boundaries[-1]
            # Prefer a substantial complete sentence; do not split after common titles.
            sentences = [
                b
                for b in boundaries
                if b >= limit * 0.5
                and re.search(r"[.!?][\"'’”)]?$", remaining[:b])
                and not re.search(
                    r"\b(?:Mr|Mrs|Ms|Dr|Prof|St|e\.g|i\.e)\.$", remaining[:b]
                )
            ]
            if sentences:
                cut = sentences[-1]
            chunks.append({"text": remaining[:cut].strip(), "paragraph_end": False})
            remaining = remaining[cut:].strip()
    if " ".join(c["text"] for c in chunks).split() != text.split():
        raise ValueError("Passage planning changed the text.")
    return chunks


def validate_request(
    data: Any,
    engines: dict[str, Any],
    *,
    default_engine: str = "natural",
    default_expression: float = 0.5,
) -> dict[str, Any]:
    """Validate supported user controls and return explicit synthesis settings."""
    allowed = {
        "text",
        "title",
        "mode",
        "engine",
        "expression",
        "speed",
        "paragraph_pause_ms",
        "voice_id",
    }
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError(
            "Request must contain only supported text and delivery settings."
        )
    voice_id = data.get("voice_id", DEFAULT_VOICE_ID)
    if not isinstance(voice_id, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,63}", voice_id
    ):
        raise ValueError("Choose a valid saved voice.")
    text = data.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f"Enter between 1 and {MAX_TEXT:,} characters of text.")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise ValueError("Text contains unsupported control characters.")
    title = data.get("title", "Untitled narration")
    if (
        not isinstance(title, str)
        or len(title) > 100
        or any(ord(c) < 32 for c in title)
    ):
        raise ValueError("Use a title of at most 100 characters, without line breaks.")
    engine = data.get("engine", default_engine)
    if (
        not isinstance(engine, str)
        or engine not in engines
        or not engines[engine]["available"]
    ):
        raise ValueError("That voice engine is unavailable on this computer.")
    mode = data.get("mode", "full")
    if mode not in ("preview", "full"):
        raise ValueError("Mode must be preview or full.")
    values: dict[str, float] = {}
    for key, default, minimum, maximum in (
        ("speed", 1.0, 0.8, 1.2),
        ("paragraph_pause_ms", 600, 0, 2000),
        ("expression", default_expression, 0.0, 1.0),
    ):
        value = data.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
        ):
            raise ValueError(f"{key} must be a finite number.")
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}.")
        values[key] = float(value)
    if engine == "natural" and "expression" in data:
        raise ValueError(
            "Natural voice has no expression slider; choose Expressive voice to use it."
        )
    source = text.strip().replace("\r\n", "\n").replace("\r", "\n")
    rendered = source
    if mode == "preview":
        words = list(re.finditer(r"\S+", source))
        if len(words) > 120:
            rendered = source[: words[119].end()]
    return {
        "text": source,
        "rendered_text": rendered,
        "title": title.strip() or "Untitled narration",
        "engine": engine,
        "mode": mode,
        "voice_id": voice_id,
        **values,
    }


class NativeEngine:
    """Keep one local MLX engine and its reference conditioning in one worker."""

    def __init__(
        self,
        profile: dict[str, Any],
        root: Path | None = None,
        reference_dir: Path | None = None,
    ) -> None:
        self.profile = copy.deepcopy(profile)
        self.root = root if root is not None else workspace_root()
        self.reference_dir = (
            reference_dir if reference_dir is not None else studio_data_dir()
        )
        runtime.register_model(self)
        self.model: Any = None
        self.conds: Any = None
        self.key: str | None = None
        self.cache_identity: str | None = None

    def select_profile(self, profile: dict[str, Any]) -> None:
        """Select a detached job profile inside the single render worker."""
        self.profile = copy.deepcopy(profile)

    def prepare(self, key: str, expression: float) -> float:
        """Load a model only when changed, then prepare the saved reference."""
        import gc

        os.environ["HF_HOME"] = str(self.root / "work/hf-narration-cache")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        mx = runtime.initialize(self.root)
        runtime.check_tokenizer(self.root)

        started = time.monotonic()
        reference = self.reference_dir / self.profile["reference"]
        model_path = self.root / self.profile["engines"][key]["path"]
        identity = profile_identity(
            {
                "engine": key,
                "model": self.profile["engines"][key],
                "model_path": str(model_path.resolve()),
                "reference_path": str(reference.resolve()),
                "reference_sha256": self.profile["reference_sha256"],
                "conditioning_expression": expression if key == "expressive" else None,
            }
        )
        if self.cache_identity != identity:
            self.conds = None
            self.model = None
            self.key = None
            self.cache_identity = None
            gc.collect()
            mx.clear_cache()
        try:
            if sha256(reference.read_bytes()) != self.profile["reference_sha256"]:
                raise ValueError(
                    "The selected voice reference changed; restore it before rendering."
                )
            if self.cache_identity != identity:
                self.model = runtime.load_checked_model(model_path)
                if key == "natural":
                    self.model.prepare_conditionals(str(reference))
                else:
                    self.conds = self.model.prepare_conditionals(
                        str(reference), ref_sr=RATE, exaggeration=expression
                    )
                self.key = key
                self.cache_identity = identity
        except Exception:
            self.conds = None
            self.model = None
            self.key = None
            self.cache_identity = None
            gc.collect()
            mx.clear_cache()
            raise
        mx.random.seed(self.profile["synthesis"]["seed"])
        return time.monotonic() - started

    def release(self) -> None:
        """Drop model and reference tensors before another GPU workload starts."""
        try:
            runtime.synchronize_mlx()
        finally:
            self.conds = None
            self.model = None
            self.key = None
            self.cache_identity = None
            runtime.clear_cache()

    def generate(
        self, text: str, settings: dict[str, Any]
    ) -> Iterator[tuple[np.ndarray, int]]:
        """Yield native audio using only controls supported by the selected model."""
        params = self.profile["synthesis"]
        kwargs: dict[str, Any] = {
            "text": text,
            "temperature": params["temperature"],
            "repetition_penalty": params["repetition_penalty"],
        }
        if self.key == "natural":
            kwargs.update(
                top_p=0.95,
                max_tokens=settings.get("max_tokens", 1200),
                split_pattern=None,
            )
        else:
            kwargs.update(
                conds=self.conds,
                exaggeration=settings["expression"],
                cfg_weight=params["expressive_cfg_weight"],
                min_p=0.05,
                top_p=1.0,
                max_new_tokens=1000,
                lang_code="en",
                verbose=False,
            )
        for result in self.model.generate(**kwargs):
            yield np.asarray(result.audio, dtype=np.float32), int(result.sample_rate)


def process_speed(audio: np.ndarray, speed: float, ffmpeg: str) -> np.ndarray:
    """Change tempo without intentional pitch shifting; bypass at normal speed."""
    if speed == 1.0:
        return audio
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-f",
            "f32le",
            "-ar",
            str(RATE),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-af",
            f"atempo={speed}",
            "-f",
            "f32le",
            "pipe:1",
        ],
        input=audio.astype("<f4").tobytes(),
        capture_output=True,
        check=True,
        timeout=120,
    )
    return np.frombuffer(result.stdout, dtype="<f4").copy()


class Cancelled(Exception):
    """Signal that a request was cancelled before completion."""


class Studio:
    """Own a single render worker and durable job manifests."""

    def __init__(
        self,
        directory: Path | None = None,
        engine_factory: Callable[..., Any] = NativeEngine,
        *,
        workspace: Path | None = None,
        web_directory: Path | None = None,
    ) -> None:
        directory = directory if directory is not None else studio_data_dir()
        self.directory = directory
        self.workspace = workspace if workspace is not None else workspace_root()
        self.web_directory = web_directory if web_directory is not None else APP / "web"
        self.profiles = load_profiles(directory)
        self.profile = self.profiles[DEFAULT_VOICE_ID]
        self.renders = directory / "renders"
        self.renders.mkdir(exist_ok=True)
        self.ffmpeg = runtime.binary("ffmpeg")
        self.engine = (
            NativeEngine(self.profile, self.workspace, self.directory)
            if engine_factory is NativeEngine
            else engine_factory(self.profile)
        )
        self.runtime_available, self.runtime_error = (
            runtime.availability(self.workspace)
            if engine_factory is NativeEngine
            else (True, None)
        )
        self.engine_factory = engine_factory
        self.engine_profile_identity = profile_identity(self.profile)
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="voice"
        )
        self.lock = threading.RLock()
        self.active: str | None = None
        self.stopping = False
        self.cancel_event = threading.Event()
        self.jobs: dict[str, dict[str, Any]] = {}
        for path in self.renders.glob("*/manifest.json"):
            try:
                job = json.loads(path.read_text())
                if not re.fullmatch(r"[a-f0-9]{32}", job["id"]):
                    continue
                if job["status"] not in TERMINAL:
                    job.update(
                        status="failed",
                        error="The app stopped before this render completed.",
                    )
                    save_json(path, job)
                self.jobs[job["id"]] = job
            except (ValueError, KeyError, OSError):
                continue

    def config(self) -> dict[str, Any]:
        """Return public voice capabilities without disclosing filesystem paths."""
        voices = {
            key: {
                "name": profile["name"],
                "description": profile.get("description", "Saved voice reference"),
                "engines": engine_capabilities(
                    profile,
                    self.workspace,
                    self.directory,
                    native=self.engine_factory is NativeEngine,
                ),
                **profile_defaults(profile),
            }
            for key, profile in self.profiles.items()
        }
        if not self.runtime_available:
            for voice in voices.values():
                for capability in voice["engines"].values():
                    capability["available"] = False
        return {
            "runtime": {
                "available": self.runtime_available,
                "error": self.runtime_error,
            },
            "voice": self.profile["name"],
            "engines": voices[DEFAULT_VOICE_ID]["engines"],
            "default_voice_id": DEFAULT_VOICE_ID,
            **profile_defaults(self.profile),
            "voices": voices,
            "max_text": MAX_TEXT,
            "preview_words": 120,
            "active": self.active,
        }

    def update(self, job_id: str, **changes: Any) -> None:
        """Persist a status transition and make it visible to browser polling."""
        with self.lock:
            self.jobs[job_id].update(changes, updated_at=time.time())
            save_json(self.renders / job_id / "manifest.json", self.jobs[job_id])

    def snapshot(self, job_id: str | None = None) -> Any:
        """Return a detached job record or reverse-chronological job history."""
        with self.lock:
            if job_id is not None:
                if job_id not in self.jobs:
                    raise KeyError("Unknown render.")
                return copy.deepcopy(self.jobs[job_id])
            return copy.deepcopy(
                sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True)
            )

    def submit(self, data: Any) -> dict[str, Any]:
        """Validate text, reserve the worker, and start a single render request."""
        if not isinstance(data, dict):
            raise ValueError(
                "Request must contain supported text and delivery settings."
            )
        voice_id = data.get("voice_id", DEFAULT_VOICE_ID)
        if not isinstance(voice_id, str) or voice_id not in self.profiles:
            raise ValueError("That saved voice is unavailable.")
        profile = copy.deepcopy(self.profiles[voice_id])
        config = self.config()
        engines = (
            config["engines"]
            if voice_id == DEFAULT_VOICE_ID
            else config["voices"][voice_id]["engines"]
        )
        settings = validate_request(data, engines, **profile_defaults(profile))
        chunks = split_text(settings["rendered_text"])
        with self.lock:
            if self.stopping:
                raise RuntimeError(
                    "The studio is closing. Relaunch it to render more audio."
                )
            if self.active is not None:
                raise RuntimeError(
                    "A render is already running. Finish or cancel it first."
                )
            job_id = uuid.uuid4().hex
            folder = self.renders / job_id
            folder.mkdir()
            (folder / "input.txt").write_text(settings["text"])
            (folder / "spoken.txt").write_text(settings["rendered_text"])
            save_json(folder / "request.json", settings)
            save_json(folder / "passages.json", chunks)
            job = {
                "id": job_id,
                "title": settings["title"],
                "status": "queued",
                "mode": settings["mode"],
                "created_at": time.time(),
                "updated_at": time.time(),
                "engine": settings["engine"],
                "voice_id": voice_id,
                "voice_name": profile["name"],
                "voice_profile_sha256": profile_identity(profile),
                "synthesis": copy.deepcopy(profile["synthesis"]),
                "settings": {
                    k: v
                    for k, v in settings.items()
                    if k not in ("text", "rendered_text", "title")
                },
                "input_sha256": sha256(settings["text"].encode()),
                "spoken_sha256": sha256(settings["rendered_text"].encode()),
                "reference_sha256": profile["reference_sha256"],
                "model": copy.deepcopy(profile["engines"][settings["engine"]]),
                "words": len(settings["rendered_text"].split()),
                "total_passages": len(chunks),
                "completed_passages": 0,
                "audio_seconds": 0,
                "error": None,
                "processing": "Peak limiting for headroom; optional pitch-preserving tempo change; MP3 encoding. No pitch, formant, EQ or added reverb.",
                "verification": "Signal and file checks only. Listen to confirm wording and voice quality.",
            }
            self.jobs[job_id] = job
            save_json(folder / "manifest.json", job)
            self.active = job_id
            self.cancel_event.clear()
            self.pool.submit(self._render, job_id, settings, chunks, profile)
            return copy.deepcopy(job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Request cancellation at the next passage or encoding boundary."""
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError("Unknown render.")
            if self.active == job_id:
                self.cancel_event.set()
                self.update(job_id, cancelling=True)
            return self.snapshot(job_id)

    def _check_cancel(self) -> None:
        """Raise when the user has requested cancellation."""
        if self.cancel_event.is_set():
            raise Cancelled()

    def _run_export(self, command: list[str]) -> None:
        """Run an encoder or decoder while keeping cancellation responsive.

        Parameters
        ----------
        command : list of str
            FFmpeg arguments, without shell interpretation.
        """
        self._check_cancel()
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=errors
            )
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if self.cancel_event.wait(0.1):
                        raise Cancelled()
                    if time.monotonic() - started > 1800:
                        raise TimeoutError("Audio export exceeded its 30-minute limit.")
                self._check_cancel()
                if process.returncode:
                    errors.seek(max(0, errors.tell() - 500))
                    message = errors.read().decode(errors="replace")
                    raise RuntimeError("Audio processing failed: " + message)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)

    def _render(
        self,
        job_id: str,
        settings: dict[str, Any],
        chunks: list[dict[str, Any]],
        profile: dict[str, Any],
    ) -> None:
        """Synthesize and export one request, without holding the HTTP thread."""
        folder = self.renders / job_id
        started = time.monotonic()
        evidence: list[dict[str, Any]] = []
        try:
            self._check_cancel()
            with contextlib.ExitStack() as stack:
                if isinstance(self.engine, NativeEngine):
                    stack.enter_context(runtime.gpu_lease())
                self._synthesize(
                    job_id, settings, chunks, profile, folder, started, evidence
                )
            return
        except Cancelled:
            self.update(job_id, status="cancelled", cancelling=False)
        except Exception as exc:
            self.update(job_id, status="failed", error=str(exc) or type(exc).__name__)
        finally:
            with self.lock:
                if self.active == job_id:
                    self.active = None

    def _synthesize(
        self,
        job_id: str,
        settings: dict[str, Any],
        chunks: list[dict[str, Any]],
        profile: dict[str, Any],
        folder: Path,
        started: float,
        evidence: list[dict[str, Any]],
    ) -> None:
        """Run the render while its native GPU lease remains held."""
        frames = 0
        try:
            self.update(job_id, status="loading", runtime=runtime.identity())
            selected_identity = profile_identity(profile)
            if self.engine_profile_identity != selected_identity:
                if isinstance(self.engine, NativeEngine):
                    self.engine.select_profile(profile)
                else:
                    self.engine = self.engine_factory(copy.deepcopy(profile))
                self.engine_profile_identity = selected_identity
            load_seconds = self.engine.prepare(
                settings["engine"], settings["expression"]
            )
            self._check_cancel()
            self.update(job_id, status="rendering", load_seconds=load_seconds)
            with sf.SoundFile(
                folder / "narration.partial.wav",
                "w",
                samplerate=RATE,
                channels=1,
                subtype="PCM_24",
            ) as output:
                for index, chunk in enumerate(chunks):
                    self._check_cancel()
                    chunk_started = time.monotonic()
                    blocks = []
                    for audio, rate in self.engine.generate(chunk["text"], settings):
                        self._check_cancel()
                        if (
                            rate != RATE
                            or audio.ndim != 1
                            or not audio.size
                            or not np.isfinite(audio).all()
                        ):
                            raise ValueError(
                                "The speech engine produced invalid audio."
                            )
                        blocks.append(audio)
                    if not blocks:
                        raise ValueError("The speech engine returned no audio.")
                    audio = np.concatenate(blocks)
                    peak = float(np.max(np.abs(audio)))
                    if peak < 0.001 or audio.size > RATE * 180:
                        raise ValueError(
                            "The speech engine returned silent or unexpectedly long audio."
                        )
                    gain = min(1.0, 0.95 / peak)
                    audio = process_speed(audio * gain, settings["speed"], self.ffmpeg)
                    if not audio.size or not np.isfinite(audio).all():
                        raise ValueError("Tempo processing returned invalid audio.")
                    # atempo can slightly overshoot: preserve headroom without altering pitch.
                    audio *= min(1.0, 0.95 / max(float(np.max(np.abs(audio))), 1e-12))
                    output.write(audio)
                    pause_frames = 0
                    if index < len(chunks) - 1:
                        pause = (
                            settings["paragraph_pause_ms"] / 1000
                            if chunk["paragraph_end"]
                            else 0.12
                        )
                        pause_frames = round(RATE * pause)
                        output.write(np.zeros(pause_frames, dtype=np.float32))
                    frames += audio.size + pause_frames
                    evidence.append(
                        {
                            "index": index,
                            "text_sha256": sha256(chunk["text"].encode()),
                            "audio_frames": audio.size,
                            "pause_frames": pause_frames,
                            "peak_gain": gain,
                            "elapsed_seconds": time.monotonic() - chunk_started,
                        }
                    )
                    save_json(folder / "passage_evidence.json", evidence)
                    self.update(
                        job_id,
                        completed_passages=index + 1,
                        audio_seconds=frames / RATE,
                        elapsed_seconds=time.monotonic() - started,
                    )
            self._check_cancel()
            self.update(job_id, status="encoding")
            self._run_export(
                [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    str(folder / "narration.partial.wav"),
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "128k",
                    "-metadata",
                    f"title={settings['title']}",
                    "-metadata",
                    f"artist={profile['name']} (AI-generated voice)",
                    "-metadata",
                    "comment=AI-generated speech using the selected saved voice reference",
                    "-y",
                    str(folder / "narration.partial.mp3"),
                ]
            )
            self._check_cancel()
            info = sf.info(folder / "narration.partial.wav")
            if info.frames != frames or info.samplerate != RATE or info.channels != 1:
                raise ValueError("The WAV export did not match the rendered audio.")
            self._run_export(
                [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    str(folder / "narration.partial.mp3"),
                    "-f",
                    "null",
                    "-",
                ]
            )
            files = {}
            with self.lock:
                self._check_cancel()
                for extension in ("wav", "mp3"):
                    final = folder / f"narration.{extension}"
                    (folder / f"narration.partial.{extension}").replace(final)
                    files[extension] = {
                        "bytes": final.stat().st_size,
                        "sha256": file_hash(final),
                    }
                self.update(
                    job_id,
                    status="completed",
                    files=files,
                    sample_rate=RATE,
                    wav_bit_depth=24,
                    elapsed_seconds=time.monotonic() - started,
                    cancelling=False,
                )
        except Cancelled:
            self.update(
                job_id,
                status="cancelled",
                cancelling=False,
                elapsed_seconds=time.monotonic() - started,
            )
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            if isinstance(exc, subprocess.CalledProcessError):
                error = (
                    "Audio processing failed: "
                    + (exc.stderr or b"").decode(errors="replace")[-500:]
                )
            self.update(
                job_id,
                status="failed",
                error=error,
                elapsed_seconds=time.monotonic() - started,
            )
        finally:
            if isinstance(self.engine, NativeEngine):
                self.engine.release()
            for path in folder.glob("*.partial.*"):
                path.unlink(missing_ok=True)


def file_hash(path: Path) -> str:
    """Hash an export without reading the whole file into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class Server(ThreadingHTTPServer):
    """Serve one local studio instance with authenticated private routes."""

    daemon_threads = True

    def __init__(self, port: int, studio: Studio, token: str) -> None:
        self.studio = studio
        self.token = token
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    """Implement the small same-origin JSON and media API."""

    server: Server

    def log_message(self, format: str, *args: Any) -> None:
        """Avoid logging private session tokens or entered text."""

    def end_headers(self) -> None:
        """Apply same-origin and no-cache headers to every response."""
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )
        super().end_headers()

    def reply(self, status: int, data: Any) -> None:
        """Write a JSON response with an explicit length."""
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, private: bool = True) -> bool:
        """Reject remote origins, rebinding hosts, and unauthenticated API calls."""
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            self.reply(403, {"error": "This studio accepts local connections only."})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{host}" for host in hosts}:
            self.reply(403, {"error": "Cross-origin access is not allowed."})
            return False
        if private:
            query = parse_qs(urlsplit(self.path).query)
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            token = token or query.get("token", [""])[0]
            if not hmac.compare_digest(token, self.server.token):
                self.reply(
                    401, {"error": "Open the studio using its launcher to reconnect."}
                )
                return False
        return True

    def do_GET(self) -> None:
        """Serve the interface, capabilities, history, or completed audio."""
        path = urlsplit(self.path).path
        private = path not in ("/", "/app.js", "/style.css", "/favicon.ico")
        if not self.authorized(private):
            return
        try:
            if path in ("/", "/app.js", "/style.css"):
                name, mime = {
                    "/": ("index.html", "text/html"),
                    "/app.js": ("app.js", "text/javascript"),
                    "/style.css": ("style.css", "text/css"),
                }[path]
                data = (self.server.studio.web_directory / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == "/api/config":
                self.reply(200, self.server.studio.config())
            elif path == "/api/jobs":
                self.reply(200, self.server.studio.snapshot())
            elif match := re.fullmatch(r"/api/jobs/([a-f0-9]{32})", path):
                self.reply(200, self.server.studio.snapshot(match[1]))
            elif match := re.fullmatch(r"/audio/([a-f0-9]{32})/(wav|mp3)", path):
                self.send_audio(match[1], match[2])
            else:
                self.reply(404, {"error": "Not found."})
        except (KeyError, FileNotFoundError):
            self.reply(404, {"error": "Not found."})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_audio(self, job_id: str, extension: str) -> None:
        """Stream completed audio with range support for browser seeking."""
        job = self.server.studio.snapshot(job_id)
        if job["status"] != "completed":
            self.reply(409, {"error": "This render has no completed export."})
            return
        path = self.server.studio.renders / job_id / f"narration.{extension}"
        size = path.stat().st_size
        start, end = 0, size - 1
        partial = self.headers.get("Range")
        if partial:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", partial)
            if not match or not any(match.groups()):
                self.reply(416, {"error": "Unsupported byte range."})
                return
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), end) if match[2] else end
            else:
                start = max(0, size - int(match[2]))
            if start > end or start >= size:
                self.reply(416, {"error": "Byte range exceeds the file."})
                return
        self.send_response(206 if partial else 200)
        self.send_header(
            "Content-Type", "audio/wav" if extension == "wav" else "audio/mpeg"
        )
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if parse_qs(urlsplit(self.path).query).get("download") == ["1"]:
            name = (
                re.sub(r"[^A-Za-z0-9 _-]", "", job["title"])[:60].strip() or "Narration"
            )
            self.send_header(
                "Content-Disposition", f'attachment; filename="{name}.{extension}"'
            )
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            left = end - start + 1
            while left:
                block = source.read(min(left, 65536))
                if not block:
                    break
                self.wfile.write(block)
                left -= len(block)

    def do_POST(self) -> None:
        """Start or cancel a render after strict local request validation."""
        if not self.authorized():
            return
        if self.headers.get_content_type() != "application/json":
            self.reply(415, {"error": "Send application/json."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                self.reply(413, {"error": "Request is empty or too large."})
                return
            data = json.loads(self.rfile.read(length))
            path = urlsplit(self.path).path
            if path == "/api/jobs":
                self.reply(202, self.server.studio.submit(data))
            elif path == "/api/quit":
                with self.server.studio.lock:
                    if self.server.studio.active is not None:
                        raise RuntimeError(
                            "Finish or cancel the current render before quitting."
                        )
                    self.server.studio.stopping = True
                    self.reply(200, {"status": "stopping"})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif match := re.fullmatch(r"/api/jobs/([a-f0-9]{32})/cancel", path):
                self.reply(200, self.server.studio.cancel(match[1]))
            else:
                self.reply(404, {"error": "Not found."})
        except (ValueError, TypeError, UnicodeError) as exc:
            self.reply(400, {"error": str(exc)})
        except RuntimeError as exc:
            self.reply(409, {"error": str(exc)})
        except KeyError:
            self.reply(404, {"error": "Unknown render."})


def main() -> None:
    """Launch the studio or reopen the existing local instance."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local port; default chooses an unused port.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the URL without opening a browser.",
    )
    parser.add_argument(
        "--data-dir", type=Path, help="Profile, recordings, and server state directory."
    )
    parser.add_argument(
        "--workspace", type=Path, help="Models, cache, and audiobook data root."
    )
    args = parser.parse_args()
    directory = args.data_dir.resolve() if args.data_dir else studio_data_dir()
    workspace = args.workspace.resolve() if args.workspace else workspace_root()
    if not (directory / "voice_profile.json").is_file():
        parser.error(
            "No saved voice profile. Configure local.toml and copy config/voice_profile.example.json to the data directory as voice_profile.json, then supply your reference and its SHA-256."
        )
    state = directory / "state"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "instance.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            info = json.loads((state / "running.json").read_text())
            request = Request(
                info["base_url"] + "/api/config",
                headers={"Authorization": "Bearer " + info["token"]},
            )
            with urlopen(request, timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError("The existing studio is not responding.")
            print("Voice Studio is already running: " + info["url"], flush=True)
            if not args.no_browser:
                webbrowser.open(info["url"])
            return
        studio = Studio(directory, workspace=workspace)
        token = secrets.token_urlsafe(32)
        server = Server(args.port, studio, token)
        base = f"http://127.0.0.1:{server.server_port}"
        url = base + "/#token=" + token
        info = {"pid": os.getpid(), "base_url": base, "url": url, "token": token}
        save_json(state / "running.json", info)
        (state / "running.json").chmod(0o600)
        print("Voice Studio: " + url, flush=True)
        print(
            "Keep this window open. Press Control-C to stop. Audio stays on this computer.",
            flush=True,
        )
        if not args.no_browser:
            webbrowser.open(url)

        def stop(signum: int, frame: Any) -> None:
            """Stop accepting requests and cancel unfinished speech at a boundary."""
            studio.cancel_event.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            studio.cancel_event.set()
            server.server_close()
            studio.pool.shutdown(wait=True, cancel_futures=True)
            with contextlib.suppress(FileNotFoundError):
                (state / "running.json").unlink()


if __name__ == "__main__":
    main()
