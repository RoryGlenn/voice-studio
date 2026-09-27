"""Package a clearly labeled draft from checked audiobook passages."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from mutagen.mp4 import MP4, MP4Cover

import runtime
from audiobook import load_plan, record_path, validate_record
from book_pacing import RATE, edge_quiet
from book_package import claim_output, export_track, has_quicktime_chapters
from doctor import file_hash
from render_book import save_json


def draft_record(record: dict[str, Any]) -> dict[str, Any]:
    """Use raw audio for a held passage without changing its saved checkpoint."""
    if record["status"] == "verified":
        return record
    if record["status"] != "needs_review":
        raise ValueError("All passages must be checked before a draft export")
    if file_hash(Path(record["asr_file"])) != record["asr_sha256"]:
        raise ValueError("Recognition evidence changed")
    wave, rate = sf.read(record["raw_file"], dtype="float32")
    if rate != RATE or len(wave) != record["signal_metrics"]["frames"]:
        raise ValueError("Raw audio changed")
    return {
        **record,
        "status": "verified",  # Required only by export_track; never saved as a checkpoint.
        "paced_file": record["raw_file"],
        "paced_sha256": record["raw_sha256"],
        "paced_frames": len(wave),
        "leading_quiet_frames": edge_quiet(wave),
        "trailing_quiet_frames": edge_quiet(wave, True),
        "peak": max(float(np.max(np.abs(wave))), 1e-6),
    }


def finish_draft(job: Path, output: Path) -> dict[str, Any]:
    """Export complete audio while retaining explicit review status."""
    plan = load_plan(job)
    checked: list[list[dict[str, Any]]] = []
    held: list[str] = []
    for track in plan["tracks"]:
        rows = []
        for segment in track["segments"]:
            record = json.loads(record_path(track, segment, job).read_text())
            validate_record(record, track, segment, plan)
            if record["status"] == "needs_review":
                held.append(f"{track['track']}/{segment['number']}")
            rows.append(draft_record(record))
        checked.append(rows)

    claim_output(output, plan)
    outputs = []
    for index, (track, records) in enumerate(zip(plan["tracks"], checked, strict=True)):
        following = (
            (plan["tracks"][index + 1]["segments"][0], checked[index + 1][0])
            if index + 1 < len(checked)
            else None
        )
        outputs.append(export_track(track, records, plan, following, job, output))

    package = job / "draft_package"
    package.mkdir(exist_ok=True)
    concat = ["ffconcat version 1.0"]
    chapters = [";FFMETADATA1"]
    cursor = 0
    for row in outputs:
        link = package / f"{row['track']:02d}.wav"
        if not link.exists():
            link.symlink_to(row["wav"])
        elif link.resolve() != Path(row["wav"]).resolve():
            raise ValueError("Draft package link points to another audio file")
        concat.append(f"file '{link.name}'")
        title = re.sub(r"([\\=;#])", r"\\\1", row["title"])
        chapters.extend(
            [
                "[CHAPTER]",
                "TIMEBASE=1/24000",
                f"START={cursor}",
                f"END={cursor + row['frames']}",
                f"title={title}",
            ]
        )
        cursor += row["frames"]
    (package / "concat.txt").write_text("\n".join(concat) + "\n")
    (package / "chapters.ffmeta").write_text("\n".join(chapters) + "\n")
    partial = output / ".audiobook.partial.m4b"
    subprocess.run(
        [
            runtime.binary("ffmpeg"),
            "-v",
            "error",
            "-nostdin",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(package / "concat.txt"),
            "-i",
            str(package / "chapters.ffmeta"),
            "-map",
            "0:a",
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            "-y",
            str(partial),
        ],
        check=True,
    )
    tags = MP4(partial)
    tags["©nam"] = [plan["title"] + " (Draft)"]
    tags["©alb"] = [plan["title"]]
    tags["©ART"] = [plan["author"]]
    tags["aART"] = [plan["narrator"]]
    tags["©gen"] = ["Audiobook"]
    tags["desc"] = [
        f"Draft AI narration. {len(held)} passages are held for review; see draft_report.json."
    ]
    tags["stik"] = [2]
    tags["covr"] = [
        MP4Cover(
            Path(plan["cover"]["path"]).read_bytes(), imageformat=MP4Cover.FORMAT_PNG
        )
    ]
    tags.save()
    readback = MP4(partial)
    if readback["©nam"] != [plan["title"] + " (Draft)"]:
        raise ValueError("Draft title metadata did not survive packaging")
    if (
        hashlib.sha256(bytes(readback["covr"][0])).hexdigest()
        != plan["cover"]["sha256"]
    ):
        raise ValueError("Draft cover metadata did not survive packaging")
    probe = json.loads(
        subprocess.check_output(
            [
                runtime.binary("ffprobe"),
                "-v",
                "error",
                "-show_chapters",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(partial),
            ]
        )
    )
    streams = [s for s in probe["streams"] if s.get("codec_type") == "audio"]
    if (
        len(streams) != 1
        or streams[0]["codec_name"] != "aac"
        or streams[0]["profile"] != "LC"
    ):
        raise ValueError("Draft audio codec is incorrect")
    if len(probe["chapters"]) != len(outputs) or not has_quicktime_chapters(partial):
        raise ValueError("Draft chapter metadata is incomplete")
    if abs(float(probe["format"]["duration"]) - cursor / RATE) >= 0.2:
        raise ValueError("Draft duration differs from chapter audio")
    for chapter, row in zip(probe["chapters"], outputs, strict=True):
        if chapter["tags"]["title"] != row["title"]:
            raise ValueError("Draft chapter title differs from source plan")
    subprocess.run(
        [
            runtime.binary("ffmpeg"),
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(partial),
            "-f",
            "null",
            "-",
        ],
        check=True,
    )
    final = output / "audiobook.m4b"
    partial.replace(final)
    (output / "Listen in order.m3u8").write_text(
        "#EXTM3U\n" + "\n".join(Path(row["mp3"]).name for row in outputs) + "\n"
    )
    report = {
        "state": "draft",
        "identity": plan["identity_sha256"],
        "total_passages": plan["total_segments"],
        "held_for_review": len(held),
        "held_passages": held,
        "chapters": len(outputs),
        "duration_seconds": cursor / RATE,
        "m4b": str(final),
        "m4b_sha256": file_hash(final),
        "full_audio_decode": "pass",
        "note": "Held passages use original generated audio; wording and timing are not verified.",
        "tracks": outputs,
    }
    save_json(output / "draft_report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = finish_draft(
        args.job.expanduser().resolve(), args.output.expanduser().resolve()
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in {"tracks", "held_passages"}},
            indent=2,
        )
    )
