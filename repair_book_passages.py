"""Repair held passages with cleaner punctuation and shorter continuous readings."""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import spacy

from render_book import (
    JOB,
    MODEL,
    REFERENCE,
    ROOT,
    compare,
    digest,
    recognize,
    save_checkpoint,
    save_json,
    split_long,
)


def repair_chunks(
    text: str, expand_negation: bool = False, shorter: bool = False
) -> list[str]:
    """Retain all source words while avoiding isolated attribution punctuation."""
    clean = re.sub(r"(?<!\w)[−–-](?=\d)", "negative ", text)
    clean = re.sub(r"^[—–-]\s*", "", clean)
    clean = re.sub(
        r"\bratio of (\d+)\s*:\s*(\d+)\b",
        r"ratio of \1 to \2",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"\bVol\b\.?", "Volume", clean, flags=re.IGNORECASE)
    clean = clean.replace("“", "").replace("”", "")
    clean = re.sub(r"([.!?])\.+", r"\1", clean)
    clean = re.sub(r"([:;])\1+", r"\1", clean)
    clean = re.sub(r"\b([A-Z]) shows\b", r"\1. shows", clean)
    clean = clean.replace(
        "make the rich richer and the poor poorer",
        "make the rich, richer, and the poor, poorer",
    )
    clean = clean.replace(
        "ethnic boundaries, boundaries between", "ethnic boundaries. Boundaries between"
    )
    clean = clean.replace(
        "Many relationships in systems are nonlinear.",
        "Many relationships, in systems, are nonlinear.",
    )
    clean = clean.replace(
        "as the stocks in the system shift", "as the stocks in the system, shift"
    )
    clean = re.sub(r"\bevent-event\b", "event. Event", clean, flags=re.IGNORECASE)
    clean = clean.replace("famous systems sayings", "famous systems, sayings")
    clean = clean.replace("Sylvia Nasar,", "Sylvia Nasar.")
    acronyms = {
        "OK",
        "MIT",
        "HIV",
        "AIDS",
        "GDP",
        "GNP",
        "USA",
        "US",
        "UN",
        "NASA",
        "AI",
        "DNA",
        "DDT",
        "PCB",
        "CFC",
    }
    clean = re.sub(
        r"\b[A-Z]{2,}\b",
        lambda match: (
            match.group() if match.group() in acronyms else match.group().capitalize()
        ),
        clean,
    )
    clean = re.sub(r"\s*[—–]\s*", ", ", clean)
    if expand_negation:
        contractions = {
            "can't": "cannot",
            "won't": "will not",
            "don't": "do not",
            "doesn't": "does not",
            "didn't": "did not",
            "isn't": "is not",
            "aren't": "are not",
            "wasn't": "was not",
            "weren't": "were not",
            "couldn't": "could not",
            "wouldn't": "would not",
            "shouldn't": "should not",
            "mustn't": "must not",
            "hasn't": "has not",
            "haven't": "have not",
            "hadn't": "had not",
        }
        clean = clean.replace("’", "'")
        for contraction, expansion in contractions.items():
            clean = re.sub(
                r"\b" + re.escape(contraction) + r"\b",
                expansion,
                clean,
                flags=re.IGNORECASE,
            )
    if text.startswith(("—", "–", "-")):
        prefix, separator, rest = clean.partition(",")
        if prefix.isupper():
            clean = prefix.title() + separator + rest
    first, dot, rest = clean.partition(".")
    if first.isupper():
        first = re.sub(
            r"\b[A-Z]{2,}\b",
            lambda match: (
                match.group()
                if match.group() in acronyms
                else match.group().capitalize()
            ),
            first,
        )
        clean = first + dot + rest
    if compare(text, clean)["changes"]:
        raise ValueError("Repair changed normalized source words")
    # Supply the ordinary silent-g pronunciation while retaining the source as the ASR target.
    clean = re.sub(r"\bparadigms\b", "para-dimes", clean, flags=re.IGNORECASE)

    def source_spelling(value: str) -> str:
        """Restore the declared pronunciation hint for source-word validation."""
        return value.replace("para-dimes", "paradigms")

    if shorter:
        nlp = spacy.blank("en")
        nlp.add_pipe("sentencizer")
        pieces = [
            piece
            for sentence in nlp(clean).sents
            for piece in split_long(sentence.text, 180, 26)
        ]
        chunks: list[str] = []
        for piece in pieces:
            if chunks and len((chunks[-1] + " " + piece).split()) <= 26:
                chunks[-1] += " " + piece
            else:
                chunks.append(piece)
        if compare(text, source_spelling(" ".join(chunks)))["changes"]:
            raise ValueError("Shorter repair changed source words")
        return chunks
    if len(clean.split()) <= 28:
        return [clean]
    nlp = spacy.blank("en")
    nlp.add_pipe("sentencizer")
    boundaries = [sentence.end_char for sentence in nlp(clean).sents][:-1]
    valid = [
        cut
        for cut in boundaries
        if len(clean[:cut].split()) >= 10 and len(clean[cut:].split()) >= 10
    ]
    if not valid:
        boundaries = [m.end() for m in re.finditer(r"[,;:]\s+", clean)]
        valid = [
            cut
            for cut in boundaries
            if len(clean[:cut].split()) >= 10 and len(clean[cut:].split()) >= 10
        ]
    if not valid:
        valid = [
            m.start()
            for m in re.finditer(r"\s+", clean)
            if len(clean[: m.start()].split()) >= 10
            and len(clean[m.start() :].split()) >= 10
        ]
    cut = min(valid, key=lambda x: abs(x - len(clean) / 2))
    chunks = [clean[:cut].strip(), clean[cut:].strip()]
    if compare(text, source_spelling(" ".join(chunks)))["changes"]:
        raise ValueError("Repair changed source words")
    return chunks


def main() -> None:
    """Regenerate held audio without changing accepted passages or their source text.

    Returns
    -------
    None
        Writes accepted repairs atomically; unresolved passages remain held.
    """
    import mlx.core as mx
    from mlx_audio.tts.utils import load_model

    parser = argparse.ArgumentParser()
    parser.add_argument("--files", nargs="+", type=Path)
    parser.add_argument("--shorter", action="store_true")
    parser.add_argument("--seed-offset", type=int, default=0)
    args = parser.parse_args()
    paths = [
        p.resolve() for p in (args.files or sorted((JOB / "segments").glob("*/*.json")))
    ]
    pending = [
        (p, json.loads(p.read_text()))
        for p in paths
        if json.loads(p.read_text())["status"] != "verified"
    ]
    if not pending:
        print("No held passages", flush=True)
        return
    model = load_model(str(MODEL))
    model.prepare_conditionals(str(REFERENCE))
    with (JOB / "repair_generation.log").open("a") as log:
        for path, original in pending:
            check = original["strong_check"] or original["tiny_check"]
            chunks = repair_chunks(
                original["text"],
                expand_negation=check.get("semantic_discrepancy", False),
                shorter=args.shorter,
            )
            save_json(JOB / "repair_history" / path.parent.name / path.name, original)
            for attempt in range(3):
                mx.random.seed(
                    20260905
                    + int(original["text_sha256"][:7], 16)
                    + attempt
                    + (5000000 if args.shorter else 0)
                    + args.seed_offset
                )
                started = time.monotonic()
                audio = []
                with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    for piece in chunks:
                        for result in model.generate(
                            text=piece,
                            split_pattern=r"\n\s*\n",
                            max_tokens=1200,
                            temperature=0.8,
                            top_p=0.95,
                            repetition_penalty=1.2,
                        ):
                            block = np.asarray(result.audio, dtype=np.float32)
                            if (
                                block.ndim != 1
                                or not len(block)
                                or not np.isfinite(block).all()
                            ):
                                raise ValueError("Invalid repair audio")
                            audio.extend([block, np.zeros(2880, dtype=np.float32)])
                    wave = np.concatenate(audio[:-1])
                    label = f"repair-{'short-' if args.shorter else ''}{attempt}"
                    if args.seed_offset:
                        label += f"-seed-{args.seed_offset}"
                    wav = path.with_name(path.stem + f".{label}.wav")
                    sf.write(wav, wave, 24000, subtype="FLOAT")
                    generation_seconds = time.monotonic() - started
                    recognized, asr_seconds = recognize(
                        wav, ROOT / "work/whisper-tiny.en-mlx"
                    )
                    tiny = compare(
                        original["text"],
                        recognized,
                        strict=original.get("strict_review", False),
                    )
                    strong = None
                    if tiny["flagged"] or tiny["changes"]:
                        recognized, elapsed = recognize(
                            wav, ROOT / "work/whisper-small.en-mlx"
                        )
                        asr_seconds += elapsed
                        strong = compare(
                            original["text"],
                            recognized,
                            strict=original.get("strict_review", False),
                        )
                log.flush()
                check = strong or tiny
                accepted = (
                    not check["flagged"]
                    and 0.3 < len(wave) / 24000 < 55
                    and len(original["text"].split()) / (len(wave) / 24000) * 60 <= 320
                )
                print(
                    json.dumps(
                        {
                            "passage": str(path),
                            "attempt": attempt,
                            "accepted": accepted,
                            "check": check,
                        }
                    ),
                    flush=True,
                )
                candidate = dict(
                    original,
                    file=str(wav),
                    audio_sha256=digest(wav.read_bytes()),
                    duration_seconds=len(wave) / 24000,
                    generation_seconds=generation_seconds,
                    asr_seconds=asr_seconds,
                    status="verified" if accepted else "needs_review",
                    tiny_check=tiny,
                    strong_check=strong,
                    duration_flagged=not (
                        0.3 < len(wave) / 24000 < 55
                        and len(original["text"].split()) / (len(wave) / 24000) * 60
                        <= 320
                    ),
                    repair_chunks=chunks,
                    repair_attempt=label,
                )
                if any("para-dimes" in piece for piece in chunks):
                    candidate["pronunciation_hints"] = {"para-dimes": "paradigms"}
                save_json(
                    JOB
                    / "repair_candidates"
                    / path.parent.name
                    / f"{path.stem}.{label}.json",
                    candidate,
                )
                if accepted:
                    repaired = dict(
                        original,
                        file=str(wav),
                        audio_sha256=digest(wav.read_bytes()),
                        duration_seconds=len(wave) / 24000,
                        generation_seconds=generation_seconds,
                        asr_seconds=asr_seconds,
                        status="verified",
                        tiny_check=tiny,
                        strong_check=strong,
                        duration_flagged=False,
                        repair_chunks=chunks,
                        repair_note="Normalized source wording preserved; attribution punctuation and heading capitalization cleaned, flagged negative contractions expanded, and longer passages split into shorter readings.",
                    )
                    repaired["attempts"] = original.get("attempts", []) + [
                        {
                            "attempt": label,
                            "status": "verified",
                            "file": str(wav),
                            "generation_seconds": generation_seconds,
                            "asr_seconds": asr_seconds,
                        }
                    ]
                    if candidate.get("pronunciation_hints"):
                        repaired["pronunciation_hints"] = candidate[
                            "pronunciation_hints"
                        ]
                    save_checkpoint(path, original, repaired)
                    break


if __name__ == "__main__":
    main()
