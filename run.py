"""Stable launcher for Voice Studio, audiobooks, and local diagnostics."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from voice_studio.project_paths import python_executable  # noqa: E402

COMMANDS = {
    "doctor": "voice_studio.doctor",
    "audiobook": "voice_studio.audiobook",
    "studio": "voice_studio.studio",
    "book": "tools.legacy.higher_precision_book",
    "book-new-b": "tools.legacy.new_b_book",
    "progress": "tools.legacy.book_progress",
    "workspace": "voice_studio.live_book_progress",
    "pipeline": "voice_studio.audiobook_pipeline",
    "draft": "voice_studio.draft_audiobook",
    "workspace-action": "voice_studio.workspace_action",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=[*COMMANDS, "test"])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    executable = python_executable()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        parser.error("Python runtime missing; run uv sync or configure local.toml")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT), str(ROOT / "tests"), env.get("PYTHONPATH", "")]
    )
    if args.command == "test":
        command = [
            str(executable),
            "-m",
            "unittest",
            "discover",
            "-s",
            str(ROOT / "tests"),
            "-p",
            "test_*.py",
            *args.arguments,
        ]
    else:
        command = [str(executable), "-m", COMMANDS[args.command], *args.arguments]
    os.execve(str(executable), command, env)


if __name__ == "__main__":
    main()
