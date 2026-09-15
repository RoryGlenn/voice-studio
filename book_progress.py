"""Summarize persisted progress without touching the running renderer."""

import argparse
import json
import time
from pathlib import Path

from project_paths import workspace_root


def main() -> None:
    """Read an explicit job, defaulting to the approved higher-precision edition."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--job",
        type=Path,
        default=workspace_root()
        / "work/own_voice/book-natural-high-precision-20260913",
    )
    args = parser.parse_args()
    root = args.job.expanduser().resolve()
    if not (root / "plan.json").is_file() or not (root / "status.json").is_file():
        parser.error(f"Job plan/status not found: {root}")
    plan = json.loads((root / "plan.json").read_text())
    status = json.loads((root / "status.json").read_text())
    records = [
        (path, json.loads(path.read_text()))
        for path in sorted((root / "segments").glob("*/*.json"))
    ]
    verified = [row for _, row in records if row["status"] == "verified"]
    flags = [str(path) for path, row in records if row["status"] != "verified"]
    done_words = sum(len(row["text"].split()) for _, row in records)
    total_words = sum(s["word_count"] for t in plan["tracks"] for s in t["segments"])
    effort = sum(
        sum(
            a["generation_seconds"] + a["asr_seconds"]
            for a in row.get("attempts", [row])
        )
        for _, row in records
    )
    print(
        json.dumps(
            {
                "state": status["state"],
                "track": status.get("track"),
                "title": status.get("title"),
                "track_progress": f"{status.get('segment', 0)}/{status.get('track_segments', 0)}",
                "completed_tracks": status.get("completed_tracks", 0),
                "verified_passages": len(verified),
                "total_passages": plan["total_segments"],
                "percent_words_rendered": round(100 * done_words / total_words, 1),
                "audio_minutes": round(
                    sum(row["duration_seconds"] for row in verified) / 60, 1
                ),
                "estimated_remaining_minutes": round(
                    (total_words - done_words) * effort / max(done_words, 1) / 60
                ),
                "status_age_seconds": round(time.time() - status["updated_at"]),
                "pending_reviews": flags,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
