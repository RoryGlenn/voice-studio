"""Reapply current word-comparison rules to cached independent transcripts."""

import json

from voice_studio.render_book import JOB, compare, save_checkpoint

cleared, newly_flagged = [], []
for path in sorted((JOB / "segments").glob("*/*.json")):
    record = json.loads(path.read_text())
    original = dict(record)
    key = "strong_check" if record.get("strong_check") else "tiny_check"
    current = compare(
        record["text"],
        record[key]["recognized"],
        strict=record.get("strict_review", False),
    )
    status = (
        "needs_review"
        if current["flagged"] or record["duration_flagged"]
        else "verified"
    )
    if status != record["status"]:
        (cleared if status == "verified" else newly_flagged).append(str(path))
    if current != record[key] or status != record["status"]:
        record[key] = current
        record["status"] = status
        save_checkpoint(path, original, record)
print(json.dumps({"cleared": cleared, "newly_flagged": newly_flagged}, indent=2))
