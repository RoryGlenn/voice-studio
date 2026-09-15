"""Render an auditable paired precision comparison through the Studio engine."""

from __future__ import annotations

import argparse
import copy
import gc
import hashlib
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from project_paths import workspace_root

ROOT = workspace_root()
JOB = ROOT / "work/own_voice/precision-comparison-20260913"
OUT = ROOT / "outputs/Rory Voice - Precision Comparison"
APP = ROOT / "outputs/Rory Voice Studio"


def digest(path: Path) -> str:
    """Return a file fingerprint without loading a model-sized file into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def save(path: Path, data: Any) -> None:
    """Atomically publish checkpoint data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    temporary.replace(path)


def load_studio() -> Any:
    """Use the existing speech engine without modifying the running application."""
    spec = importlib.util.spec_from_file_location(
        "comparison_studio", APP / "studio.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot import Voice Studio")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_inputs(label: str) -> dict[str, str]:
    """Reject changed settings or model files before any cached audio is reused."""
    frozen = json.loads((JOB / "frozen_inputs.json").read_text())
    for path, key in (
        (JOB / "plan.json", "plan_sha256"),
        (JOB / "voice_profile.json", "profile_sha256"),
        (APP / "studio.py", "studio_sha256"),
    ):
        if digest(path) != frozen[key]:
            raise ValueError(f"Frozen comparison input changed: {path}")
    plan = json.loads((JOB / "plan.json").read_text())
    model_path = ROOT / plan["models"][label]["path"]
    for name, expected in frozen["models"][label].items():
        if digest(model_path / name) != expected:
            raise ValueError(f"Frozen model file changed: {model_path / name}")
    profile = json.loads((JOB / "voice_profile.json").read_text())
    for key in ("temperature", "repetition_penalty"):
        if profile["synthesis"][key] != plan["settings"][key]:
            raise ValueError(f"Profile differs from planned setting: {key}")
    return frozen["models"][label]


def render(label: str, selected: list[str] | None) -> None:
    """Render each passage once, retaining failures and repeat samples for review."""
    import fcntl

    with (JOB / "render.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _render(label, selected)


def _render(label: str, selected: list[str] | None) -> None:
    """Synthesize sequentially with identical settings and paired passage seeds."""
    model_hashes = verify_inputs(label)
    plan = json.loads((JOB / "plan.json").read_text())
    identity = digest(JOB / "plan.json")
    profile = json.loads((JOB / "voice_profile.json").read_text())
    if digest(Path(profile["reference"])) != plan["reference_sha256"]:
        raise ValueError("Frozen reference changed")
    profile = copy.deepcopy(profile)
    profile["engines"]["natural"] = plan["models"][label]
    studio = load_studio()
    engine = studio.NativeEngine(profile)
    loaded = engine.prepare("natural", 0.5)
    import mlx.core as mx

    save(
        JOB / f"model-{label}.json",
        {
            **plan["models"][label],
            "load_seconds": loaded,
            "config_sha256": model_hashes["config.json"],
            "weights_sha256": model_hashes["model.safetensors"],
        },
    )
    for case in plan["cases"]:
        if selected and case["id"] not in selected:
            continue
        folder = JOB / label / case["id"]
        folder.mkdir(parents=True, exist_ok=True)
        destination = OUT / label
        destination.mkdir(parents=True, exist_ok=True)
        wav = destination / f"{case['id']}.wav"
        mp3 = destination / f"{case['id']}.mp3"
        checkpoint = folder / "result.json"
        if checkpoint.exists():
            prior = json.loads(checkpoint.read_text())
            if prior["identity"] != identity:
                raise ValueError("Comparison plan changed after rendering")
            if all(
                digest(OUT / item["file"]) == item["sha256"]
                for item in prior["files"].values()
            ):
                print(
                    json.dumps({"event": "reused", "model": label, "case": case["id"]}),
                    flush=True,
                )
                continue
        started = time.monotonic()
        evidence = []
        partial = wav.with_suffix(".partial.wav")
        with sf.SoundFile(
            partial, "w", samplerate=24000, channels=1, subtype="PCM_16"
        ) as output:
            for index, segment in enumerate(case["segments"]):
                save(
                    JOB / "status.json",
                    {
                        "state": "rendering",
                        "model": label,
                        "case": case["id"],
                        "passage": index + 1,
                        "passages": len(case["segments"]),
                        "updated_at": time.time(),
                    },
                )
                seed = case["seed"] + index
                raw = folder / f"{index:03d}.wav"
                entry = folder / f"{index:03d}.json"
                record = json.loads(entry.read_text()) if entry.exists() else None
                if (
                    record
                    and record["identity"] == identity
                    and digest(raw) == record["raw_sha256"]
                ):
                    audio, rate = sf.read(raw, dtype="float32")
                else:
                    mx.random.seed(seed)
                    begin = time.monotonic()
                    blocks = []
                    for block, rate in engine.generate(
                        segment["text"], {"expression": 0.5}
                    ):
                        if (
                            rate != 24000
                            or block.ndim != 1
                            or not len(block)
                            or not np.isfinite(block).all()
                        ):
                            raise ValueError("Invalid model output")
                        blocks.append(block)
                    if not blocks:
                        raise ValueError("Empty model output")
                    audio = np.concatenate(blocks)
                    if len(audio) > 24000 * 180 or float(np.max(np.abs(audio))) < 0.001:
                        raise ValueError("Silent or excessively long model output")
                    sf.write(raw, audio, 24000, subtype="FLOAT")
                    record = {
                        "identity": identity,
                        "text_sha256": segment["text_sha256"],
                        "raw_sha256": digest(raw),
                        "seed": seed,
                        "frames": len(audio),
                        "generation_seconds": time.monotonic() - begin,
                    }
                    save(entry, record)
                peak = float(np.max(np.abs(audio)))
                gain = min(1.0, 0.95 / peak)
                output.write(audio * gain)
                pause = round(segment["pause_seconds"] * 24000)
                output.write(np.zeros(pause, dtype=np.float32))
                evidence.append(dict(record, peak_gain=gain, pause_frames=pause))
                status = {
                    "state": "rendering",
                    "model": label,
                    "case": case["id"],
                    "passage": index + 1,
                    "passages": len(case["segments"]),
                    "updated_at": time.time(),
                }
                save(JOB / "status.json", status)
                print(json.dumps(status), flush=True)
        temporary_mp3 = mp3.with_suffix(".partial.mp3")
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(partial),
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                "-metadata",
                f"title={case['title']} — {label}",
                "-metadata",
                "comment=AI-generated voice comparison",
                "-y",
                str(temporary_mp3),
            ],
            check=True,
        )
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(temporary_mp3), "-f", "null", "-"],
            check=True,
        )
        partial.replace(wav)
        temporary_mp3.replace(mp3)
        report = {
            "identity": identity,
            "model": label,
            "case": case["id"],
            "title": case["title"],
            "reference_sha256": plan["reference_sha256"],
            "passages": evidence,
            "words": case["words"],
            "duration_seconds": sf.info(wav).duration,
            "elapsed_seconds": time.monotonic() - started,
            "files": {
                ext: {"file": path.relative_to(OUT).as_posix(), "sha256": digest(path)}
                for ext, path in (("wav", wav), ("mp3", mp3))
            },
        }
        save(checkpoint, report)
        print(
            json.dumps(
                {
                    "event": "completed",
                    "model": label,
                    "case": case["id"],
                    "seconds": report["duration_seconds"],
                }
            ),
            flush=True,
        )
    engine.model = None
    engine.conds = None
    del engine
    gc.collect()
    mx.clear_cache()


def recognize() -> None:
    """Check wording after both models finish, without altering audition audio."""
    from render_book import compare
    from render_book import recognize as transcribe

    plan = json.loads((JOB / "plan.json").read_text())
    findings = []
    for label in ("A", "B"):
        for case in plan["cases"]:
            folder = JOB / label / case["id"]
            for index, segment in enumerate(case["segments"]):
                raw = folder / f"{index:03d}.wav"
                check_path = folder / f"{index:03d}.asr.json"
                check = (
                    json.loads(check_path.read_text()) if check_path.exists() else None
                )
                if not check or check["audio_sha256"] != digest(raw):
                    recognized, elapsed = transcribe(
                        raw, ROOT / "work/whisper-small.en-mlx"
                    )
                    check = {
                        "audio_sha256": digest(raw),
                        "asr_seconds": elapsed,
                        "check": compare(segment["text"], recognized),
                    }
                    save(check_path, check)
                if check["check"]["changes"]:
                    findings.append(
                        {
                            "model": label,
                            "case": case["id"],
                            "passage": index + 1,
                            **check["check"],
                        }
                    )
            print(
                json.dumps({"event": "asr", "model": label, "case": case["id"]}),
                flush=True,
            )
    save(
        OUT / "Speech checks.json",
        {
            "note": "Recognition differences require review; this does not measure metallic timbre.",
            "differences": findings,
        },
    )


def main() -> None:
    """Run one model at a time or perform the subsequent wording check."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("render", "asr"))
    parser.add_argument("--model", choices=("A", "B"))
    parser.add_argument("--cases", nargs="+")
    args = parser.parse_args()
    os.environ["HF_HOME"] = str(ROOT / "work/hf-narration-cache")
    os.environ["HF_HUB_OFFLINE"] = "1"
    if args.action == "render":
        if args.model is None:
            parser.error("render requires --model")
        try:
            render(args.model, args.cases)
        except BlockingIOError:
            # Another process owns the render and its progress file.
            raise
        except Exception as error:
            path = JOB / "status.json"
            status = json.loads(path.read_text()) if path.exists() else {}
            save(
                path,
                dict(
                    status,
                    state="failed",
                    model=args.model,
                    error=repr(error),
                    updated_at=time.time(),
                ),
            )
            raise
    else:
        recognize()


if __name__ == "__main__":
    main()
