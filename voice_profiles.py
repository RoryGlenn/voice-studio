"""Load private voice profiles while preserving the original default profile."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import runtime

DEFAULT_VOICE_ID = "default"


def profile_identity(profile: dict[str, Any]) -> str:
    """Fingerprint the complete settings snapshot used by one render."""
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


def profile_defaults(profile: dict[str, Any]) -> dict[str, Any]:
    """Return validated per-voice defaults without mutating the saved profile."""
    engines = profile["engines"]
    fallback = "natural" if "natural" in engines else next(iter(engines))
    engine = profile.get("default_engine", fallback)
    if not isinstance(engine, str) or engine not in engines:
        raise ValueError("The default engine must be defined in the voice profile")
    expression = profile.get("default_expression", 0.5)
    if (
        isinstance(expression, bool)
        or not isinstance(expression, (int, float))
        or not math.isfinite(expression)
        or not 0 <= expression <= 1
    ):
        raise ValueError("The default expression must be a finite number from 0 to 1")
    return {"default_engine": engine, "default_expression": float(expression)}


def load_profiles(directory: Path) -> dict[str, dict[str, Any]]:
    """Load the default and optional voices without changing their source files.

    Parameters
    ----------
    directory : pathlib.Path
        Private Studio data directory. All reference paths remain relative here.

    Returns
    -------
    dict
        Profiles keyed by stable file-stem IDs, with the original default first.
    """
    files = [(DEFAULT_VOICE_ID, directory / "voice_profile.json")]
    files.extend((p.stem, p) for p in sorted((directory / "voices").glob("*.json")))
    profiles = {}
    for voice_id, path in files:
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", voice_id)
            or voice_id in profiles
        ):
            raise ValueError(f"Invalid or duplicate voice ID: {voice_id}")
        profile = json.loads(path.read_text())
        if not isinstance(profile, dict):
            raise ValueError(f"Invalid voice profile: {path.name}")
        for key in ("name", "reference", "reference_sha256"):
            if not isinstance(profile.get(key), str) or not profile[key].strip():
                raise ValueError(f"Voice {voice_id} needs a nonempty {key}")
        if not re.fullmatch(r"[a-f0-9]{64}", profile["reference_sha256"]):
            raise ValueError(f"Voice {voice_id} needs a SHA-256 reference fingerprint")
        if not isinstance(profile.get("engines"), dict) or not profile["engines"]:
            raise ValueError(f"Voice {voice_id} needs an engine")
        for key, engine in profile["engines"].items():
            if key not in {"natural", "expressive"} or not isinstance(engine, dict):
                raise ValueError(f"Unsupported engine for voice {voice_id}")
            if any(
                not isinstance(engine.get(field), str) or not engine[field]
                for field in ("path", "label")
            ):
                raise ValueError(f"Voice {voice_id} needs engine path and label")
        if not isinstance(profile.get("synthesis"), dict):
            raise ValueError(f"Voice {voice_id} needs synthesis settings")
        profile_defaults(profile)
        profiles[voice_id] = profile
    return profiles


def engine_capabilities(
    profile: dict[str, Any], workspace: Path, directory: Path, *, native: bool = True
) -> dict[str, Any]:
    """Describe cheap native readiness without hashing model files during polling."""
    reference_present = (directory / profile["reference"]).is_file()
    prerequisite_error = runtime.prerequisites(workspace) if native else None
    engines = {}
    for key, value in profile["engines"].items():
        path = workspace / value["path"]
        required = ["config.json"]
        if native:
            required.extend(("model.safetensors", "conds.safetensors"))
        present = all(
            (path / name).is_file() and (path / name).stat().st_size > 0
            for name in required
        )
        if not native:
            present = present and any(path.glob("*.safetensors"))
        if native:
            if key == "natural":
                text_tokenizer = (path / "tokenizer.json").is_file() or all(
                    (path / name).is_file()
                    for name in ("vocab.json", "merges.txt", "tokenizer_config.json")
                )
            else:
                text_tokenizer = (path / "tokenizer.json").is_file()
            present = present and text_tokenizer
        engines[key] = {
            "label": value["label"],
            "available": reference_present and present and prerequisite_error is None,
            "expression": key == "expressive",
        }
    return engines
