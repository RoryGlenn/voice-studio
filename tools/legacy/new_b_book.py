"""Run the accepted book layout as an isolated Natural New B edition."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys

from voice_studio import render_book as book
from voice_studio.project_paths import studio_data_dir

JOB = book.ROOT / "work/own_voice/book-natural-new-b-20260913"
OUTPUT = book.ROOT / "outputs/Thinking in Systems - Natural New B"
STUDIO = studio_data_dir()
REFERENCE_SHA256 = "6d0db451f9319afb1e9d44f5967bcf38c40008286de717c9da1eb963d18bf01a"


def configure() -> None:
    """Freeze the selected voice and isolate every checkpoint and output path."""
    JOB.mkdir(parents=True, exist_ok=True)
    profile_path = JOB / "voice_profile.json"
    if not profile_path.exists():
        profile = json.loads((STUDIO / "voice_profile.json").read_text())
        reference = STUDIO / profile["reference"]
        if (
            profile["reference_sha256"] != REFERENCE_SHA256
            or book.digest(reference.read_bytes()) != REFERENCE_SHA256
        ):
            raise ValueError(
                "The Studio reference is no longer the selected New B voice"
            )
        shutil.copy2(reference, JOB / "reference.wav")
        shutil.copy2(
            STUDIO / profile["reference_provenance"], JOB / "reference_provenance.json"
        )
        book.save_json(profile_path, profile)
    profile = json.loads(profile_path.read_text())
    if book.digest((JOB / "reference.wav").read_bytes()) != REFERENCE_SHA256:
        raise ValueError("Frozen New B reference changed")
    if (
        profile["engines"]["natural"]["revision"]
        != "c63817725071d7b5269c7b558772d6e8cbf59cec"
        or profile["synthesis"]["temperature"] != 0.8
        or profile["synthesis"]["repetition_penalty"] != 1.2
        or profile["synthesis"]["seed"] != 20260905
    ):
        raise ValueError(
            "Frozen Natural synthesis settings differ from the book renderer"
        )
    book.JOB = JOB
    book.OUTPUT = OUTPUT
    book.REFERENCE = JOB / "reference.wav"
    book.MODEL = book.ROOT / profile["engines"]["natural"]["path"]
    book.ALBUM = "Thinking in Systems - Natural New B"
    book.PEAK_ONLY = True
    book.PARAGRAPH_PAUSE = 0.6
    book.PASSAGE_PAUSE = 0.12
    os.environ["HF_HOME"] = str(book.ROOT / "work/hf-narration-cache")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


def main() -> None:
    """Dispatch the existing renderer or checks after configuring the new edition."""
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
            "Usage: new_b_book.py {render|repair|adjudicate|finalize|verify|audit} [arguments]"
        )
    action = sys.argv.pop(1)
    configure()
    module = importlib.import_module(commands[action])
    if action == "verify":
        module.JOB, module.OUT = JOB, OUTPUT
    if action != "audit":
        module.main()


if __name__ == "__main__":
    main()
