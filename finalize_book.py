"""Assemble the final audiobook exclusively from the latest verified checkpoints."""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from render_book import (
    JOB,
    assemble_track,
    build_plan,
    compare,
    digest,
    publish_navigation,
    save_json,
)


def checked_records(track: dict[str, Any], identity: str) -> list[dict[str, Any]]:
    """Require checked text, provenance and intact mono 24 kHz audio for each passage."""
    records = []
    for segment in track["segments"]:
        path = (
            JOB
            / "segments"
            / f"{track['track']:02d}"
            / f"{segment['number']:04d}-{segment['text_sha256'][:10]}.json"
        )
        record = json.loads(path.read_text())
        if (
            record["status"] != "verified"
            or record["identity"] != identity
            or record["text"] != segment["text"]
            or record["text_sha256"] != segment["text_sha256"]
        ):
            raise ValueError(f"Unchecked or stale passage: {path}")
        check = record["strong_check"] or record["tiny_check"]
        if (
            record["duration_flagged"]
            or compare(
                record["text"],
                check["recognized"],
                strict=record.get("strict_review", False),
            )["flagged"]
        ):
            raise ValueError(f"Passage requires a review: {path}")
        audio = Path(record["file"])
        if digest(audio.read_bytes()) != record["audio_sha256"]:
            raise ValueError(f"Changed passage audio: {audio}")
        wave, rate = sf.read(audio, dtype="float32")
        if (
            rate != 24000
            or wave.ndim != 1
            or not len(wave)
            or not np.isfinite(wave).all()
        ):
            raise ValueError(f"Invalid passage audio: {audio}")
        if abs(len(wave) / rate - record["duration_seconds"]) > 1 / rate:
            raise ValueError(f"Stale passage duration: {audio}")
        if len(record["text"].split()) / record["duration_seconds"] * 60 > 320:
            raise ValueError(f"Implausibly rapid speech requires review: {audio}")
        records.append(record)
    return records


def main() -> None:
    """Rebuild all thirty tracks after rendering and passage repairs have finished.

    Returns
    -------
    None
        Saves final navigation and completion state only after every export succeeds.
    """
    with (JOB / "job.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads((JOB / "plan.json").read_text())
        if plan != build_plan():
            raise ValueError("Rendering plan or source provenance changed")
        all_records = [
            checked_records(track, plan["identity_sha256"]) for track in plan["tracks"]
        ]
        completed = []
        for track, records in zip(plan["tracks"], all_records, strict=True):
            save_json(
                JOB / "status.json",
                {
                    "state": "assembling",
                    "track": track["track"],
                    "title": track["title"],
                    "completed_tracks": len(completed),
                    "updated_at": time.time(),
                },
            )
            row = assemble_track(track, records)
            completed.append(row)
            save_json(JOB / "completed_tracks.json", completed)
            publish_navigation(completed)
            print(
                json.dumps(
                    {
                        "event": "final_track_exported",
                        "track": row["track"],
                        "duration_seconds": row["duration_seconds"],
                    }
                ),
                flush=True,
            )
        save_json(
            JOB / "status.json",
            {
                "state": "complete",
                "completed_tracks": len(completed),
                "pending_reviews": [],
                "updated_at": time.time(),
            },
        )


if __name__ == "__main__":
    main()
