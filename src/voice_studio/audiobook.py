"""Prepare, render, check, review, repair, and package reusable audiobook jobs."""

from __future__ import annotations

import argparse
import contextlib
import gc
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import soundfile as sf

from voice_studio import runtime
from voice_studio.book_pacing import RATE, dash_insertions, edge_quiet, insert_pauses
from voice_studio.book_prepare import PAUSES, pacing_text, prepare
from voice_studio.doctor import file_hash, model_hashes
from voice_studio.project_paths import studio_data_dir, workspace_root
from voice_studio.render_book import compare, save_json
from voice_studio.studio import NativeEngine
from voice_studio.voice_profiles import profile_identity


def wording_check(expected: str, actual: str) -> dict[str, Any]:
    """Require exact normalized tokens, including ambiguous word joins and splits."""
    original = actual
    digits = "zero one two three four five six seven eight nine".split()

    def preserve_zero_prefixes(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            if (
                match.start() >= 2
                and text[match.start() - 1] == "."
                and text[match.start() - 2].isdigit()
            ):
                return match[0]
            return " ".join(digits[int(char)] for char in match[0])

        return re.sub(r"(?<![\w,])0\d+(?!\w)", replace, text)

    result = compare(
        preserve_zero_prefixes(expected), preserve_zero_prefixes(actual), strict=True
    )
    result["recognized"] = original
    result["flagged"] = result["flagged"] or bool(result["changes"])
    return result


def record_path(track: dict[str, Any], segment: dict[str, Any], job: Path) -> Path:
    """Resolve a stable source-indexed checkpoint inside its job."""
    return job / "segments" / f"{track['track']:04d}" / f"{segment['number']:04d}.json"


def load_plan(job: Path, *, assets: bool = True) -> dict[str, Any]:
    """Reject edited source, plan, runtime, voice, or model inputs before reuse."""
    plan = json.loads((job / "plan.json").read_text())
    if (
        profile_identity({k: v for k, v in plan.items() if k != "plan_sha256"})
        != plan["plan_sha256"]
    ):
        raise ValueError("Job plan was modified")
    identity = plan["identity"]
    if (
        profile_identity(identity) != plan["identity_sha256"]
        or profile_identity({"tracks": plan["tracks"]}) != identity["tracks_sha256"]
    ):
        raise ValueError("Job plan identity changed; prepare a new job")
    expected = {
        "epub_sha256": file_hash(job / "source.epub"),
        "profile": json.loads((job / "voice_profile.json").read_text()),
        "narration_standard_sha256": profile_identity(plan["pause_seconds"]),
    }
    if (
        plan["profile"] != identity["profile"]
        or plan["pause_seconds"] != PAUSES
        or any(identity[k] != v for k, v in expected.items())
    ):
        raise ValueError("Frozen source, profile, or narration policy changed")
    if file_hash(job / "reference.wav") != identity["profile"]["reference_sha256"]:
        raise ValueError("Frozen voice reference changed")
    provenance = identity["profile"].get("reference_provenance")
    if (
        provenance
        and file_hash(job / provenance)
        != identity["profile"]["reference_provenance_sha256"]
    ):
        raise ValueError("Frozen voice provenance changed")
    if file_hash(Path(plan["cover"]["path"])) != plan["cover"]["sha256"]:
        raise ValueError("Frozen cover changed")
    if assets and (
        identity["runtime"] != runtime.identity()
        or identity["model_files"] != model_hashes(Path(plan["model_path"]))
        or identity["asr_files"] != model_hashes(Path(plan["asr_model"]))
    ):
        raise ValueError(
            "Runtime or model changed; prepare a new job instead of reusing its checkpoints"
        )
    return plan


def signal_metrics(wave: np.ndarray) -> dict[str, Any]:
    """Identify invalid, silent, clipped, truncated, or implausibly long passages."""
    valid = wave.ndim == 1 and len(wave) > 0 and bool(np.isfinite(wave).all())
    if not valid:
        raise ValueError("Invalid generated waveform")
    peak = float(np.max(np.abs(wave)))
    clipped = int(np.count_nonzero(np.abs(wave) >= 0.9999))
    return {
        "frames": len(wave),
        "peak": peak,
        "clipped_samples": clipped,
        "flagged": peak < 0.001
        or clipped >= 24
        or abs(float(wave[0])) > 0.02
        or abs(float(wave[-1])) > 0.02
        or not 0.3 < len(wave) / RATE < 180,
    }


def validate_record(
    record: dict[str, Any],
    track: dict[str, Any],
    segment: dict[str, Any],
    plan: dict[str, Any],
) -> None:
    """Recompute source, raw evidence, wording, and insert-only pacing decisions."""
    expected = {
        "identity": plan["identity_sha256"],
        "text_sha256": segment["text_sha256"],
        "text": segment["text"],
        "track": track["track"],
        "segment": segment["number"],
    }
    if any(record.get(k) != value for k, value in expected.items()):
        raise ValueError("Checkpoint source identity mismatch")
    if record["input_text"] != segment["speech_text"]:
        raise ValueError("Generation input changed")
    raw_path = Path(record["raw_file"])
    attempt = json.loads(raw_path.with_suffix(".json").read_text())
    if file_hash(raw_path.with_suffix(".json")) != record["attempt_sha256"]:
        raise ValueError("Immutable attempt evidence changed")
    for key in (
        "raw_sha256",
        "text_sha256",
        "identity",
        "seed",
        "attempt",
        "seed_offset",
    ):
        if record[key] != attempt[key]:
            raise ValueError("Checkpoint no longer matches its original attempt")
    if file_hash(raw_path) != record["raw_sha256"]:
        raise ValueError("Original audio checksum mismatch")
    raw, rate = sf.read(raw_path, dtype="float32")
    if rate != RATE or signal_metrics(raw) != record["signal_metrics"]:
        raise ValueError("Raw signal evidence mismatch")
    if (
        record["seed"]
        != plan["identity"]["seed_base"]
        + segment["global_number"] * 10
        + record["attempt"]
        + record["seed_offset"]
    ):
        raise ValueError("Generation seed evidence changed")
    if record["status"] != "verified":
        return
    asr_path = Path(record["asr_file"])
    if file_hash(asr_path) != record["asr_sha256"]:
        raise ValueError("Raw ASR evidence changed")
    evidence = json.loads(asr_path.read_text())
    if (
        evidence["raw_sha256"] != record["raw_sha256"]
        or evidence["identity"] != plan["identity_sha256"]
    ):
        raise ValueError("ASR belongs to another waveform or job")
    asr = evidence["result"]
    transcript = asr["text"]
    if record.get("review"):
        pointer = record["review"]
        path = Path(pointer["file"])
        if file_hash(path) != pointer["sha256"]:
            raise ValueError("Review report changed")
        review = json.loads(path.read_text())
        if (
            any(
                review[k] != record[k]
                for k in ("identity", "raw_sha256", "text_sha256", "asr_sha256")
            )
            or not review["note"].strip()
        ):
            raise ValueError(
                "Review does not match its original source, audio, and transcript"
            )
        transcript = review["heard_text"]
    if (
        wording_check(segment["speech_text"], transcript)["flagged"]
        or record["signal_metrics"]["flagged"]
    ):
        raise ValueError("Wording or signal remains unresolved")
    points = dash_insertions(pacing_text(segment), asr, raw)
    if points != record["dash_insertions"] or len(points) != len(
        segment["sentence_dashes"]
    ):
        raise ValueError("Pacing witness mismatch")
    paced_path = Path(record["paced_file"])
    if file_hash(paced_path) != record["paced_sha256"]:
        raise ValueError("Paced audio checksum mismatch")
    paced, rate = sf.read(paced_path, dtype="float32")
    if (
        rate != RATE
        or not np.array_equal(paced, insert_pauses(raw, points))
        or record["paced_frames"] != len(paced)
        or record["leading_quiet_frames"] != edge_quiet(paced)
        or record["trailing_quiet_frames"] != edge_quiet(paced, True)
        or record["peak"] != float(np.max(np.abs(paced)))
    ):
        raise ValueError("Pacing changed speech samples or edge-silence evidence")


@contextlib.contextmanager
def job_lease(job: Path) -> Any:
    """Prevent simultaneous mutations of one book while permitting pause requests."""
    import fcntl

    job.mkdir(parents=True, exist_ok=True)
    with (job / "job.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "This audiobook job already has an active worker"
            ) from exc
        yield


def status(job: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Summarize observable checkpoint state without loading a speech model."""
    counts = {"pending": 0, "rendered": 0, "needs_review": 0, "verified": 0}
    for track in plan["tracks"]:
        for segment in track["segments"]:
            path = record_path(track, segment, job)
            row = (
                json.loads(path.read_text()) if path.exists() else {"status": "pending"}
            )
            counts[row["status"]] += 1
    return {
        "state": "paused" if (job / "paused").exists() else "ready",
        "counts": counts,
        "total_segments": plan["total_segments"],
    }


def recover_checkpoints(job: Path, plan: dict[str, Any]) -> None:
    """Preserve unreadable JSON; restore valid original evidence, never bypass hashes."""
    damaged = []
    for path in (job / "segments").rglob("*.json"):
        try:
            json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            damaged.append(path)
    # If an original attempt is unreadable, preserve its derived checkpoint too.
    for track in plan["tracks"]:
        for segment in track["segments"]:
            path = record_path(track, segment, job)
            if path.exists() and path not in damaged:
                row = json.loads(path.read_text())
                raw = row.get("raw_file")
                if raw and Path(raw).with_suffix(".json") in damaged:
                    damaged.append(path)
    if damaged:
        backup = job / "recovery" / str(time.time_ns())
        for path in damaged:
            target = backup / path.relative_to(job)
            target.parent.mkdir(parents=True, exist_ok=True)
            path.replace(target)
    for track in plan["tracks"]:
        for segment in track["segments"]:
            path = record_path(track, segment, job)
            if path.exists():
                # A valid JSON record with altered evidence must stop, not regenerate.
                validate_record(json.loads(path.read_text()), track, segment, plan)
                continue
            originals = sorted(
                (path.parent / "attempts").glob(f"{segment['number']:04d}-*.json")
            )
            originals = [
                p
                for p in originals
                if p.stem.count("-") == 1 and p.stem.split("-")[-1].isdigit()
            ]
            if originals:
                row = json.loads(originals[-1].read_text())
                row["attempt_sha256"] = file_hash(originals[-1])
                validate_record(row, track, segment, plan)
                save_json(path, row)
    save_json(job / "status.json", status(job, plan))


def render(
    job: Path,
    plan: dict[str, Any],
    maximum: int | None = None,
    *,
    selected: str | None = None,
    seed_offset: int = 0,
    engine_factory: Any = NativeEngine,
) -> None:
    """Render pending passages or one explicit repair, preserving every raw attempt."""
    if (job / "paused").exists():
        return
    engine = engine_factory(plan["profile"], Path(plan["workspace"]), job)
    prepared = False
    try:
        with runtime.gpu_lease():
            count = 0
            for track in plan["tracks"]:
                for segment in track["segments"]:
                    identifier = f"{track['track']}/{segment['number']}"
                    if selected and identifier != selected:
                        continue
                    if (job / "paused").exists() or (
                        maximum is not None and count >= maximum
                    ):
                        return
                    path = record_path(track, segment, job)
                    previous = json.loads(path.read_text()) if path.exists() else None
                    folder = path.parent / "attempts"
                    originals = sorted(
                        p
                        for p in folder.glob(f"{segment['number']:04d}-*.json")
                        if p.stem.count("-") == 1 and p.stem.split("-")[-1].isdigit()
                    )
                    if previous is None and originals and not selected:
                        recovered = json.loads(originals[-1].read_text())
                        recovered["attempt_sha256"] = file_hash(originals[-1])
                        validate_record(recovered, track, segment, plan)
                        save_json(path, recovered)
                        previous = recovered
                    if previous:
                        validate_record(previous, track, segment, plan)
                        if not selected:
                            continue
                        if previous["status"] != "needs_review":
                            raise ValueError(
                                "Repair requires an explicitly held passage"
                            )
                    existing_numbers = [
                        int(p.stem.split("-")[-1])
                        for p in folder.glob(f"{segment['number']:04d}-*.wav")
                        if p.stem.split("-")[-1].isdigit()
                    ]
                    attempt = (
                        max(
                            existing_numbers
                            + ([previous["attempt"]] if previous else [])
                            + [-1]
                        )
                        + 1
                    )
                    seed = (
                        plan["identity"]["seed_base"]
                        + segment["global_number"] * 10
                        + attempt
                        + seed_offset
                    )
                    if not prepared:
                        engine.prepare(plan["engine"], 0.5)
                        prepared = True
                    if engine_factory is NativeEngine:
                        runtime.initialize(Path(plan["workspace"])).random.seed(seed)
                    started = time.monotonic()
                    blocks = []
                    for audio, rate in engine.generate(
                        segment["speech_text"], {"expression": 0.5, "max_tokens": 2400}
                    ):
                        if rate != RATE:
                            raise ValueError("Unexpected generation sample rate")
                        blocks.append(audio)
                    if not blocks:
                        raise ValueError("Speech engine returned no audio")
                    wave = np.concatenate(blocks).astype(np.float32)
                    metrics = signal_metrics(wave)
                    folder = path.parent / "attempts"
                    folder.mkdir(parents=True, exist_ok=True)
                    raw = folder / f"{segment['number']:04d}-{attempt:04d}.wav"
                    if raw.exists() or raw.with_suffix(".json").exists():
                        raise ValueError(
                            "An interrupted raw attempt exists; preserve it and prepare an explicit repair before continuing"
                        )
                    sf.write(raw, wave, RATE, subtype="FLOAT")
                    with raw.open("rb") as handle:
                        os.fsync(handle.fileno())
                    row = {
                        "identity": plan["identity_sha256"],
                        "track": track["track"],
                        "segment": segment["number"],
                        "text": segment["text"],
                        "text_sha256": segment["text_sha256"],
                        "input_text": segment["speech_text"],
                        "seed": seed,
                        "seed_offset": seed_offset,
                        "attempt": attempt,
                        "raw_file": str(raw),
                        "raw_sha256": file_hash(raw),
                        "duration_seconds": len(wave) / RATE,
                        "generation_seconds": time.monotonic() - started,
                        "signal_metrics": metrics,
                        "status": "rendered",
                        "runtime": plan["identity"]["runtime"],
                    }
                    save_json(raw.with_suffix(".json"), row)
                    row["attempt_sha256"] = file_hash(raw.with_suffix(".json"))
                    if previous:
                        save_json(
                            path.parent
                            / "history"
                            / f"{path.stem}-{previous['attempt']:04d}.json",
                            previous,
                        )
                    save_json(path, row)
                    count += 1
                    save_json(job / "status.json", status(job, plan))
            if selected and count == 0:
                raise ValueError("Selected repair passage was not found")
    finally:
        engine.release()


@contextlib.contextmanager
def recognition_session(workspace: Path) -> Iterator[None]:
    """Retain Whisper only within one GPU-owned worker; always release on exit."""
    runtime.release_models()
    runtime.initialize(workspace)
    try:
        yield
    finally:
        module = sys.modules.get("mlx_whisper.transcribe")
        holder = getattr(module, "ModelHolder", None)
        if holder is not None:
            holder.model = None
            holder.model_path = None
        gc.collect()
        runtime.clear_cache()


def transcribe_cached(path: Path, model: Path, workspace: Path) -> dict[str, Any]:
    """Recognize independently while reusing weights owned by recognition_session."""
    import mlx_whisper

    return mlx_whisper.transcribe(
        str(path),
        path_or_hf_repo=str(model),
        language="en",
        task="transcribe",
        temperature=0.0,
        condition_on_previous_text=False,
        initial_prompt=None,
        word_timestamps=True,
        fp16=True,
        verbose=None,
    )


def transcribe(path: Path, model: Path, workspace: Path) -> dict[str, Any]:
    """Standalone recognition retains the original per-call cleanup contract."""
    with recognition_session(workspace):
        return transcribe_cached(path, model, workspace)


def apply_check(
    row: dict[str, Any],
    segment: dict[str, Any],
    evidence: dict[str, Any],
    transcript: str | None = None,
) -> dict[str, Any]:
    """Hold uncertain wording or timing and otherwise construct conserved paced audio."""
    result = dict(row)
    raw, _ = sf.read(row["raw_file"], dtype="float32")
    asr = evidence["result"]
    result["wording_check"] = wording_check(
        segment["speech_text"],
        transcript if transcript is not None else asr["text"],
    )
    result["status"] = "needs_review"
    result["timing_error"] = None
    try:
        points = dash_insertions(pacing_text(segment), asr, raw)
        result["dash_insertions"] = points
    except ValueError as exc:
        result["timing_error"] = str(exc)
        return result
    if result["wording_check"]["flagged"] or row["signal_metrics"]["flagged"]:
        return result
    paced = insert_pauses(raw, points)
    target = Path(row["raw_file"]).with_suffix(".paced.wav")
    sf.write(target, paced, RATE, subtype="FLOAT")
    with target.open("rb") as handle:
        os.fsync(handle.fileno())
    result.update(
        status="verified",
        paced_file=str(target),
        paced_sha256=file_hash(target),
        paced_frames=len(paced),
        leading_quiet_frames=edge_quiet(paced),
        trailing_quiet_frames=edge_quiet(paced, True),
        peak=float(np.max(np.abs(paced))),
    )
    return result


def check(
    job: Path,
    plan: dict[str, Any],
    recognizer: Any = transcribe,
    maximum: int | None = None,
    selected: str | None = None,
) -> None:
    """Check newly generated passages once, preserving raw recognition evidence."""
    native = recognizer is transcribe
    session = (
        recognition_session(Path(plan["workspace"]))
        if native
        else contextlib.nullcontext()
    )
    with runtime.gpu_lease(), session:
        if native:
            recognizer = transcribe_cached
        count = 0
        for track in plan["tracks"]:
            for segment in track["segments"]:
                if selected and selected != f"{track['track']}/{segment['number']}":
                    continue
                if (job / "paused").exists() or (
                    maximum is not None and count >= maximum
                ):
                    return
                path = record_path(track, segment, job)
                if not path.exists():
                    continue
                row = json.loads(path.read_text())
                validate_record(row, track, segment, plan)
                if row["status"] != "rendered":
                    continue
                asr_path = Path(row["raw_file"]).with_suffix(".asr.json")
                if asr_path.exists():
                    evidence = json.loads(asr_path.read_text())
                    if (
                        evidence["raw_sha256"] != row["raw_sha256"]
                        or evidence["identity"] != plan["identity_sha256"]
                    ):
                        raise ValueError(
                            "Existing ASR evidence belongs to different inputs"
                        )
                else:
                    result = recognizer(
                        Path(row["raw_file"]),
                        Path(plan["asr_model"]),
                        Path(plan["workspace"]),
                    )
                    evidence = {
                        "identity": plan["identity_sha256"],
                        "raw_sha256": row["raw_sha256"],
                        "result": result,
                        "source_prompt": None,
                    }
                    save_json(asr_path, evidence)
                row.update(asr_file=str(asr_path), asr_sha256=file_hash(asr_path))
                row = apply_check(row, segment, evidence)
                validate_record(row, track, segment, plan)
                save_json(path, row)
                count += 1
                save_json(job / "status.json", status(job, plan))


def review(
    job: Path,
    plan: dict[str, Any],
    selected: str | None,
    transcript: Path | None,
    note: str | None,
) -> list[dict[str, Any]]:
    """List held evidence or bind a deliberate listening review to its waveform."""
    held = []
    matched = False
    for track in plan["tracks"]:
        for segment in track["segments"]:
            path = record_path(track, segment, job)
            if not path.exists():
                continue
            row = json.loads(path.read_text())
            validate_record(row, track, segment, plan)
            if row["status"] != "needs_review":
                continue
            identifier = f"{track['track']}/{segment['number']}"
            held.append(
                {
                    "id": identifier,
                    "text": segment["text"],
                    "raw_file": row["raw_file"],
                    "wording": row.get("wording_check"),
                    "timing_error": row.get("timing_error"),
                }
            )
            if selected != identifier:
                continue
            matched = True
            if transcript is None or not note or not note.strip():
                raise ValueError(
                    "Accepting review needs --transcript and a nonempty --note describing the evidence"
                )
            evidence = json.loads(Path(row["asr_file"]).read_text())
            report = {
                k: row[k]
                for k in ("identity", "raw_sha256", "text_sha256", "asr_sha256")
            }
            report.update(heard_text=transcript.read_text().strip(), note=note)
            if wording_check(segment["speech_text"], report["heard_text"])["flagged"]:
                raise ValueError(
                    "Reviewed transcript does not preserve the expected words"
                )
            review_path = (
                path.parent
                / "reviews"
                / f"{path.stem}-{row['attempt']:04d}-{time.time_ns()}.json"
            )
            save_json(review_path, report)
            row["review"] = {"file": str(review_path), "sha256": file_hash(review_path)}
            updated = apply_check(row, segment, evidence, report["heard_text"])
            validate_record(updated, track, segment, plan)
            save_json(
                path.parent / "history" / f"{path.stem}-review-{time.time_ns()}.json",
                row,
            )
            save_json(path, updated)
    if selected and not matched:
        raise ValueError("Selected passage is not held for review")
    return held


def main() -> None:
    """Dispatch explicit source preparation and resumable production stages."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser(
        "prepare", help="Create a new immutable source/voice/runtime plan"
    )
    prep.add_argument("--epub", type=Path, required=True)
    prep.add_argument("--job", type=Path, required=True)
    prep.add_argument("--workspace", type=Path, default=workspace_root())
    prep.add_argument("--data-dir", type=Path, default=studio_data_dir())
    prep.add_argument("--voice", default="default")
    prep.add_argument("--engine", choices=("natural",))
    prep.add_argument("--code", choices=("include", "omit"), default="include")
    prep.add_argument(
        "--asr-model", type=Path, default=Path("work/whisper-small.en-mlx")
    )
    prep.add_argument("--cover", type=Path)
    for name in (
        "render",
        "check",
        "review",
        "repair",
        "finish",
        "verify",
        "status",
        "pause",
        "resume",
        "recover",
    ):
        action = sub.add_parser(name)
        action.add_argument("--job", type=Path, required=True)
        if name in {"render", "resume", "check"}:
            action.add_argument("--max-segments", type=int)
        if name in {"repair", "review"}:
            action.add_argument("--segment")
        if name == "review":
            action.add_argument("--transcript", type=Path)
            action.add_argument("--note")
        if name == "repair":
            action.add_argument("--seed-offset", type=int, required=True)
        if name == "finish":
            action.add_argument("--output", type=Path)
    args = parser.parse_args()
    job = args.job.expanduser().resolve()
    if args.command == "prepare":
        plan = prepare(
            args.epub,
            job,
            args.workspace,
            args.data_dir,
            args.voice,
            args.engine,
            args.code,
            args.asr_model,
            args.cover,
        )
        print(json.dumps(status(job, plan), indent=2))
        return
    if args.command == "pause":
        if not (job / "plan.json").is_file():
            parser.error("No prepared job exists")
        (job / "paused").write_text("Paused by user\n")
        print("Pause requested; current passage completes before the worker stops.")
        return
    if args.command == "status":
        # Atomic plan/checkpoint writes let progress readers coexist with a worker.
        plan = load_plan(job, assets=False)
        print(json.dumps(status(job, plan), indent=2))
        return
    with job_lease(job):
        plan = load_plan(job)
        if args.command == "recover":
            recover_checkpoints(job, plan)
        elif args.command in {"render", "resume"}:
            if args.max_segments is not None and args.max_segments <= 0:
                parser.error("--max-segments must be positive")
            if args.command == "resume":
                (job / "paused").unlink(missing_ok=True)
            render(job, plan, args.max_segments)
        elif args.command == "check":
            if args.max_segments is not None and args.max_segments <= 0:
                parser.error("--max-segments must be positive")
            check(job, plan, maximum=args.max_segments)
        elif args.command == "repair":
            if not args.segment or args.seed_offset <= 0:
                parser.error(
                    "repair requires --segment TRACK/PASSAGE and positive --seed-offset"
                )
            render(job, plan, selected=args.segment, seed_offset=args.seed_offset)
            check(job, plan)
        elif args.command == "review":
            print(
                json.dumps(
                    review(job, plan, args.segment, args.transcript, args.note),
                    indent=2,
                )
            )
            return
        elif args.command == "finish":
            from voice_studio.book_package import finish

            output = (args.output or job / "exports").expanduser().resolve()
            print(json.dumps(finish(plan, job, output), indent=2))
            return
        elif args.command == "verify":
            report = json.loads((job / "final_verification.json").read_text())
            if (
                report["identity"] != plan["identity_sha256"]
                or file_hash(Path(report["m4b"])) != report["m4b_sha256"]
            ):
                raise ValueError("Final audiobook fingerprint changed")
            for output in report["tracks"]:
                for kind in ("wav", "mp3"):
                    if file_hash(Path(output[kind])) != output[kind + "_sha256"]:
                        raise ValueError("Chapter output fingerprint changed")
            import subprocess

            subprocess.run(
                [
                    runtime.binary("ffmpeg"),
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    report["m4b"],
                    "-f",
                    "null",
                    "-",
                ],
                check=True,
            )
            for track in plan["tracks"]:
                for segment in track["segments"]:
                    row = json.loads(record_path(track, segment, job).read_text())
                    validate_record(row, track, segment, plan)
                    if row["status"] != "verified":
                        raise ValueError("An unverified passage remains")
            print(json.dumps(report, indent=2))
            return
        print(json.dumps(status(job, plan), indent=2))


if __name__ == "__main__":
    main()
