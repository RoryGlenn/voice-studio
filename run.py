"""Launch Voice Studio or a saved audiobook workflow with the selected runtime."""

from __future__ import annotations

import argparse
import os

from project_paths import REPOSITORY, python_executable

COMMANDS = {
    "doctor": "doctor.py",
    "audiobook": "audiobook.py",
    "studio": "studio.py",
    "book": "higher_precision_book.py",
    "book-new-b": "new_b_book.py",
    "progress": "book_progress.py",
}


def main() -> None:
    """Forward arguments without a shell or installing dependencies implicitly."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    executable = python_executable()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        parser.error(
            "Python runtime is missing. Run uv sync or set python in local.toml."
        )
    command = [
        str(executable),
        str(REPOSITORY / COMMANDS[args.command]),
        *args.arguments,
    ]
    os.execv(str(executable), command)


if __name__ == "__main__":
    main()
