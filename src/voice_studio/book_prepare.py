"""Prepare source-mapped EPUB jobs without publisher-specific text replacements."""

from __future__ import annotations

import io
import posixpath
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from voice_studio import runtime
from voice_studio.doctor import file_hash, model_hashes
from voice_studio.render_book import digest, save_json
from voice_studio.voice_profiles import (
    load_profiles,
    profile_defaults,
    profile_identity,
)

PAUSES = {
    "heading_to_paragraph": 0.85,
    "paragraph_to_paragraph": 0.70,
    "paragraph_to_heading": 1.00,
    "sentence_dash": 0.35,
}


def clean(text: str) -> str:
    """Normalize whitespace without removing or substituting source wording."""
    return " ".join(text.split())


def local_tag(element: ET.Element) -> str:
    """Return the namespace-free HTML or OPF element name."""
    return element.tag.rsplit("}", 1)[-1].lower()


def archive_path(base: str, href: str) -> str:
    """Resolve a relative EPUB resource while rejecting paths outside the archive."""
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        raise ValueError(f"External EPUB resource is unsupported: {href}")
    path = posixpath.normpath(
        posixpath.join(posixpath.dirname(base), unquote(parsed.path))
    )
    if path.startswith("../") or path.startswith("/") or path == "..":
        raise ValueError("EPUB resource escapes its archive")
    return path


def visible(element: ET.Element) -> str:
    """Preserve inline text, tails, line breaks, and image alternative text."""
    result = element.text or ""
    for child in element:
        kind = local_tag(child)
        result += (
            " "
            if kind == "br"
            else child.get("alt", "")
            if kind == "img"
            else visible(child)
        )
        result += child.tail or ""
    return result


def units(
    document: bytes, href: str, code: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract ordered narration units and explicitly account for omissions."""
    root = ET.fromstring(document)
    body = next((e for e in root.iter() if local_tag(e) == "body"), None)
    if body is None:
        raise ValueError(f"EPUB content has no body: {href}")
    rows: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    blocks = {
        "body",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "pre",
        "figcaption",
        "figure",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "ul",
        "ol",
        "li",
        "blockquote",
        "hr",
        "nav",
        "script",
        "style",
    }

    def add(text: str, kind: str, location: str) -> None:
        value = clean(text)
        if value:
            rows.append(
                {
                    "text": value,
                    "kind": kind,
                    "source": {"href": href, "element": location},
                }
            )

    def walk(element: ET.Element, location: str) -> None:
        kind = local_tag(element)
        text = clean(visible(element))
        if kind in {"script", "style", "nav"} or (kind == "pre" and code == "omit"):
            if text:
                omitted.append(
                    {
                        "href": href,
                        "element": location,
                        "reason": "standalone code policy"
                        if kind == "pre"
                        else f"non-narrative {kind}",
                        "text": text,
                        "sha256": digest(text),
                    }
                )
            return
        role = "heading" if re.fullmatch(r"h[1-6]", kind) else "paragraph"
        inline = element.text or ""
        run = 0
        for index, child in enumerate(element):
            child_has_block = any(
                local_tag(descendant) in blocks for descendant in child.iter()
            )
            if child_has_block:
                add(inline, role, location + f"/inline[{run}]")
                inline = ""
                run += 1
                walk(child, location + f"/{local_tag(child)}[{index}]")
            else:
                child_kind = local_tag(child)
                inline += (
                    " "
                    if child_kind == "br"
                    else child.get("alt", "")
                    if child_kind == "img"
                    else visible(child)
                )
            inline += child.tail or ""
        add(inline, role, location + f"/inline[{run}]")

    walk(body, "body")
    return rows, omitted


def sentence_dashes(text: str) -> list[int]:
    """Identify internal sentence dashes without interpreting numeric ranges as pauses."""
    offsets = []
    for mark in re.finditer(r"—|(?<=\s)[–-](?=\s)", text):
        left = re.search(r"(\w+)\W*$", text[: mark.start()])
        right = re.match(r"\W*(\w+)", text[mark.end() :])
        if left and right and not (left[1].isdigit() and right[1].isdigit()):
            offsets.append(mark.start())
    return offsets


def pacing_text(segment: dict[str, Any]) -> str:
    """Mark only classified sentence dashes for the timing witness algorithm."""
    chosen = set(segment["sentence_dashes"])
    return "".join(
        "—" if index in chosen else "-" if char == "—" else char
        for index, char in enumerate(segment["text"])
    )


def split_unit(text: str, char_limit: int = 700, word_limit: int = 100) -> list[str]:
    """Split long prose while keeping each sentence dash with both anchor words."""
    spans = []
    for offset in sentence_dashes(text):
        left = re.search(r"\w+(?:[’']\w+)?\W*$", text[:offset])
        right = re.match(r"\W*\w+(?:[’']\w+)?", text[offset + 1 :])
        if left is None or right is None:
            raise ValueError("Could not resolve sentence-dash anchors")
        spans.append((left.start(), offset + 1 + right.end()))
    pieces = []
    cursor = 0
    while cursor < len(text):
        remaining = text[cursor:]
        words = list(re.finditer(r"\S+", remaining))
        if len(remaining) <= char_limit and len(words) <= word_limit:
            pieces.append(remaining.strip())
            break
        limit = cursor + min(
            char_limit,
            words[word_limit - 1].end() if len(words) >= word_limit else len(remaining),
        )
        candidates = [
            cursor + match.start()
            for match in re.finditer(r"\s+", text[cursor : limit + 1])
            if match.start() > 0
            and not any(a < cursor + match.start() < b for a, b in spans)
        ]
        if not candidates:
            raise ValueError(
                "An unbreakable token or connected dash-anchor span exceeds the passage limit"
            )
        preferred = [
            cut
            for cut in candidates
            if cut - cursor >= (limit - cursor) * 0.5
            and re.search(r"[.!?;:,][\"'’”)]?$", text[cursor:cut])
        ]
        cut = (preferred or candidates)[-1]
        pieces.append(text[cursor:cut].strip())
        cursor = cut
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    if clean(" ".join(pieces)) != clean(text) or sum(
        len(sentence_dashes(piece)) for piece in pieces
    ) != len(sentence_dashes(text)):
        raise ValueError(
            "Passage splitting changed source words or sentence-dash anchors"
        )
    return pieces


def cover_strip(data: bytes, target: Path) -> dict[str, Any]:
    """Append a readable AUDIOBOOK band while preserving original RGB pixels."""
    from PIL import Image, ImageDraw, ImageFont

    original = Image.open(io.BytesIO(data)).convert("RGB")
    width, height = original.size
    band_height = max(1, round(width * 0.125))
    scale = 4
    band = Image.new("RGB", (width * scale, band_height * scale), "#20252B")
    draw = ImageDraw.Draw(band)
    font_size = max(1, round(width * 0.058 * scale))
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont = ImageFont.load_default(
        size=font_size
    )
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
    ):
        try:
            font = ImageFont.truetype(candidate, font_size)
            break
        except OSError:
            continue
    label = "AUDIOBOOK"
    bounds = draw.textbbox((0, 0), label, font=font)
    text_width, text_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    icon_size = max(1, round(width * 0.065 * scale))
    gap = max(1, round(width * 0.022 * scale))
    x = (band.width - icon_size - gap - text_width) // 2
    y = (band.height - icon_size) // 2
    stroke = max(1, round(width * 0.006 * scale))
    draw.arc((x, y, x + icon_size, y + icon_size), 180, 360, fill="white", width=stroke)
    ear_width = max(1, round(icon_size * 0.21))
    ear_top, ear_bottom = y + round(icon_size * 0.45), y + icon_size
    draw.rounded_rectangle(
        (x, ear_top, x + ear_width, ear_bottom),
        radius=stroke,
        outline="white",
        width=stroke,
    )
    draw.rounded_rectangle(
        (x + icon_size - ear_width, ear_top, x + icon_size, ear_bottom),
        radius=stroke,
        outline="white",
        width=stroke,
    )
    draw.text(
        (x + icon_size + gap - bounds[0], (band.height - text_height) // 2 - bounds[1]),
        label,
        font=font,
        fill="white",
    )
    band = band.resize((width, band_height), Image.Resampling.LANCZOS)
    image = Image.new("RGB", (width, height + band_height))
    image.paste(original, (0, 0))
    image.paste(band, (0, height))
    if image.crop((0, 0, width, height)).tobytes() != original.tobytes():
        raise ValueError("Audiobook strip changed original cover pixels")
    image.save(target, "PNG")
    return {
        "path": str(target),
        "sha256": file_hash(target),
        "original_sha256": digest(data),
        "original_rgb_preserved": True,
        "headphone_icon": True,
    }


def prepare(
    epub: Path,
    job: Path,
    workspace: Path,
    directory: Path,
    voice: str,
    engine: str | None,
    code: str,
    asr_model: Path,
    cover: Path | None = None,
) -> dict[str, Any]:
    """Freeze a new source, voice, model, runtime, and explicit narration policy."""
    if job.exists() and any(job.iterdir()):
        raise ValueError(
            "Prepare needs a new empty job directory; resume the existing plan"
        )
    profile = load_profiles(directory)[voice]
    engine = engine or profile_defaults(profile)["default_engine"]
    if engine != "natural":
        raise ValueError(
            "The reusable audiobook pipeline currently requires the Natural engine"
        )
    if file_hash(directory / profile["reference"]) != profile["reference_sha256"]:
        raise ValueError("Voice reference checksum mismatch")
    provenance = (
        (directory / profile["reference_provenance"])
        if profile.get("reference_provenance")
        else None
    )
    if provenance is not None and not provenance.is_file():
        raise ValueError("Configured voice reference provenance is missing")
    model_path = (workspace / profile["engines"][engine]["path"]).resolve()
    asr_model = (workspace / asr_model).resolve()
    models = model_hashes(model_path)
    asr = model_hashes(asr_model)
    with zipfile.ZipFile(epub) as archive:
        if "META-INF/encryption.xml" in archive.namelist():
            raise ValueError("Encrypted EPUB resources require an unencrypted source")
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        paths = [
            e.get("full-path") for e in container.iter() if local_tag(e) == "rootfile"
        ]
        if not paths or not paths[0]:
            raise ValueError("EPUB package document is missing")
        package_path = archive_path("", paths[0])
        package = ET.fromstring(archive.read(package_path))
        title = next(
            (clean(e.text or "") for e in package.iter() if local_tag(e) == "title"),
            epub.stem,
        )
        authors = [
            clean(e.text or "") for e in package.iter() if local_tag(e) == "creator"
        ]
        items = {e.get("id"): e for e in package.iter() if local_tag(e) == "item"}
        cover_id = next(
            (
                e.get("content")
                for e in package.iter()
                if local_tag(e) == "meta" and e.get("name") == "cover"
            ),
            None,
        )
        cover_item = next(
            (
                e
                for e in items.values()
                if "cover-image" in e.get("properties", "").split()
            ),
            items.get(cover_id),
        )
        cover_data = (
            cover.read_bytes()
            if cover
            else archive.read(archive_path(package_path, cover_item.get("href", "")))
            if cover_item is not None
            else None
        )
        if cover_data is None:
            raise ValueError(
                "No EPUB cover found; supply --cover to preserve original artwork"
            )
        tracks: list[dict[str, Any]] = []
        ledger = []
        global_number = 0
        for entry in (e for e in package.iter() if local_tag(e) == "itemref"):
            item = items.get(entry.get("idref"))
            if item is None:
                raise ValueError("Spine references a missing resource")
            href = archive_path(package_path, item.get("href", ""))
            if (
                entry.get("linear", "yes") == "no"
                or "nav" in item.get("properties", "").split()
            ):
                ledger.append(
                    {
                        "href": href,
                        "reason": "nonlinear or navigation spine resource",
                        "sha256": digest(archive.read(href)),
                    }
                )
                continue
            if item.get("media-type") != "application/xhtml+xml":
                raise ValueError(f"Unsupported spine media type: {href}")
            extracted, omissions = units(archive.read(href), href, code)
            ledger.extend(omissions)
            if not extracted:
                continue
            track_number = len(tracks) + 1
            segments: list[dict[str, Any]] = []
            for unit_number, unit in enumerate(extracted, 1):
                pieces = split_unit(unit["text"])
                for index, text in enumerate(pieces):
                    global_number += 1
                    segments.append(
                        {
                            **unit,
                            "text": text,
                            "speech_text": text,
                            "text_sha256": digest(text),
                            "speech_text_sha256": digest(text),
                            "number": len(segments) + 1,
                            "global_number": global_number,
                            "unit": unit_number,
                            "unit_first": index == 0,
                            "unit_last": index == len(pieces) - 1,
                            "sentence_dashes": sentence_dashes(text),
                        }
                    )
            tracks.append(
                {
                    "track": track_number,
                    "title": next(
                        (u["text"] for u in extracted if u["kind"] == "heading"),
                        f"Chapter {track_number}",
                    ),
                    "source_href": href,
                    "source_sha256": digest(archive.read(href)),
                    "segments": segments,
                }
            )
    if not tracks:
        raise ValueError("EPUB has no narratable linear content")
    job.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(epub, job / "source.epub")
    shutil.copyfile(directory / profile["reference"], job / "reference.wav")
    frozen_profile = dict(profile, reference="reference.wav")
    if provenance is not None:
        shutil.copyfile(provenance, job / "reference_provenance.json")
        frozen_profile["reference_provenance"] = "reference_provenance.json"
        frozen_profile["reference_provenance_sha256"] = file_hash(
            job / "reference_provenance.json"
        )
    save_json(job / "voice_profile.json", frozen_profile)
    standard_hash = profile_identity(PAUSES)
    identity = {
        "version": 1,
        "epub_sha256": file_hash(job / "source.epub"),
        "profile": frozen_profile,
        "voice_id": voice,
        "engine": engine,
        "model_files": models,
        "asr_files": asr,
        "runtime": runtime.identity(),
        "tracks_sha256": profile_identity({"tracks": tracks}),
        "code_policy": code,
        "seed_base": profile["synthesis"]["seed"],
        "narration_standard_sha256": standard_hash,
    }
    plan = {
        "identity": identity,
        "identity_sha256": profile_identity(identity),
        "title": title,
        "author": ", ".join(authors) or "Unknown author",
        "narrator": profile["name"],
        "year": "",
        "scope": f"Linear EPUB content; standalone code policy: {code}.",
        "workspace": str(workspace.resolve()),
        "model_path": str(model_path),
        "asr_model": str(asr_model),
        "profile": frozen_profile,
        "engine": engine,
        "cover": cover_strip(cover_data, job / "cover.png"),
        "pause_seconds": PAUSES,
        "tracks": tracks,
        "total_segments": global_number,
        "sentence_dash_count": sum(
            len(s["sentence_dashes"]) for t in tracks for s in t["segments"]
        ),
        "omissions": ledger,
    }
    plan["plan_sha256"] = profile_identity(plan)
    save_json(job / "plan.json", plan)
    save_json(
        job / "status.json", {"state": "prepared", "total_segments": global_number}
    )
    return plan
