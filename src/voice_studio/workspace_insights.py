"""Explain saved completion evidence and surface actionable local job problems."""

import difflib
import json
import re
import shutil


def insights(job, output, data, ready):
    alerts = []
    if data["state"] in ("failed", "stopped") and not ready:
        alerts.append(
            "Worker is stopped. Inspect the System log, then resume when the cause is resolved."
        )
    if data.get("failures", 0) >= 2:
        alerts.append(
            "Repeated worker failures. Inspect the System log before retrying; saved passages are retained."
        )
    if (
        data["state"] in ("rendering", "checking")
        and data.get("saved_age_seconds", 0) > 900
    ):
        alerts.append(
            "No passage has been saved for over 15 minutes. Inspect the worker log for a stall or a long passage."
        )
    free = shutil.disk_usage(job).free
    destination = output
    while not destination.exists() and destination != destination.parent:
        destination = destination.parent
    free = min(free, shutil.disk_usage(destination).free)
    if free < 10 * 1024**3:
        alerts.append(
            "Less than 10 GiB free on a job/output disk. Free space before continuing; no files are removed automatically."
        )
    if data["state"] == "complete" and not ready:
        alerts.append(
            "The worker finished, but this download has not passed validation. Inspect the report or rebuild the draft."
        )
    if ready:
        completion = "Ready to download · validated draft"
    elif data["generated"] < data["total"]:
        completion = "Narration incomplete · final book not ready"
    elif data["checked"] < data["total"]:
        completion = "Audio generated · quality checks remain"
    else:
        completion = "Checks finished · packaging or validation remains"
    path = job / "worker-history.jsonl"
    rows = []
    if path.exists():
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 128000))
            for line in handle.read().splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return {
        "completion": completion,
        "alerts": alerts,
        "disk_free_gib": round(free / 1024**3, 1),
        "batches": rows[-30:],
    }


def wording_diff(expected, recognized):
    """Return literal text spans; compare words/punctuation ignoring letter case."""

    def tokens(text):
        return list(re.finditer(r"\w+(?:['’]\w+)*|[^\w\s]", text))

    left, right = tokens(expected), tokens(recognized)
    matcher = difflib.SequenceMatcher(
        None,
        [m.group().casefold() for m in left],
        [m.group().casefold() for m in right],
        autojunk=False,
    )
    changed = [set(), set()]
    for tag, a, b, c, d in matcher.get_opcodes():
        if tag != "equal":
            changed[0].update(range(a, b))
            changed[1].update(range(c, d))

    def spans(text, matches, indexes):
        result, end = [], 0
        for index, match in enumerate(matches):
            if match.start() > end:
                result.append({"text": text[end : match.start()], "changed": False})
            result.append({"text": match.group(), "changed": index in indexes})
            end = match.end()
        if end < len(text):
            result.append({"text": text[end:], "changed": False})
        return result

    return {
        "expected": spans(expected, left, changed[0]),
        "recognized": spans(recognized, right, changed[1]),
        "has_changes": bool(changed[0] or changed[1]),
    }
