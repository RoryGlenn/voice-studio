"""Insert-only audiobook pacing with exact waveform conservation checks."""

from __future__ import annotations

import difflib
import re
from typing import Any

import numpy as np

RATE = 24000


def words(text: str) -> list[str]:
    """Tokenize ordinary speech while allowing the British spelling in the page."""
    return re.findall(
        "[^\\W_]+(?:'[^\\W_]+)?",
        text.casefold().replace("’", "'").replace("recognise", "recognize"),
    )


def quiet_runs(
    wave: np.ndarray, lo: int, hi: int, threshold: float
) -> list[tuple[int, int]]:
    """Find low-energy runs in a bounded region using five-millisecond RMS frames."""
    size = 120
    a = max(0, lo // size)
    b = min(len(wave) // size, (hi + size - 1) // size)
    runs = []
    start = None
    for frame in range(a, b):
        block = wave[frame * size : (frame + 1) * size]
        quiet = float(np.sqrt(np.mean(block * block))) <= threshold
        if quiet and start is None:
            start = frame * size
        if start is not None and (not quiet or frame == b - 1):
            end = (frame + 1) * size if quiet else frame * size
            left, right = (max(start, lo), min(end, hi))
            if right - left >= round(0.03 * RATE):
                runs.append((left, right))
            start = None
    return runs


def dash_insertions(
    text: str, asr: dict[str, Any], wave: np.ndarray
) -> list[dict[str, Any]]:
    """Locate dash boundaries only inside existing quiet gaps between matched words."""
    expected = words(text)
    heard = []
    timings = []
    for segment in asr["segments"]:
        for word in segment.get("words", []):
            for token in words(word["word"]):
                heard.append(token)
                timings.append(word)
    mapping = {}
    for block in difflib.SequenceMatcher(
        None, expected, heard, autojunk=False
    ).get_matching_blocks():
        for k in range(block.size):
            mapping[block.a + k] = block.b + k
    level = float(np.sqrt(np.mean(wave * wave)))
    threshold = max(0.0002, min(0.001, level * 0.012))
    results: list[dict[str, Any]] = []
    for mark in re.finditer("—", text):
        left_index = len(words(text[: mark.start()])) - 1
        right_index = left_index + 1
        if left_index not in mapping or right_index not in mapping:
            raise ValueError("Dash anchors did not align exactly")
        if mapping[right_index] != mapping[left_index] + 1:
            raise ValueError("Dash anchors are not adjacent in recognition")
        a, b = (timings[mapping[left_index]], timings[mapping[right_index]])
        values = [
            a.get("end"),
            b.get("start"),
            a.get("probability", 0),
            b.get("probability", 0),
        ]
        if any((v is None or not np.isfinite(v) for v in values)):
            raise ValueError("Nonfinite dash timing or confidence")
        if min(a.get("probability", 0), b.get("probability", 0)) < 0.35:
            raise ValueError("Low-confidence dash boundary")
        lo = round(float(a["end"]) * RATE)
        hi = round(float(b["start"]) * RATE)
        if not 0 <= lo < hi <= len(wave):
            raise ValueError("Dash timing is outside the recording")
        if hi - lo < round(0.03 * RATE):
            raise ValueError(
                f"No credible timed gap at dash {expected[left_index]} / {expected[right_index]}"
            )
        gaps = quiet_runs(wave, lo, hi, threshold)
        if not gaps:
            raise ValueError(
                f"No quiet inter-word gap at dash {expected[left_index]} / {expected[right_index]}"
            )
        gap = max(gaps, key=lambda p: p[1] - p[0])
        cut = (gap[0] + gap[1]) // 2
        native_start = gap[0] // 120 * 120
        native_end = (gap[1] + 119) // 120 * 120
        while native_start >= 120:
            quiet_block = wave[native_start - 120 : native_start]
            if float(np.sqrt(np.mean(quiet_block * quiet_block))) > threshold:
                break
            native_start -= 120
        while native_end + 120 <= len(wave):
            quiet_block = wave[native_end : native_end + 120]
            if float(np.sqrt(np.mean(quiet_block * quiet_block))) > threshold:
                break
            native_end += 120
        if results and native_start < results[-1]["quiet_end_frame"]:
            raise ValueError("Dash boundaries share a native quiet run")
        additional = max(0, round(0.35 * RATE) - (native_end - native_start))
        results.append(
            {
                "before": expected[left_index],
                "after": expected[right_index],
                "source_offset": mark.start(),
                "left_word": a,
                "right_word": b,
                "safety_quiet_start_frame": gap[0],
                "safety_quiet_end_frame": gap[1],
                "quiet_start_frame": native_start,
                "quiet_end_frame": native_end,
                "cut_frame": cut,
                "insert_frames": additional,
                "existing_quiet_seconds": (native_end - native_start) / RATE,
                "result_quiet_seconds": (native_end - native_start + additional) / RATE,
                "rms_threshold": threshold,
            }
        )
    return results


def insert_pauses(wave: np.ndarray, points: list[dict[str, Any]]) -> np.ndarray:
    """Insert silence without deleting, fading or changing any generated speech sample."""
    blocks = []
    cursor = 0
    for item in sorted(points, key=lambda p: p["cut_frame"]):
        cut = item["cut_frame"]
        if not cursor <= cut <= len(wave):
            raise ValueError("Waveform conservation check failed")
        blocks.extend(
            [wave[cursor:cut], np.zeros(item["insert_frames"], dtype=np.float32)]
        )
        cursor = cut
    blocks.append(wave[cursor:])
    out = np.concatenate(blocks)
    cursor_in = cursor_out = 0
    for item in sorted(points, key=lambda p: p["cut_frame"]):
        count = item["cut_frame"] - cursor_in
        if not np.array_equal(
            out[cursor_out : cursor_out + count], wave[cursor_in : cursor_in + count]
        ):
            raise ValueError("Waveform conservation check failed")
        cursor_in += count
        cursor_out += count
        if not not np.count_nonzero(
            out[cursor_out : cursor_out + item["insert_frames"]]
        ):
            raise ValueError("Waveform conservation check failed")
        cursor_out += item["insert_frames"]
    if not np.array_equal(out[cursor_out:], wave[cursor_in:]):
        raise ValueError("Waveform conservation check failed")
    return out


def edge_quiet(wave: np.ndarray, reverse: bool = False) -> int:
    """Estimate native edge silence conservatively, retaining the full waveform."""
    values = wave[::-1] if reverse else wave
    size = 120
    frames = 0
    for start in range(0, len(values) - size + 1, size):
        if np.sqrt(np.mean(values[start : start + size] ** 2)) > 0.0005:
            break
        frames += size
    return frames
