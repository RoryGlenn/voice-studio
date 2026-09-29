"""Forecast active-stage work from completed batches and remaining text."""

import json
import math
from statistics import median


def fit_cost(samples):
    """Nonnegative least squares: seconds = passage overhead + word cost."""
    nn = sum(n * n for n, w, s in samples)
    ww = sum(w * w for n, w, s in samples)
    nw = sum(n * w for n, w, s in samples)
    ns = sum(n * s for n, w, s in samples)
    ws = sum(w * s for n, w, s in samples)
    candidates = [(ns / nn, 0)] if nn else []
    if ww:
        candidates.append((0, ws / ww))
    determinant = nn * ww - nw * nw
    if determinant > nn * ww * 1e-10:
        a, b = (ns * ww - ws * nw) / determinant, (ws * nn - ns * nw) / determinant
        if a >= 0 and b >= 0:
            candidates.append((a, b))
    return min(
        candidates,
        key=lambda ab: sum((s - ab[0] * n - ab[1] * w) ** 2 for n, w, s in samples),
    )


class Estimator:
    def __init__(self, job, plan):
        self.path = job / "worker-history.jsonl"
        self.prefix = [0]
        for track in plan["tracks"]:
            for segment in track["segments"]:
                self.prefix.append(
                    self.prefix[-1] + len(segment.get("text", "").split())
                )
        self.stamp, self.rows = None, []

    def forecast(self, data, pending_words):
        result = dict(
            estimate_seconds=None,
            estimate_low_seconds=None,
            estimate_high_seconds=None,
            estimate_batches=0,
        )
        stage = data["state"]
        if stage not in ("rendering", "checking"):
            return result
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return result
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp != self.stamp:
            rows = []
            for line in self.path.read_text().splitlines():
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except ValueError:
                    continue
            self.rows, self.stamp = rows, stamp
        total = len(self.prefix) - 1
        eligible = [r for r in self.rows if r.get("stage") == stage]
        # Reproducible across refreshes/restarts: anchor to last finished batch,
        # not the clock. Retain at most six hours and 120 completed batches.
        latest = max((r.get("finished_at", 0) for r in eligible), default=0)
        samples = []
        for row in eligible:
            if (
                row.get("exit_code") != 0
                or row.get("timed_out")
                or row.get("finished_at", 0) < latest - 21600
            ):
                continue
            try:
                seconds = float(row["seconds"])
                before, after = row["before"], row["after"]
                if stage == "rendering":
                    lo, hi = total - before["pending"], total - after["pending"]
                    if not 0 <= lo < hi <= total:
                        continue
                    count, words = hi - lo, self.prefix[hi] - self.prefix[lo]
                else:
                    count = (
                        after["verified"]
                        + after["needs_review"]
                        - before["verified"]
                        - before["needs_review"]
                    )
                    words = 0
                if count > 0 and math.isfinite(seconds) and seconds > 0:
                    samples.append((count, words, seconds))
            except (KeyError, TypeError, ValueError):
                continue
        samples = samples[-120:]
        result["estimate_batches"] = len(samples)
        if len(samples) < 8 or sum(s for n, w, s in samples) < 600:
            return result
        a, b = fit_cost(samples)
        ratios = sorted(s / (a * n + b * w) for n, w, s in samples)
        # One unusually slow batch cannot set the central forecast.
        scale = median(ratios)
        remaining = (
            data["remaining"]
            if stage == "rendering"
            else data["total"] - data["checked"]
        )
        cost = a * remaining + b * pending_words
        estimate = cost * scale
        low = min(ratios[int((len(ratios) - 1) * 0.1)], scale * 0.85)
        high = max(ratios[int((len(ratios) - 1) * 0.9)], scale * 1.15)
        result.update(
            estimate_seconds=estimate,
            estimate_low_seconds=cost * low,
            estimate_high_seconds=cost * high,
        )
        return result
