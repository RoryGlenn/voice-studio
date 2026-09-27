"""Compatibility entrypoint for existing audiobook supervisors."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
if __name__ == "__main__":
    runpy.run_module("voice_studio.draft_audiobook", run_name="__main__")
