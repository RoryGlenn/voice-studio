"""Original synthetic fixtures; no personal audio, books, or models are required."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import render_book as book


def synthetic_profile() -> dict[str, Any]:
    """Return a fake profile suitable for the tone-producing test backend."""
    return {
        "name": "Example Voice",
        "reference": "profile/reference.wav",
        "reference_provenance": "profile/reference.json",
        "reference_sha256": "0" * 64,
        "engines": {
            "natural": {
                "label": "Natural",
                "path": "models/natural",
                "repository": "example/natural",
                "revision": "synthetic-natural",
            },
            "expressive": {
                "label": "Expressive",
                "path": "models/expressive",
                "repository": "example/expressive",
                "revision": "synthetic-expressive",
            },
        },
        "synthesis": {
            "temperature": 0.8,
            "repetition_penalty": 1.2,
            "seed": 20260905,
            "expressive_cfg_weight": 0.3,
        },
    }


@contextmanager
def synthetic_book(edition: Any = None) -> Iterator[Path]:
    """Create a temporary three-track source and optional pinned edition inputs.

    Parameters
    ----------
    edition : module, optional
        Edition wrapper whose external paths and fingerprints should be isolated.

    Yields
    ------
    pathlib.Path
        Temporary workspace root, removed after the test.
    """
    with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ):
        root = Path(temporary)
        text_dir = root / "work/narration_text"
        text_dir.mkdir(parents=True)
        reference = root / "old-reference.wav"
        reference.write_bytes(b"synthetic old reference, not audio")
        tracks = []
        for index in range(1, 4):
            text = (
                f"Measuring Water {index}.\n\n"
                + "A marked bottle receives water from a small tap while a narrow pipe drains it slowly. "
                * 12
                + "\n\nWe record the level at regular intervals and compare our predictions with the observations.\n"
            )
            path = text_dir / f"track-{index}.txt"
            path.write_text(text)
            tracks.append(
                {
                    "track": index,
                    "chapter": index,
                    "disc": index,
                    "folder": f"chapter-{index}",
                    "title": f"Measuring Water {index}",
                    "included_sections": ["Synthetic observations"],
                    "text_path": str(path),
                    "sha256": book.digest(text),
                }
            )
        source = text_dir / "spoken_manifest.json"
        source.write_text(json.dumps({"tracks": tracks}))
        (root / "work/book_subsections.json").write_text(
            json.dumps(
                {
                    "chapters": [
                        {
                            "chapter_title": track["title"],
                            "headings": [{"title": track["title"]}],
                        }
                        for track in tracks
                    ]
                }
            )
        )
        keys = (
            "ROOT",
            "SOURCE",
            "JOB",
            "OUTPUT",
            "REFERENCE",
            "MODEL",
            "MODEL_REVISION",
            "MODEL_FILES_SHA256",
            "ALBUM",
            "PEAK_ONLY",
            "PARAGRAPH_PAUSE",
            "PASSAGE_PAUSE",
        )
        state = {key: getattr(book, key) for key in keys}
        state.update(
            ROOT=root,
            SOURCE=source,
            REFERENCE=reference,
            JOB=root / "work/own_voice/book",
            OUTPUT=root / "outputs/old",
        )
        with patch.multiple(book, **state):
            if edition is None:
                yield root
                return
            studio = root / "studio"
            (studio / "profile").mkdir(parents=True)
            new_reference = b"synthetic new reference, not audio"
            (studio / "profile/reference.wav").write_bytes(new_reference)
            (studio / "profile/reference.json").write_text("{}")
            profile = synthetic_profile()
            profile["reference_sha256"] = book.digest(new_reference)
            if hasattr(edition, "MODEL_FILES"):
                model = copy.deepcopy(edition.MODEL)
            else:
                model = {
                    "path": "models/natural",
                    "repository": "example/natural",
                    "revision": "c63817725071d7b5269c7b558772d6e8cbf59cec",
                }
            profile["engines"]["natural"].update(model)
            (studio / "voice_profile.json").write_text(json.dumps(profile))
            model_path = root / model["path"]
            model_path.mkdir(parents=True)
            (model_path / "config.json").write_text("{}")
            (model_path / "model.safetensors").write_bytes(b"synthetic model weights")
            overrides = {
                "JOB": root / "work/own_voice/edition",
                "OUTPUT": root / "outputs/edition",
                "STUDIO": studio,
                "REFERENCE_SHA256": profile["reference_sha256"],
            }
            if hasattr(edition, "MODEL_FILES"):
                overrides["MODEL_FILES"] = {
                    name: book.digest((model_path / name).read_bytes())
                    for name in edition.MODEL_FILES
                }
            with patch.multiple(edition, **overrides):
                yield root
