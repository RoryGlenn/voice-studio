"""Verify chapter WAV/MP3 and AAC-LC M4B packaging from accepted passages."""

from __future__ import annotations

import hashlib
import json
import re
import struct
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from voice_studio import runtime
from voice_studio.book_pacing import RATE
from voice_studio.doctor import file_hash as digest
from voice_studio.render_book import save_json as save


def chapter_stem(number: int, title: str) -> str:
    """Return a readable filename stem bounded by UTF-8 bytes, preserving its track ID."""
    prefix = f"{number:02d} - "
    safe_title = re.sub(r'[\x00-\x1f\x7f/:?"<>|*\\]', "", title)
    stem = prefix + safe_title
    # Reserve the longest media suffix plus a possible leading dot, including
    # temporary names, within the 255-byte component limit on supported hosts.
    budget = 255 - len(".partial.wav".encode("utf-8")) - len(".".encode("utf-8"))
    if len(stem.encode("utf-8")) <= budget:
        return stem
    suffix = "-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]
    title_budget = budget - len(prefix.encode("utf-8")) - len(suffix.encode("utf-8"))
    if title_budget < 0:
        raise ValueError("Track identifier exceeds the filename component limit")
    truncated = safe_title.encode("utf-8")[:title_budget].decode(
        "utf-8", errors="ignore"
    )
    return prefix + truncated + suffix


def claim_output(output: Path, plan: dict[str, Any]) -> None:
    """Require a new directory or the same book's prior ownership before writes."""
    output.mkdir(parents=True, exist_ok=True)
    owner = output / ".voice-studio-job.json"
    if owner.exists():
        if (
            owner.is_symlink()
            or json.loads(owner.read_text()).get("identity") != plan["identity_sha256"]
        ):
            raise ValueError("Output directory belongs to another audiobook job")
    else:
        if any(output.iterdir()):
            raise ValueError(
                "Output directory contains unrelated files; choose a new empty directory"
            )
        with owner.open("x") as target:
            json.dump({"identity": plan["identity_sha256"]}, target)
    if any(path.is_symlink() for path in output.iterdir()):
        raise ValueError(
            "Output directory contains symlinks; refusing to overwrite external files"
        )


def has_quicktime_chapters(path: Path) -> bool:
    """Check MP4 track references for an Apple-compatible chapter track.

    Parameters
    ----------
    path : Path
        Finished MP4 container.
    """
    with path.open("rb") as stream:

        def boxes(start: int, end: int) -> list[tuple[bytes, int, int]]:
            result = []
            offset = start
            while offset + 8 <= end:
                stream.seek(offset)
                size, kind = struct.unpack(">I4s", stream.read(8))
                header = 8
                if size == 1:
                    size = struct.unpack(">Q", stream.read(8))[0]
                    header = 16
                elif size == 0:
                    size = end - offset
                if size < header or offset + size > end:
                    raise ValueError("Invalid MP4 atom")
                result.append((kind, offset + header, offset + size))
                offset += size
            return result

        for kind, start, end in boxes(0, path.stat().st_size):
            if kind != b"moov":
                continue
            for kind, start, end in boxes(start, end):
                if kind != b"trak":
                    continue
                for kind, start, end in boxes(start, end):
                    if kind == b"tref" and any(
                        (k == b"chap" and b - a >= 4 for k, a, b in boxes(start, end))
                    ):
                        return True
    return False


def gap_target(
    left: dict[str, Any], right: dict[str, Any], plan: dict[str, Any]
) -> tuple[str, float]:
    """Choose structural timing without promoting sentence chunks to paragraphs.

    Parameters
    ----------
    left, right : dict
        Neighboring source passages.
    plan : dict
        Approved quiet-duration targets.
    """
    if right["unit"] == left["unit"] and left.get("track") == right.get("track"):
        return ("same_paragraph_chunk", 0.0)
    kind = (
        "paragraph_to_heading"
        if right["kind"] == "heading"
        else "heading_to_paragraph"
        if left["kind"] == "heading"
        else "paragraph_to_paragraph"
    )
    return (kind, plan["pause_seconds"][kind])


def export_track(
    track: dict[str, Any],
    records: list[dict[str, Any]],
    plan: dict[str, Any],
    following: tuple[dict[str, Any], dict[str, Any]] | None,
    job: Path,
    output: Path,
) -> dict[str, Any]:
    """Assemble a verified chapter with measured native-edge silence.

    Parameters
    ----------
    track, records, plan : dict or list
        Source and verified passage evidence.
    following : tuple, optional
        Next track's first source passage and recording, for cross-track timing.
    """
    from mutagen.id3 import APIC, ID3, TALB, TCON, TIT2, TPE1, TPE2, TRCK, TXXX

    output.mkdir(parents=True, exist_ok=True)
    stem = chapter_stem(track["track"], track["title"])
    wav = output / f"{stem}.wav"
    mp3 = output / f"{stem}.mp3"
    temporary = output / f".chapter-{track['track']:04d}.partial.wav"
    gain = min(1.0, 0.95 / max((r["peak"] for r in records)))
    frames = 0
    boundaries = []
    with sf.SoundFile(
        temporary, "w", samplerate=RATE, channels=1, subtype="PCM_24"
    ) as target:
        for index, (segment, record) in enumerate(
            zip(track["segments"], records, strict=True)
        ):
            if not (
                record["status"] == "verified"
                and record["identity"] == plan["identity_sha256"]
            ):
                raise ValueError("Waveform conservation check failed")
            if not digest(Path(record["paced_file"])) == record["paced_sha256"]:
                raise ValueError("Waveform conservation check failed")
            wave, rate = sf.read(record["paced_file"], dtype="float32")
            if not (rate == RATE and len(wave) == record["paced_frames"]):
                raise ValueError("Waveform conservation check failed")
            target.write(wave * gain)
            frames += len(wave)
            next_pair = (
                (track["segments"][index + 1], records[index + 1])
                if index + 1 < len(records)
                else following
            )
            if next_pair:
                next_segment, next_record = next_pair
                left = dict(segment, track=track["track"])
                right = dict(
                    next_segment,
                    track=track["track"]
                    if index + 1 < len(records)
                    else track["track"] + 1,
                )
                kind, seconds = gap_target(left, right, plan)
                native = (
                    record["trailing_quiet_frames"]
                    + next_record["leading_quiet_frames"]
                )
                added = max(0, round(seconds * RATE) - native)
                target.write(np.zeros(added, dtype=np.float32))
                boundaries.append(
                    {
                        "after_segment": segment["number"],
                        "kind": kind,
                        "target_seconds": seconds,
                        "native_frames": native,
                        "inserted_frames": added,
                        "result_seconds": (native + added) / RATE,
                    }
                )
                frames += added
    if not sf.info(temporary).frames == frames:
        raise ValueError("Waveform conservation check failed")
    with sf.SoundFile(temporary) as source:
        for index, record in enumerate(records):
            expected, _ = sf.read(record["paced_file"], dtype="float32")
            actual = source.read(len(expected), dtype="float32")
            if not np.max(np.abs(actual - expected * gain)) < 2e-07:
                raise ValueError("Waveform conservation check failed")
            if index < len(boundaries):
                zeros = source.read(
                    boundaries[index]["inserted_frames"], dtype="float32"
                )
                if not not np.count_nonzero(zeros):
                    raise ValueError("Waveform conservation check failed")
        if not len(source.read(1)) == 0:
            raise ValueError("Waveform conservation check failed")
    temporary.replace(wav)
    subprocess.run(
        [
            runtime.binary("ffmpeg"),
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(wav),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-y",
            str(mp3),
        ],
        check=True,
    )
    tags = ID3(mp3)
    for tag in [
        TIT2(encoding=3, text=track["title"]),
        TALB(encoding=3, text=plan["title"] + " — narrated by " + plan["narrator"]),
        TPE1(encoding=3, text=plan["author"]),
        TPE2(encoding=3, text=plan["narrator"]),
        TRCK(encoding=3, text=f"{track['track']}/{len(plan['tracks'])}"),
        TCON(encoding=3, text="Audiobook"),
        TXXX(
            encoding=3,
            desc="Narration",
            text="AI-generated narration with a saved voice",
        ),
        APIC(
            encoding=3,
            mime="image/png",
            type=3,
            desc="Audiobook cover",
            data=Path(plan["cover"]["path"]).read_bytes(),
        ),
    ]:
        tags.add(tag)
    tags.save(mp3, v2_version=3)
    subprocess.run(
        [
            runtime.binary("ffmpeg"),
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(mp3),
            "-f",
            "null",
            "-",
        ],
        check=True,
    )
    result = {
        "track": track["track"],
        "title": track["title"],
        "wav": str(wav),
        "mp3": str(mp3),
        "wav_sha256": digest(wav),
        "mp3_sha256": digest(mp3),
        "frames": frames,
        "duration_seconds": frames / RATE,
        "boundaries": boundaries,
        "gain": gain,
        "waveform_conservation": True,
        "identity": plan["identity_sha256"],
        "composition": [
            {"raw_sha256": r["raw_sha256"], "paced_sha256": r["paced_sha256"]}
            for r in records
        ],
    }
    save(job / "exports" / f"{track['track']:02d}.json", result)
    return result


def finish(plan: dict[str, Any], job: Path, output: Path) -> dict[str, Any]:
    """Export all verified audio and validate an Apple Books package.

    Parameters
    ----------
    plan : dict
        Frozen full-book source and generation identity.
    """
    from mutagen.mp4 import MP4, MP4Cover

    from voice_studio.audiobook import record_path, validate_record

    all_records = [
        [json.loads(record_path(t, s, job).read_text()) for s in t["segments"]]
        for t in plan["tracks"]
    ]
    for track, records in zip(plan["tracks"], all_records, strict=True):
        for segment, record in zip(track["segments"], records, strict=True):
            validate_record(record, track, segment, plan)
    if any((r["status"] != "verified" for rows in all_records for r in rows)):
        raise ValueError("Unresolved wording or pacing review; packaging is blocked")
    claim_output(output, plan)
    outputs = []
    for index, (track, records) in enumerate(
        zip(plan["tracks"], all_records, strict=True)
    ):
        following = (
            (plan["tracks"][index + 1]["segments"][0], all_records[index + 1][0])
            if index + 1 < len(all_records)
            else None
        )
        outputs.append(export_track(track, records, plan, following, job, output))
    concat = ["ffconcat version 1.0"]
    metadata = [";FFMETADATA1"]
    cursor = 0
    for row in outputs:
        link = job / "package" / f"{row['track']:02d}.wav"
        link.parent.mkdir(exist_ok=True)
        if not link.is_symlink() and not link.exists():
            link.symlink_to(row["wav"])
        elif not link.resolve() == Path(row["wav"]).resolve():
            raise ValueError("Waveform conservation check failed")
        concat.append(f"file '{link.name}'")
        title = re.sub("([\\\\=;#])", "\\\\\\1", row["title"])
        metadata.extend(
            [
                "[CHAPTER]",
                "TIMEBASE=1/24000",
                f"START={cursor}",
                f"END={cursor + row['frames']}",
                f"title={title}",
            ]
        )
        cursor += row["frames"]
    (job / "package/concat.txt").write_text("\n".join(concat) + "\n")
    (job / "package/chapters.ffmeta").write_text("\n".join(metadata) + "\n")
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
            str(job / "package/concat.txt"),
            "-i",
            str(job / "package/chapters.ffmeta"),
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
    tags["©nam"] = [plan["title"]]
    tags["©alb"] = [plan["title"]]
    tags["©ART"] = [plan["author"]]
    tags["aART"] = [plan["narrator"]]
    tags["©gen"] = ["Audiobook"]
    tags["©day"] = [plan["year"]]
    tags["desc"] = ["AI-generated narration using a saved voice. " + plan["scope"]]
    tags["stik"] = [2]
    tags["covr"] = [
        MP4Cover(
            Path(plan["cover"]["path"]).read_bytes(), imageformat=MP4Cover.FORMAT_PNG
        )
    ]
    tags.save()
    readback = MP4(partial)
    if not (readback["stik"] == [2] and readback["©ART"] == [plan["author"]]):
        raise ValueError("Waveform conservation check failed")
    if (
        not hashlib.sha256(bytes(readback["covr"][0])).hexdigest()
        == plan["cover"]["sha256"]
    ):
        raise ValueError("Waveform conservation check failed")
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
    audio_streams = [
        stream for stream in probe["streams"] if stream.get("codec_type") == "audio"
    ]
    if not len(audio_streams) == 1:
        raise ValueError("Waveform conservation check failed")
    if not (
        audio_streams[0]["codec_name"] == "aac" and audio_streams[0]["profile"] == "LC"
    ):
        raise ValueError("Waveform conservation check failed")
    if not (
        audio_streams[0]["sample_rate"] == str(RATE)
        and audio_streams[0]["channels"] == 1
    ):
        raise ValueError("Waveform conservation check failed")
    for key, expected in {
        "©nam": plan["title"],
        "©alb": plan["title"],
        "©ART": plan["author"],
        "aART": plan["narrator"],
        "©gen": "Audiobook",
        "©day": plan["year"],
    }.items():
        if not readback[key] == [expected]:
            raise ValueError(key)
    chapters = probe["chapters"]
    if not len(chapters) == len(outputs):
        raise ValueError("Waveform conservation check failed")
    elapsed = 0
    for chapter, row in zip(chapters, outputs, strict=True):
        if not chapter["tags"]["title"] == row["title"]:
            raise ValueError("Waveform conservation check failed")
        if not abs(float(chapter["start_time"]) - elapsed / RATE) < 0.02:
            raise ValueError("Waveform conservation check failed")
        elapsed += row["frames"]
        if not abs(float(chapter["end_time"]) - elapsed / RATE) < 0.02:
            raise ValueError("Waveform conservation check failed")
    if not abs(float(probe["format"]["duration"]) - cursor / RATE) < 0.2:
        raise ValueError("Waveform conservation check failed")
    if not has_quicktime_chapters(partial):
        raise ValueError("Missing QuickTime chapter reference")
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
        "#EXTM3U\n" + "\n".join((Path(r["mp3"]).name for r in outputs)) + "\n"
    )
    report = {
        "state": "complete",
        "identity": plan["identity_sha256"],
        "narration_standard_sha256": plan["identity"]["narration_standard_sha256"],
        "checked_passages": sum(map(len, all_records)),
        "chapters": len(chapters),
        "duration_seconds": cursor / RATE,
        "m4b": str(final),
        "m4b_sha256": digest(final),
        "cover_verified": True,
        "quicktime_chapters_verified": True,
        "full_audio_decode": "pass",
        "waveform_conservation": "pass",
        "dash_boundaries": sum(
            (len(r["dash_insertions"]) for rows in all_records for r in rows)
        ),
        "human_listening": "Not a complete human listening review",
        "tracks": outputs,
    }
    if not report["dash_boundaries"] == plan["sentence_dash_count"]:
        raise ValueError("Waveform conservation check failed")
    save(output / "verification.json", report)
    save(job / "final_verification.json", report)
    return report
