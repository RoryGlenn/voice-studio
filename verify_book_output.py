"""Independently inspect the completed audiobook files, tags, playlists and cache coverage."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from mutagen.id3 import ID3

from project_paths import workspace_root
from render_book import compare, composition_digest

ROOT = workspace_root()
JOB = ROOT / "work/own_voice/book"
OUT = ROOT / "outputs/Thinking in Systems - Your Voice"


def main() -> None:
    """Require all planned audio and navigation assets before reporting completion.

    Returns
    -------
    None
        Saves a final validation report or raises on the first defect.
    """
    plan = json.loads((JOB / "plan.json").read_text())
    rows = json.loads((JOB / "completed_tracks.json").read_text())
    rows.sort(key=lambda row: row["track"])
    assert [r["track"] for r in rows] == list(range(1, 31)), (
        "Missing or duplicate tracks"
    )
    expected_files = [r["file"] for r in rows]
    assert sorted(p.relative_to(OUT).as_posix() for p in OUT.rglob("*.mp3")) == sorted(
        expected_files
    ), "Unexpected audio inventory"
    playlist = [
        line
        for line in (OUT / "Play all.m3u8").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert playlist == expected_files, "Whole-book playlist order differs"
    total_passages, recognized_differences = 0, 0
    details = []
    for track, row in zip(plan["tracks"], rows, strict=True):
        assert row["identity"] == plan["identity_sha256"], "Wrong rendering provenance"
        file = OUT / row["file"]
        assert hashlib.sha256(file.read_bytes()).hexdigest() == row["sha256"], (
            "Changed MP3"
        )
        tags = ID3(file)
        assert str(tags["TRCK"]) == f"{row['track']}/30", "Wrong track number"
        assert str(tags["TIT2"]) == track["title"], "Wrong track title"
        assert str(tags["TPOS"]) == f"{track['disc']}/9", "Wrong chapter number"
        assert "AI-generated" in str(tags["TXXX:Narration"]), (
            "Missing AI narration metadata"
        )
        info = json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_name,sample_rate,channels",
                    "-of",
                    "json",
                    str(file),
                ]
            )
        )
        audio = info["streams"]
        assert len(audio) == 1 and audio[0] == {
            "codec_name": "mp3",
            "sample_rate": "24000",
            "channels": 1,
        }, "Unexpected audio format"
        assert (
            abs(float(info["format"]["duration"]) - row["duration_seconds"]) < 0.03
        ), "Duration mismatch"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", str(file), "-f", "null", "-"],
            check=True,
        )
        records = []
        for segment in track["segments"]:
            checkpoint = (
                JOB
                / "segments"
                / f"{track['track']:02d}"
                / f"{segment['number']:04d}-{segment['text_sha256'][:10]}.json"
            )
            record = json.loads(checkpoint.read_text())
            assert (
                record["status"] == "verified"
                and record["identity"] == plan["identity_sha256"]
            ), "Unchecked or stale passage"
            assert record["text"] == segment["text"], "Passage text differs from plan"
            assert (
                hashlib.sha256(Path(record["file"]).read_bytes()).hexdigest()
                == record["audio_sha256"]
            ), "Changed passage audio"
            final_check = record["strong_check"] or record["tiny_check"]
            assert not final_check["flagged"], "Unresolved ASR review"
            assert not compare(
                record["text"],
                final_check["recognized"],
                strict=record.get("strict_review", False),
            )["flagged"], "Current verification policy requires a passage review"
            recognized_differences += bool(final_check["changes"])
            total_passages += 1
            records.append(record)
        assert row["composition_sha256"] == composition_digest(track, records), (
            "Export contains stale or reordered passage audio"
        )
        expected_duration = sum(
            record["duration_seconds"] + segment["pause_seconds"]
            for segment, record in zip(track["segments"], records, strict=True)
        )
        assert abs(float(info["format"]["duration"]) - expected_duration) < 0.03, (
            "Export duration differs from current passage composition"
        )
        details.append(
            {
                "track": row["track"],
                "file": row["file"],
                "duration_seconds": row["duration_seconds"],
                "decode": "pass",
            }
        )
    for folder in sorted({r["folder"] for r in rows}):
        expected = [Path(r["file"]).name for r in rows if r["folder"] == folder]
        actual = [
            line
            for line in (OUT / folder / "Play in order.m3u8").read_text().splitlines()
            if line and not line.startswith("#")
        ]
        assert actual == expected, "Chapter playlist order differs"
    assert total_passages == plan["total_segments"], "Incomplete passage coverage"
    report = {
        "result": "pass",
        "tracks": 30,
        "passages": total_passages,
        "duration_seconds": sum(row["duration_seconds"] for row in rows),
        "passages_with_minor_asr_differences": recognized_differences,
        "note": "Minor word-recognition differences can remain. Automated checks do not guarantee perfect pronunciation or voice likeness.",
        "files": details,
    }
    (JOB / "final_verification.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "files"}), flush=True)


if __name__ == "__main__":
    main()
