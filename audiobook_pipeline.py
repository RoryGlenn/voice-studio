"""Compatibility entrypoint; implementation lives in src/voice_studio."""

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
if __name__ == "__main__":
    runpy.run_module("voice_studio.audiobook_pipeline", run_name="__main__")
