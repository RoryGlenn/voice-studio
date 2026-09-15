"""Use the stronger recognizer for minor differences from the initial speech check."""

from __future__ import annotations

import json
from pathlib import Path

from render_book import JOB, ROOT, compare, recognize, save_checkpoint


def main() -> None:
    """Adjudicate existing minor differences without resynthesizing or prompting speech."""
    for path in sorted((JOB / "segments").glob("*/*.json")):
        record = json.loads(path.read_text())
        original = dict(record)
        if record["strong_check"] or not record["tiny_check"]["changes"]:
            continue
        recognized, elapsed = recognize(
            Path(record["file"]), ROOT / "work/whisper-small.en-mlx"
        )
        check = compare(
            record["text"], recognized, strict=record.get("strict_review", False)
        )
        record["strong_check"] = check
        record["asr_seconds"] += elapsed
        record["status"] = (
            "needs_review"
            if check["flagged"] or record["duration_flagged"]
            else "verified"
        )
        save_checkpoint(path, original, record)
        print(json.dumps({"path": str(path), "check": check}), flush=True)


if __name__ == "__main__":
    main()
