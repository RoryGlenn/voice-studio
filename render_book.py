"""Render and automatically check the accepted 30-track audiobook voice."""

from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import hashlib
import json
import re
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from project_paths import workspace_root

ROOT = workspace_root()
JOB = ROOT / "work/own_voice/book"
OUTPUT = ROOT / "outputs/Thinking in Systems - Your Voice"
SOURCE = ROOT / "work/narration_text/spoken_manifest.json"
REFERENCE = ROOT / "work/own_voice/male_reference_extended.wav"
MODEL = ROOT / "work/own_voice/models/chatterbox-turbo-4bit"
MODEL_REVISION = "c63817725071d7b5269c7b558772d6e8cbf59cec"
MODEL_FILES_SHA256: dict[str, str] | None = None
VERSION = 1
ALBUM = "Thinking in Systems - Your Voice"
PEAK_ONLY = False
PARAGRAPH_PAUSE = 0.4
PASSAGE_PAUSE = 0.16


def digest(value: str | bytes) -> str:
    """Return a stable SHA-256 fingerprint for text or bytes."""
    return hashlib.sha256(
        value.encode() if isinstance(value, str) else value
    ).hexdigest()


def save_json(path: Path, value: Any) -> None:
    """Atomically save a JSON checkpoint without truncating the previous one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    temporary.replace(path)


def save_checkpoint(
    path: Path, original: dict[str, Any], updated: dict[str, Any]
) -> None:
    """Reject a stale helper result if another operation changed its checkpoint."""
    if json.loads(path.read_text()) != original:
        raise RuntimeError(
            f"Checkpoint changed during review; preserving newer state: {path}"
        )
    save_json(path, updated)


def lexical(text: str) -> list[str]:
    """Return source words and numbers for lossless chunk-plan validation."""
    return re.findall(r"\w+", text.casefold(), re.UNICODE)


def split_long(text: str, char_limit: int = 400, word_limit: int = 60) -> list[str]:
    """Split oversized prose at a clause or word boundary, retaining every word."""
    remaining = text.strip()
    pieces = []
    while len(remaining) > char_limit or len(remaining.split()) > word_limit:
        words = list(re.finditer(r"\S+", remaining))
        maximum = min(
            char_limit,
            words[word_limit - 1].end() if len(words) >= word_limit else len(remaining),
        )
        boundaries = [
            m.end() for m in re.finditer(r"[,;:—–]\s+", remaining[: maximum + 1])
        ]
        meaningful = [x for x in boundaries if x >= maximum * 0.55]
        if meaningful:
            cut = meaningful[-1]
        else:
            spaces = [m.start() for m in re.finditer(r"\s+", remaining[: maximum + 1])]
            if not spaces:
                raise ValueError(f"Unbreakable overlong token: {remaining[:60]}")
            cut = spaces[-1]
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    if lexical(" ".join(pieces)) != lexical(text):
        raise ValueError("Chunk splitting lost or reordered words")
    return pieces


def build_plan() -> dict[str, Any]:
    """Build bounded speech passages and verify conservation of all source words."""
    import spacy

    nlp = spacy.blank("en")
    nlp.add_pipe("sentencizer")
    source = json.loads(SOURCE.read_text())
    headings = json.loads((ROOT / "work/book_subsections.json").read_text())
    heading_words = {
        tuple(lexical(h["title"]))
        for chapter in headings["chapters"]
        for h in chapter["headings"]
    }
    heading_words |= {
        tuple(lexical(chapter["chapter_title"])) for chapter in headings["chapters"]
    }
    tracks = []
    for track in source["tracks"]:
        raw = Path(track["text_path"]).read_text()
        if digest(raw) != track["sha256"]:
            raise ValueError(f"Source fingerprint changed: track {track['track']}")
        paragraphs = re.split(r"\n\s*\n", raw.strip())
        segments: list[dict[str, Any]] = []
        pending = ""
        pending_paragraphs: list[int] = []
        pending_heading = False

        def flush() -> None:
            nonlocal pending, pending_paragraphs, pending_heading
            if pending:
                segments.append(
                    {"text": pending, "paragraphs": sorted(set(pending_paragraphs))}
                )
            pending, pending_paragraphs = "", []
            pending_heading = False

        for paragraph_index, paragraph in enumerate(paragraphs):
            paragraph = " ".join(paragraph.split())
            is_heading = tuple(lexical(paragraph)) in heading_words or (
                paragraph.isupper()
                and len(paragraph.split()) <= 15
                and not paragraph.startswith("—")
            )
            if (
                pending
                and not pending_heading
                and (is_heading or len(pending.split()) >= 12)
            ):
                flush()
            if paragraph and paragraph[-1] not in ".!?;:—–":
                paragraph += "."
            for sentence in nlp(paragraph).sents:
                pieces = deque(split_long(sentence.text))
                while pieces:
                    piece = pieces.popleft()
                    combined = f"{pending} {piece}".strip()
                    if pending and (len(combined) > 400 or len(combined.split()) > 60):
                        if (
                            pending_heading
                            and 400 - len(pending) - 1 >= 80
                            and 60 - len(pending.split()) >= 10
                        ):
                            smaller = split_long(
                                piece, 400 - len(pending) - 1, 60 - len(pending.split())
                            )
                            piece = smaller[0]
                            pieces.extendleft(reversed(smaller[1:]))
                        else:
                            flush()
                    pending = f"{pending} {piece}".strip()
                    pending_paragraphs.append(paragraph_index)
                    pending_heading = is_heading
        flush()
        # Keep short headings and attributions with adjacent prose.
        position = 0
        while position < len(segments):
            item = segments[position]
            if len(item["text"].split()) < 8 and len(segments) > 1:
                candidates = (
                    [position + 1] if position + 1 < len(segments) else []
                ) + ([position - 1] if position else [])
                merged = False
                for neighbor in candidates:
                    lo, hi = sorted((position, neighbor))
                    combined = segments[lo]["text"] + " " + segments[hi]["text"]
                    if len(combined) <= 420 and len(combined.split()) <= 65:
                        segments[lo] = {
                            "text": combined,
                            "paragraphs": sorted(
                                set(
                                    segments[lo]["paragraphs"]
                                    + segments[hi]["paragraphs"]
                                )
                            ),
                        }
                        segments.pop(hi)
                        position = max(0, lo - 1)
                        merged = True
                        break
                if merged:
                    continue
            position += 1
        if lexical(" ".join(s["text"] for s in segments)) != lexical(raw):
            raise ValueError(f"Source word conservation failed: track {track['track']}")
        for number, segment in enumerate(segments, 1):
            segment.update(
                {
                    "number": number,
                    "text_sha256": digest(segment["text"]),
                    "word_count": len(segment["text"].split()),
                    "pause_seconds": PARAGRAPH_PAUSE
                    if number == len(segments)
                    or segment["paragraphs"][-1] != segments[number]["paragraphs"][0]
                    else PASSAGE_PAUSE,
                }
            )
        tracks.append(
            {
                k: track[k]
                for k in (
                    "track",
                    "chapter",
                    "disc",
                    "folder",
                    "title",
                    "included_sections",
                )
            }
            | {"source_text_sha256": digest(raw), "segments": segments}
        )
    identity = {
        "renderer_version": VERSION,
        "source_manifest_sha256": digest(SOURCE.read_bytes()),
        "chunk_plan_sha256": digest(
            json.dumps(
                [[s["text"] for s in t["segments"]] for t in tracks], ensure_ascii=False
            )
        ),
        "voice_reference_sha256": digest(REFERENCE.read_bytes()),
        "model_revision": MODEL_REVISION,
        "generation": {
            "temperature": 0.8,
            "top_p": 0.95,
            "repetition_penalty": 1.2,
            "max_tokens": 1200,
        },
        "postprocessing": "loudness normalization only; mono24kHz128kbpsMP3",
    }
    if MODEL_FILES_SHA256 is not None:
        identity["model_files_sha256"] = dict(MODEL_FILES_SHA256)
    if PEAK_ONLY:
        identity["postprocessing"] = (
            "Per-passage peak attenuation only to 0.95; speed1; mono24kHz128kbpsMP3"
        )
    if PEAK_ONLY or (PARAGRAPH_PAUSE, PASSAGE_PAUSE) != (0.4, 0.16):
        identity["pauses_seconds"] = {
            "paragraph": PARAGRAPH_PAUSE,
            "passage": PASSAGE_PAUSE,
        }
        identity["seed_strategy"] = "20260905 + track*10000 + segment + attempt*1000000"
    return {
        "identity": identity,
        "identity_sha256": digest(json.dumps(identity, sort_keys=True)),
        "tracks": tracks,
        "total_segments": sum(len(t["segments"]) for t in tracks),
    }


def compare(expected: str, actual: str, strict: bool = False) -> dict[str, Any]:
    """Flag substantial ASR discrepancies, preserving parenthetical speech.

    Parameters
    ----------
    expected, actual : str
        Intended speech and independent recognized speech.

    Returns
    -------
    dict
        Review flags and every normalized word difference; not a listening verdict.
    """
    from transformers.models.whisper.english_normalizer import EnglishTextNormalizer

    normalizer = EnglishTextNormalizer({})

    def normalized(value: str) -> list[str]:
        value = re.sub(r"[\[\]()<>]", " ", value.replace("’", "'"))
        value = re.sub(r"\bcannot\b", "can not", value, flags=re.IGNORECASE)
        value = re.sub(r"(?<!\w)[−–-](?=\d)", "negative ", value)
        for word, ordinal, number in (
            ("third", "3rd", 3),
            ("sixth", "6th", 6),
            ("eighth", "8th", 8),
        ):
            value = re.sub(
                r"\b(?:one|1)[ -]+(?:" + word + "|" + ordinal + r")\b",
                f"one over {number}",
                value,
                flags=re.IGNORECASE,
            )
        value = re.sub(
            r"\bratio of (\d+)\s*:\s*(\d+)\b",
            r"ratio of \1 to \2",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(r"\bvol\b\.?", "volume", value, flags=re.IGNORECASE)
        value = normalizer(value)
        for initialism in (
            "hiv",
            "aids",
            "mit",
            "gdp",
            "gnp",
            "ai",
            "us",
            "usa",
            "nasa",
            "dna",
            "ddt",
            "pcb",
            "cfc",
        ):
            value = re.sub(r"\b" + r"\s+".join(initialism) + r"\b", initialism, value)
        return [
            token for token in value.split() if not re.fullmatch(r"[.,;:!?…]+", token)
        ]

    left, right = normalized(expected), normalized(actual)
    changes, errors, longest, numeric, semantic, boundary, extra_words = (
        [],
        0,
        0,
        False,
        False,
        False,
        False,
    )
    for kind, a, b, c, d in difflib.SequenceMatcher(
        None, left, right, autojunk=False
    ).get_opcodes():
        if kind == "equal":
            continue
        n = max(b - a, d - c)
        errors += n
        longest = max(longest, n)
        changed_left, changed_right = left[a:b], right[c:d]
        same_letters = "".join(changed_left) == "".join(changed_right)
        numeric |= not same_letters and any(
            re.search(r"\d", w) or w in {"one", "-one", "+one"}
            for w in changed_left + changed_right
        )
        semantic |= not same_letters and bool(
            {
                "not",
                "no",
                "never",
                "without",
                "positive",
                "negative",
                "appreciation",
                "depreciation",
                "inflow",
                "outflow",
                "inflows",
                "outflows",
                "increase",
                "decrease",
                "increasing",
                "decreasing",
                "grow",
                "growing",
                "growth",
                "decline",
                "declining",
                "rise",
                "rising",
                "fall",
                "falling",
                "more",
                "less",
                "higher",
                "lower",
                "faster",
                "slower",
            }
            & set(changed_left + changed_right)
        )
        boundary |= kind in {"delete", "insert"} and (a == 0 or b == len(left))
        extra_words |= kind == "insert" and n >= 2
        changes.append(
            {
                "kind": kind,
                "expected": " ".join(changed_left),
                "recognized": " ".join(changed_right),
                "expected_start": a,
                "expected_end": b,
            }
        )
    rate = errors / max(len(left), 1)
    strict_difference = strict and any(
        c["expected"].replace(" ", "") != c["recognized"].replace(" ", "")
        for c in changes
    )
    return {
        "flagged": longest >= 3
        or rate > 0.20
        or numeric
        or semantic
        or boundary
        or extra_words
        or strict_difference
        or not right,
        "error_rate": rate,
        "longest_changed_span": longest,
        "numeric_discrepancy": numeric,
        "semantic_discrepancy": semantic,
        "boundary_deletion": boundary,
        "extra_words": extra_words,
        "strict_difference": strict_difference,
        "changes": changes,
        "recognized": actual,
    }


def recognize(path: Path, model_path: Path) -> tuple[str, float]:
    """Transcribe an original generated passage without prompting expected text."""
    import mlx_whisper

    started = time.monotonic()
    result = mlx_whisper.transcribe(
        str(path),
        path_or_hf_repo=str(model_path),
        language="en",
        task="transcribe",
        temperature=0.0,
        condition_on_previous_text=False,
        initial_prompt=None,
        word_timestamps=False,
        fp16=True,
        verbose=None,
    )
    return result["text"], time.monotonic() - started


def render_segment(
    model: Any, track: dict[str, Any], segment: dict[str, Any], identity: str
) -> dict[str, Any]:
    """Render a resumable passage and adjudicate major discrepancies before reuse."""
    folder = JOB / "segments" / f"{track['track']:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{segment['number']:04d}-{segment['text_sha256'][:10]}"
    checkpoint = folder / f"{stem}.json"
    review_metadata: dict[str, Any] = {}
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        review_metadata = {
            key: saved[key] for key in ("strict_review", "review_note") if key in saved
        }
        audio_path = Path(saved["file"])
        if (
            saved.get("identity") != identity
            or saved["text_sha256"] != segment["text_sha256"]
        ):
            raise ValueError("Checkpoint provenance mismatch")
        if (
            saved["status"] == "verified"
            and audio_path.exists()
            and digest(audio_path.read_bytes()) == saved["audio_sha256"]
        ):
            return dict(saved, reused=True)
    import mlx.core as mx

    attempts = []
    for attempt in range(3):
        mx.random.seed(
            20260905 + track["track"] * 10000 + segment["number"] + attempt * 1000000
        )
        started = time.monotonic()
        chunks = []
        for result in model.generate(
            text=segment["text"],
            split_pattern=r"\n\s*\n",
            max_tokens=1200,
            temperature=0.8,
            top_p=0.95,
            repetition_penalty=1.2,
        ):
            block = np.asarray(result.audio, dtype=np.float32)
            if block.ndim != 1 or not len(block) or not np.isfinite(block).all():
                raise ValueError("Invalid generated waveform")
            if result.sample_rate != 24000:
                raise ValueError("Unexpected sample rate")
            chunks.append(block)
        wave = np.concatenate(chunks)
        duration = len(wave) / 24000
        wav = folder / f"{stem}.attempt-{attempt}.wav"
        sf.write(wav, wave, 24000, subtype="FLOAT")
        generation_seconds = time.monotonic() - started
        text, asr_seconds = recognize(wav, ROOT / "work/whisper-tiny.en-mlx")
        tiny = compare(
            segment["text"], text, strict=review_metadata.get("strict_review", False)
        )
        final = tiny
        strong = None
        if tiny["flagged"]:
            text, elapsed = recognize(wav, ROOT / "work/whisper-small.en-mlx")
            asr_seconds += elapsed
            strong = compare(
                segment["text"],
                text,
                strict=review_metadata.get("strict_review", False),
            )
            final = strong
        invalid_duration = (
            duration < 0.3
            or duration > 46.8
            or len(segment["text"].split()) / max(duration, 0.1) * 60 > 320
        )
        accepted = not final["flagged"] and not invalid_duration
        saved = {
            "identity": identity,
            "text_sha256": segment["text_sha256"],
            "text": segment["text"],
            "file": str(wav),
            "audio_sha256": digest(wav.read_bytes()),
            "duration_seconds": duration,
            "generation_seconds": generation_seconds,
            "asr_seconds": asr_seconds,
            "status": "verified" if accepted else "needs_review",
            "attempt": attempt,
            "tiny_check": tiny,
            "strong_check": strong,
            "duration_flagged": invalid_duration,
            **review_metadata,
        }
        attempts.append(saved)
        if accepted:
            saved["attempts"] = [
                {
                    k: a[k]
                    for k in (
                        "attempt",
                        "status",
                        "file",
                        "generation_seconds",
                        "asr_seconds",
                    )
                }
                for a in attempts
            ]
            save_json(checkpoint, saved)
            return saved
    best = min(
        attempts,
        key=lambda a: (
            a["duration_flagged"],
            (a["strong_check"] or a["tiny_check"])["error_rate"],
        ),
    )
    best = dict(best)
    best["attempts"] = [
        {
            k: a[k]
            for k in ("attempt", "status", "file", "generation_seconds", "asr_seconds")
        }
        for a in attempts
    ]
    save_json(checkpoint, best)
    return best


def composition_digest(track: dict[str, Any], records: list[dict[str, Any]]) -> str:
    """Fingerprint the exact ordered passage audio and pauses used in a track."""
    pieces = [
        {
            "text_sha256": segment["text_sha256"],
            "audio_sha256": record["audio_sha256"],
            "pause_frames": round(24000 * segment["pause_seconds"]),
        }
        for segment, record in zip(track["segments"], records, strict=True)
    ]
    return digest(json.dumps(pieces, sort_keys=True))


def assemble_track(
    track: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Export a complete checked track with metadata and no pitch manipulation."""
    from mutagen.id3 import ID3, TALB, TCON, TIT2, TPE1, TPOS, TRCK, TXXX

    folder = OUTPUT / track["folder"]
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{track['track']:02d} - {track['title']}.mp3"
    joined = JOB / f"track-{track['track']:02d}.wav"
    with sf.SoundFile(
        joined, "w", samplerate=24000, channels=1, subtype="FLOAT"
    ) as target:
        for segment, record in zip(track["segments"], records, strict=True):
            wave, rate = sf.read(record["file"], dtype="float32")
            if rate != 24000:
                raise ValueError("Incorrect cached sample rate")
            if PEAK_ONLY:
                peak = float(np.max(np.abs(wave)))
                if not np.isfinite(wave).all() or peak < 0.001:
                    raise ValueError("Invalid or silent cached waveform")
                wave = wave * min(1.0, 0.95 / peak)
            target.write(wave)
            target.write(
                np.zeros(round(24000 * segment["pause_seconds"]), dtype=np.float32)
            )
    temp = JOB / f"track-{track['track']:02d}.partial.mp3"
    filters = [] if PEAK_ONLY else ["-af", "loudnorm=I=-19:TP=-2:LRA=7"]
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(joined),
            *filters,
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-y",
            str(temp),
        ],
        check=True,
    )
    tags = ID3(temp)
    for frame in [
        TIT2(encoding=3, text=track["title"]),
        TALB(encoding=3, text=ALBUM),
        TPE1(encoding=3, text="Donella H. Meadows"),
        TRCK(encoding=3, text=f"{track['track']}/30"),
        TPOS(encoding=3, text=f"{track['disc']}/9"),
        TCON(encoding=3, text="Audiobook"),
        TXXX(
            encoding=3,
            desc="Narration",
            text="AI-generated using the user-supplied male voice reference",
        ),
        TXXX(encoding=3, desc="Sections", text="; ".join(track["included_sections"])),
    ]:
        tags.add(frame)
    tags.save(temp, v2_version=3)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(temp), "-f", "null", "-"],
        check=True,
    )
    temp.replace(out)
    duration = float(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(out),
            ]
        )
    )
    return {
        "track": track["track"],
        "title": track["title"],
        "folder": track["folder"],
        "identity": records[0]["identity"],
        "composition_sha256": composition_digest(track, records),
        "file": out.relative_to(OUTPUT).as_posix(),
        "duration_seconds": duration,
        "sha256": digest(out.read_bytes()),
        "segments": len(records),
        "full_decode": "pass",
    }


def publish_navigation(completed: list[dict[str, Any]]) -> None:
    """Keep playlists synchronized with fully verified exported tracks."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    complete = sorted(completed, key=lambda row: row["track"])
    playlist = ["#EXTM3U"]
    folders: dict[str, list[str]] = {}
    for row in complete:
        info = f"#EXTINF:{row['duration_seconds']:.3f},{row['title']}"
        playlist.extend([info, row["file"]])
        folders.setdefault(row["folder"], ["#EXTM3U"]).extend(
            [info, Path(row["file"]).name]
        )
    (OUTPUT / "Play all.m3u8").write_text("\n".join(playlist) + "\n")
    for folder, entries in folders.items():
        (OUTPUT / folder / "Play in order.m3u8").write_text("\n".join(entries) + "\n")
    save_json(OUTPUT / "Track guide.json", complete)


def main() -> None:
    """Run a resumable local rendering pass and write progress after each passage."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--tracks", nargs="+", type=int)
    parser.add_argument("--max-new-segments", type=int)
    args = parser.parse_args()
    JOB.mkdir(parents=True, exist_ok=True)
    lock_handle = (JOB / "job.lock").open("a")
    fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    plan = build_plan()
    plan_path = JOB / "plan.json"
    if (
        plan_path.exists()
        and json.loads(plan_path.read_text())["identity_sha256"]
        != plan["identity_sha256"]
        and (
            any((JOB / "segments").glob("*/*.json"))
            or (JOB / "completed_tracks.json").exists()
        )
    ):
        raise ValueError(
            "Existing job uses different source, reference, or rendering settings"
        )
    save_json(plan_path, plan)
    if args.plan_only:
        print(
            json.dumps(
                {
                    "tracks": len(plan["tracks"]),
                    "segments": plan["total_segments"],
                    "conservation": "pass",
                }
            )
        )
        return
    from mlx_audio.tts.utils import load_model

    model = load_model(str(MODEL))
    model.prepare_conditionals(str(REFERENCE))
    completed_path = JOB / "completed_tracks.json"
    completed = (
        json.loads(completed_path.read_text()) if completed_path.exists() else []
    )
    completed = [
        row
        for row in completed
        if row.get("identity") == plan["identity_sha256"]
        and (OUTPUT / row["file"]).exists()
        and digest((OUTPUT / row["file"]).read_bytes()) == row["sha256"]
    ]
    if len({row["track"] for row in completed}) != len(completed):
        raise ValueError("Duplicate completed-track records")
    save_json(completed_path, completed)
    completed_ids = {t["track"] for t in completed}
    new_count = 0
    started = time.monotonic()
    current_records = []
    with (JOB / "generation.log").open("a") as quiet:
        for track in plan["tracks"]:
            if args.tracks and track["track"] not in args.tracks:
                continue
            if track["track"] in completed_ids:
                matching = next(t for t in completed if t["track"] == track["track"])
                path = OUTPUT / matching["file"]
                if path.exists() and digest(path.read_bytes()) == matching["sha256"]:
                    continue
                completed = [t for t in completed if t["track"] != track["track"]]
            current_records = []
            for segment in track["segments"]:
                with (
                    contextlib.redirect_stdout(quiet),
                    contextlib.redirect_stderr(quiet),
                ):
                    record = render_segment(
                        model, track, segment, plan["identity_sha256"]
                    )
                quiet.flush()
                current_records.append(record)
                new_count += not record.get("reused", False)
                status = {
                    "state": "rendering",
                    "track": track["track"],
                    "title": track["title"],
                    "segment": segment["number"],
                    "track_segments": len(track["segments"]),
                    "total_segments": plan["total_segments"],
                    "completed_tracks": len(completed),
                    "last_segment_status": record["status"],
                    "session_segments": new_count,
                    "session_elapsed_seconds": time.monotonic() - started,
                    "updated_at": time.time(),
                }
                save_json(JOB / "status.json", status)
                print(json.dumps(status), flush=True)
                if (
                    args.max_new_segments
                    and new_count >= args.max_new_segments
                    and segment["number"] != len(track["segments"])
                ):
                    save_json(
                        JOB / "status.json",
                        dict(status, state="paused", updated_at=time.time()),
                    )
                    return
            if all(r["status"] == "verified" for r in current_records):
                exported = assemble_track(track, current_records)
                completed.append(exported)
                save_json(completed_path, completed)
                publish_navigation(completed)
                print(json.dumps({"event": "track_exported", **exported}), flush=True)
            if args.max_new_segments and new_count >= args.max_new_segments:
                save_json(
                    JOB / "status.json",
                    {
                        "state": "complete" if len(completed) == 30 else "paused",
                        "completed_tracks": len(completed),
                        "updated_at": time.time(),
                    },
                )
                return
    pending = []
    for checkpoint in sorted((JOB / "segments").glob("*/*.json")):
        data = json.loads(checkpoint.read_text())
        if data["status"] != "verified":
            pending.append(str(checkpoint))
    status = {
        "state": "complete" if len(completed) == 30 and not pending else "needs_review",
        "completed_tracks": len(completed),
        "pending_reviews": pending,
        "updated_at": time.time(),
    }
    save_json(JOB / "status.json", status)
    print(json.dumps(status), flush=True)


if __name__ == "__main__":
    try:
        main()
    except BlockingIOError:
        # A second invocation must not overwrite the running owner's progress.
        raise
    except Exception as error:
        save_json(
            JOB / "status.json",
            {"state": "failed", "error": repr(error), "updated_at": time.time()},
        )
        raise
