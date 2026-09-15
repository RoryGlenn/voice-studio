"""Render the approved unquantized Natural voice in an isolated book edition."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import render_book as book
from project_paths import studio_data_dir

JOB = book.ROOT / "work/own_voice/book-natural-high-precision-20260913"
OUTPUT = book.ROOT / "outputs/Thinking in Systems - Natural Higher Precision"
STUDIO = studio_data_dir()
REFERENCE_SHA256 = "6d0db451f9319afb1e9d44f5967bcf38c40008286de717c9da1eb963d18bf01a"
MODEL = {
    "path": "work/own_voice/models/chatterbox-turbo-fp16",
    "repository": "mlx-community/chatterbox-turbo-fp16",
    "revision": "b2d0a13aa7cfff0a06d9acb247ae91c8f19a6d75",
}
MODEL_FILES = {
    "config.json": "aacce8af47c9c930636e47da981339fe0d30e08623d19226aa9a43bd3425b736",
    "model.safetensors": "9f70328a3f6c5257257aea76e9b14c34d8225745d133e4c1e83aa31f3f72a80b",
}


def file_digest(path: Path) -> str:
    """Hash a model file without allocating a model-sized bytes object."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def validate_profile(profile: dict[str, Any]) -> None:
    """Require the exact approved model, reference, and synthesis settings."""
    if any(
        profile["engines"]["natural"].get(key) != value for key, value in MODEL.items()
    ):
        raise ValueError(
            "Natural model differs from the approved higher-precision model"
        )
    if profile["reference_sha256"] != REFERENCE_SHA256:
        raise ValueError("Voice reference differs from the approved New B sample")
    synthesis = profile["synthesis"]
    if any(
        synthesis.get(key) != value
        for key, value in {
            "temperature": 0.8,
            "repetition_penalty": 1.2,
            "seed": 20260905,
        }.items()
    ):
        raise ValueError("Synthesis settings differ from the approved comparison")


def configure() -> None:
    """Validate model bytes before freezing or reusing this edition's cache."""
    model_path = book.ROOT / MODEL["path"]
    for name, expected in MODEL_FILES.items():
        if file_digest(model_path / name) != expected:
            raise ValueError(f"Approved model file changed: {name}")
    JOB.mkdir(parents=True, exist_ok=True)
    profile_path = JOB / "voice_profile.json"
    if not profile_path.exists():
        profile = json.loads((STUDIO / "voice_profile.json").read_text())
        validate_profile(profile)
        reference = STUDIO / profile["reference"]
        if file_digest(reference) != REFERENCE_SHA256:
            raise ValueError("Studio reference audio changed")
        shutil.copy2(reference, JOB / "reference.wav")
        shutil.copy2(
            STUDIO / profile["reference_provenance"], JOB / "reference_provenance.json"
        )
        book.save_json(profile_path, profile)
    profile = json.loads(profile_path.read_text())
    validate_profile(profile)
    if file_digest(JOB / "reference.wav") != REFERENCE_SHA256:
        raise ValueError("Frozen reference audio changed")
    book.JOB, book.OUTPUT = JOB, OUTPUT
    book.REFERENCE = JOB / "reference.wav"
    book.MODEL = model_path
    book.MODEL_REVISION = MODEL["revision"]
    book.MODEL_FILES_SHA256 = dict(MODEL_FILES)
    book.ALBUM = "Thinking in Systems - Natural Higher Precision"
    book.PEAK_ONLY = True
    book.PARAGRAPH_PAUSE, book.PASSAGE_PAUSE = 0.6, 0.12
    os.environ["HF_HOME"] = str(book.ROOT / "work/hf-narration-cache")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


def validate_repair_args(arguments: list[str]) -> None:
    """Allow repair controls without permitting an external checkpoint inventory."""
    parser = argparse.ArgumentParser(
        prog="higher_precision_book.py repair", allow_abbrev=False
    )
    parser.add_argument("--shorter", action="store_true")
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.parse_args(arguments)


def main() -> None:
    """Configure globals before importing the existing book workflow helpers."""
    commands = {
        "render": "render_book",
        "repair": "repair_book_passages",
        "adjudicate": "adjudicate_minor_checks",
        "finalize": "finalize_book",
        "verify": "verify_book_output",
        "audit": "audit_book_checks",
    }
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        raise SystemExit(
            "Usage: higher_precision_book.py {render|repair|adjudicate|finalize|verify|audit} [arguments]"
        )
    action = sys.argv.pop(1)
    try:
        configure()
        # Repair operates only on this edition's own inventory.
        if action == "repair":
            validate_repair_args(sys.argv[1:])
        module = importlib.import_module(commands[action])
        if action == "verify":
            module.JOB, module.OUT = JOB, OUTPUT
        if action != "audit":
            module.main()
    except BlockingIOError:
        # A running owner, if present, retains its progress state.
        raise
    except Exception as error:
        status_path = JOB / "status.json"
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
        book.save_json(
            status_path,
            dict(
                status,
                state="failed",
                action=action,
                error=repr(error),
                updated_at=time.time(),
            ),
        )
        raise


if __name__ == "__main__":
    main()
