"""Verify paired exports and publish their local listening-page data."""

from __future__ import annotations

import argparse
import json
import subprocess

import numpy as np
import soundfile as sf

from tools.legacy.precision_comparison import APP, JOB, OUT, digest, save, verify_inputs


def publish(allow_partial: bool = False) -> None:
    """Check exact audio composition, file integrity and paired case coverage."""
    plan = json.loads((JOB / "plan.json").read_text())
    identity = digest(JOB / "plan.json")
    data = {
        "identity": identity,
        "models": {"A": "Current · 4-bit", "B": "Higher precision · unquantized"},
        "cases": [],
    }
    details = []
    for case in plan["cases"]:
        row = dict(case, results={})
        if case["id"] == "17":
            row["title"] = "Familiar garden passage"
        for label in ("A", "B"):
            folder = JOB / label / case["id"]
            path = folder / "result.json"
            if not path.exists():
                if allow_partial:
                    continue
                raise ValueError(f"Missing completed comparison: {label}/{case['id']}")
            result = json.loads(path.read_text())
            if (
                result["identity"] != identity
                or result["reference_sha256"] != plan["reference_sha256"]
            ):
                raise ValueError("Changed case provenance")
            row["results"][label] = result
            if allow_partial:
                continue
            wav = OUT / result["files"]["wav"]["file"]
            mp3 = OUT / result["files"]["mp3"]["file"]
            for item in result["files"].values():
                if digest(OUT / item["file"]) != item["sha256"]:
                    raise ValueError("Changed exported audio")
            info = sf.info(wav)
            if (
                info.samplerate != 24000
                or info.channels != 1
                or info.subtype != "PCM_16"
            ):
                raise ValueError("Unexpected WAV format")
            if info.frames != sum(
                e["frames"] + e["pause_frames"] for e in result["passages"]
            ):
                raise ValueError("Missing, repeated, or reordered audio frames")
            maximum_peak = 0.0
            with sf.SoundFile(wav) as exported:
                for index, (segment, evidence) in enumerate(
                    zip(case["segments"], result["passages"], strict=True)
                ):
                    raw = folder / f"{index:03d}.wav"
                    if (
                        digest(raw) != evidence["raw_sha256"]
                        or evidence["text_sha256"] != segment["text_sha256"]
                    ):
                        raise ValueError("Changed source passage")
                    audio, rate = sf.read(raw, dtype="float32")
                    target = exported.read(len(audio), dtype="float32")
                    expected_gain = min(1.0, 0.95 / float(np.max(np.abs(audio))))
                    if (
                        evidence["seed"] != case["seed"] + index
                        or evidence["peak_gain"] != expected_gain
                    ):
                        raise ValueError("Wrong seed or processing gain")
                    if rate != 24000 or not np.isfinite(audio).all():
                        raise ValueError("Invalid source audio")
                    if not np.allclose(
                        target, audio * expected_gain, atol=1 / 32768 + 1e-7, rtol=0
                    ):
                        raise ValueError("Export differs from paired source audio")
                    pause = exported.read(evidence["pause_frames"], dtype="float32")
                    if len(pause) != round(segment["pause_seconds"] * 24000) or np.any(
                        pause
                    ):
                        raise ValueError("Changed passage pause")
                    maximum_peak = max(maximum_peak, float(np.max(np.abs(target))))
            metadata = json.loads(
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_name,sample_rate,channels:format=duration",
                        "-of",
                        "json",
                        str(mp3),
                    ]
                )
            )
            if metadata["streams"] != [
                {"codec_name": "mp3", "sample_rate": "24000", "channels": 1}
            ]:
                raise ValueError("Unexpected MP3 format")
            if abs(float(metadata["format"]["duration"]) - info.duration) > 0.08:
                raise ValueError("MP3 duration differs")
            subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(mp3), "-f", "null", "-"], check=True
            )
            details.append(
                {
                    "model": label,
                    "case": case["id"],
                    "duration_seconds": info.duration,
                    "peak": maximum_peak,
                    "full_decode": "pass",
                    "composition": "pass",
                }
            )
        data["cases"].append(row)
    if allow_partial:
        temporary = OUT / "comparison-data.js.tmp"
        temporary.write_text(
            "window.COMPARISON_DATA=" + json.dumps(data, ensure_ascii=False) + ";\n"
        )
        temporary.replace(OUT / "comparison-data.js")
        return
    for label in ("A", "B"):
        verify_inputs(label)
    source = json.loads((JOB / "source_preservation.json").read_text())
    if digest(APP / "voice_profile.json") != source["studio_profile_sha256"]:
        raise ValueError("Studio default changed during comparison")
    repeats = []
    for row in data["cases"]:
        if row.get("repeat_of"):
            original = next(c for c in data["cases"] if c["id"] == row["repeat_of"])
            for label in ("A", "B"):
                repeats.append(
                    {
                        "model": label,
                        "case": row["id"],
                        "identical_wav": row["results"][label]["files"]["wav"]["sha256"]
                        == original["results"][label]["files"]["wav"]["sha256"],
                    }
                )
    save(
        OUT / "Verification.json",
        {
            "result": "pass",
            "cases_per_model": len(plan["cases"]),
            "paired_models": 2,
            "audio_files": len(details) * 2,
            "voice_reference_sha256": plan["reference_sha256"],
            "studio_default_preserved": True,
            "note": "File and source checks passed. First takes retained; subjective naturalness awaits listening.",
            "repeat_checks": repeats,
            "files": details,
        },
    )
    temporary = OUT / "comparison-data.js.tmp"
    temporary.write_text(
        "window.COMPARISON_DATA=" + json.dumps(data, ensure_ascii=False) + ";\n"
    )
    temporary.replace(OUT / "comparison-data.js")
    print(
        json.dumps(
            {
                "result": "pass",
                "paired_cases": len(plan["cases"]),
                "audio_files": len(details) * 2,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-partial", action="store_true")
    publish(parser.parse_args().allow_partial)
