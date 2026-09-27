"""Run one paused-job review action under the pipeline and checkpoint locks."""

import argparse
import fcntl
import json
from pathlib import Path

from voice_studio.audiobook import (
    check,
    job_lease,
    load_plan,
    record_path,
    render,
    review,
    status,
)
from voice_studio.render_book import save_json


def perform(job, request_path):
    payload = json.loads(request_path.read_text())
    with (job / "scheduled.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with job_lease(job):
            if not (job / "paused").exists():
                raise ValueError("Job must be paused")
            plan = load_plan(job)
            selected = payload["id"]
            track, segment = next(
                (t, s)
                for t in plan["tracks"]
                for s in t["segments"]
                if f"{t['track']}/{s['number']}" == selected
            )
            row = json.loads(record_path(track, segment, job).read_text())
            if (
                row["status"] != "needs_review"
                or row["raw_sha256"] != payload["fingerprint"]
            ):
                raise ValueError("Review evidence changed")
            (job / "dashboard-package-stale").touch()
            if payload["action"] == "approve":
                transcript = request_path.with_suffix(".txt")
                transcript.write_text(payload["transcript"])
                review(job, plan, selected, transcript, payload["note"])
            elif payload["action"] == "regenerate":
                (job / "paused").unlink()
                try:
                    render(job, plan, selected=selected, seed_offset=1)
                    check(job, plan, selected=selected)
                finally:
                    (job / "paused").write_text("Paused after workspace regeneration\n")
            else:
                raise ValueError("Unknown review action")
            save_json(job / "status.json", status(job, plan))
            current = json.loads(record_path(track, segment, job).read_text())
            print(
                "Passage verified. Job remains paused; resume when ready."
                if current["status"] == "verified"
                else "Audio and review evidence saved. Passage still needs review; job remains paused."
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    perform(args.job, args.request)
