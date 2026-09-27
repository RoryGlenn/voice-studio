"""Create an original two-chapter EPUB for the narration walkthrough."""

from __future__ import annotations

import argparse
import io
import zipfile
from pathlib import Path

from PIL import Image


def create_sample(output: Path) -> None:
    """Write a new book without overwriting an existing input."""
    cover = io.BytesIO()
    Image.new("RGB", (320, 480), "#35685c").save(cover, "PNG")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x") as book:
        book.writestr(
            "mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED
        )
        book.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            '<rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        book.writestr(
            "OPS/book.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="id">urn:voice-studio:sample-garden</dc:identifier>'
            "<dc:title>The Library Garden</dc:title><dc:creator>Voice Studio example</dc:creator>"
            '<dc:language>en</dc:language><meta property="dcterms:modified">2026-09-27T00:00:00Z</meta>'
            '</metadata><manifest><item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>',
        )
        book.writestr("OPS/cover.png", cover.getvalue())
        book.writestr(
            "OPS/nav.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            '<head><title>Contents</title></head><body><nav epub:type="toc"><ol>'
            '<li><a href="one.xhtml">The Garden</a></li><li><a href="two.xhtml">The Notebook</a></li>'
            "</ol></nav></body></html>",
        )
        for name, title, paragraph in [
            (
                "one",
                "The Garden",
                "A small garden grows beside the library. Each morning, we water the plants.",
            ),
            (
                "two",
                "The Notebook",
                "We record the weather in a notebook. On Friday, the first flower opens.",
            ),
        ]:
            book.writestr(
                f"OPS/{name}.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                f"<title>{title}</title></head><body><h1>{title}</h1><p>{paragraph}</p></body></html>",
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("data/books/incoming/library-garden.epub")
    )
    args = parser.parse_args()
    create_sample(args.output)
    print(args.output)
