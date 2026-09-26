"""Check the selected GPU, kernel compilation, encoders, and offline voice assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

import runtime
from project_paths import studio_data_dir, workspace_root
from voice_profiles import load_profiles


def file_hash(path: Path) -> str:
    """Hash an asset without reading a model-sized allocation into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def model_hashes(path: Path) -> dict[str, str]:
    """Require and fingerprint model config, weights, and local tokenizer files."""
    if not (path / "config.json").is_file() or not (
        (path / "model.safetensors").is_file()
        or (path / "weights.safetensors").is_file()
        or (path / "weights.npz").is_file()
    ):
        raise RuntimeError(f"Model config or weights missing: {path}")
    return {
        p.name: file_hash(p)
        for p in sorted(path.iterdir())
        if p.is_file()
        and p.suffix in {".json", ".safetensors", ".txt", ".model", ".npz"}
    }


def inspect(
    root: Path, directory: Path, voice: str, engine: str, *, kernel_only: bool = False
) -> dict[str, Any]:
    """Check real GPU evaluation and local assets without producing narration."""
    report: dict[str, Any] = {"runtime": runtime.identity()}
    for name in ("ffmpeg", "ffprobe"):
        command = runtime.binary(name)
        result = subprocess.run(
            [command, "-version"], check=True, capture_output=True, text=True
        )
        report[name] = result.stdout.splitlines()[0]
    with runtime.gpu_lease():
        mx = runtime.initialize(root)
        # Elementwise compilation exercises CUDA headers/NVRTC, unlike import-only checks.
        function = mx.compile(lambda x: mx.exp(x) + x * x)
        result = function(mx.array([0.0, 1.0, 2.0]))
        mx.eval(result)
        if not np.allclose(
            np.asarray(result),
            np.exp([0.0, 1.0, 2.0]) + np.square([0.0, 1.0, 2.0]),
            rtol=1e-5,
        ):
            raise RuntimeError("GPU kernel produced an incorrect result")
        report["kernel_compilation"] = "passed"
        if kernel_only:
            return report
        report["speech_tokenizer"] = runtime.check_tokenizer(root)
        profile = load_profiles(directory)[voice]
        if engine not in profile["engines"]:
            raise RuntimeError(f"The selected voice has no {engine} engine")
        if file_hash(directory / profile["reference"]) != profile["reference_sha256"]:
            raise RuntimeError("Voice reference checksum mismatch")
        report["reference_sha256"] = profile["reference_sha256"]
        model_path = root / profile["engines"][engine]["path"]
        required = (
            ("conds.safetensors",)
            if engine == "natural"
            else ("tokenizer.json", "conds.safetensors")
        )
        for name in required:
            if not (model_path / name).is_file():
                raise RuntimeError(f"Required speech model asset is missing: {name}")
        from transformers import AutoTokenizer

        if engine == "natural":
            AutoTokenizer.from_pretrained(str(model_path), local_files_only=True)
        report["model_files"] = model_hashes(root / profile["engines"][engine]["path"])
    return report


def main() -> None:
    """Print machine-readable diagnostics and fail when a prerequisite is absent."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=workspace_root())
    parser.add_argument("--data-dir", type=Path, default=studio_data_dir())
    parser.add_argument("--voice", default="default")
    parser.add_argument(
        "--engine", choices=("natural", "expressive"), default="natural"
    )
    parser.add_argument(
        "--kernel-only",
        action="store_true",
        help="Check runtime and encoders without claiming asset readiness",
    )
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                inspect(
                    args.workspace,
                    args.data_dir,
                    args.voice,
                    args.engine,
                    kernel_only=args.kernel_only,
                ),
                indent=2,
            )
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
