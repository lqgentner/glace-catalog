#!/usr/bin/env python3
r"""Render the root README as a PDF for the dataset's landing page at LaRS.

The PDF is ``catalog/README.md``, the README Source Cooperative shows,
typeset with Quarto's Typst format. The README opens with an HTML header (the
wordmark, title, tagline and links), which Typst does not read. This tool
takes those four from the header and sets them as the PDF's title block, then
appends the rest of the README unchanged. Run it through ``make landing``
after ``make render``.

Quarto must be installed; it is not a Python dependency.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import logging
from pathlib import Path, PurePosixPath
import re
import shutil
from string import Template
import subprocess
import tempfile
from typing import override
from urllib.parse import urlparse

from glace_catalog.stac.items import DATASET_VERSION

logger = logging.getLogger(Path(__file__).stem)

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / "catalog"
OUT_DIR = ROOT / ".cache" / "landing"

LINK_COLOUR = "#1f5fa8"
"""Colour of link text in the PDF, which otherwise shows links as plain text."""

DOCUMENT = Template("""---
title: $title
subtitle: $tagline
format:
  typst:
    papersize: a4
    margin: {x: 2.2cm, y: 2.2cm}
    include-before-body:
      text: |
        #set par(justify: false)
        #show link: set text(fill: rgb("$link_colour"))
---

![]($wordmark){width=45% fig-align="center"}

```{=typst}
#align(center)[
```

$links

```{=typst}
]
```

$body""")
"""The Quarto document: title block, wordmark, centred links, then the README.

Justification is off because it stretches the words of narrow table cells.
"""


@dataclass(frozen=True)
class Header:
    """What the README's HTML header shows, in reading order."""

    wordmark: str
    title: str
    tagline: str
    links: tuple[tuple[str, str], ...]
    """``(label, href)`` pairs; a label drops the emoji the README puts first."""


class _HeaderParser(HTMLParser):
    """Collect the wordmark, the ``<h1>``, the tagline and the links."""

    def __init__(self) -> None:
        super().__init__()
        self.wordmark = ""
        self.title = ""
        self.tagline = ""
        self.links: list[tuple[str, str]] = []
        self._in_title = False
        self._after_title = False
        self._breaks = 0
        self._href: str | None = None
        self._label = ""

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "img" and not self.wordmark:
            self.wordmark = values.get("src") or ""
        elif tag == "h1":
            self._in_title = True
        elif tag == "br" and self._after_title:
            self._breaks += 1
        elif tag == "a":
            self._href = values.get("href") or ""
            self._label = ""

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self._in_title = False
            self._after_title = True
        elif tag == "a" and self._href is not None:
            label = re.sub(r"^\W+", "", " ".join(self._label.split()))
            self.links.append((label, self._href))
            self._href = None

    @override
    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif self._href is not None:
            self._label += data
        elif self._after_title and self._breaks == 0:
            # The tagline is the text between the title and the first break.
            self.tagline += data


def split_readme(text: str) -> tuple[str, str]:
    """Split the README into its HTML header and its Markdown body.

    The header is the leading run of blocks that open with an HTML tag.

    Raises
    ------
    ValueError
        If the README does not open with an HTML header.
    """
    blocks = text.split("\n\n")
    count = 0
    while count < len(blocks) and blocks[count].lstrip().startswith("<"):
        count += 1
    if count == 0:
        msg = "the README does not open with an HTML header"
        raise ValueError(msg)
    return "\n\n".join(blocks[:count]), "\n\n".join(blocks[count:])


def parse_header(header: str) -> Header:
    """Read the wordmark, title, tagline and links from the README's header.

    Raises
    ------
    ValueError
        If any of the four is missing, so a changed template fails here
        rather than producing a PDF without it.
    """
    parser = _HeaderParser()
    parser.feed(header)
    parser.close()
    parsed = Header(
        wordmark=parser.wordmark,
        title=" ".join(parser.title.split()),
        tagline=" ".join(parser.tagline.split()),
        links=tuple(parser.links),
    )
    missing = [
        name for name in ("wordmark", "title", "tagline", "links") if not getattr(parsed, name)
    ]
    if missing:
        msg = f"the README's header has no {', '.join(missing)}"
        raise ValueError(msg)
    return parsed


def local_wordmark(src: str) -> Path:
    """Return the tracked file the header's wordmark URL names.

    The README links the published copy; the PDF embeds the one under
    ``catalog/_assets/``.
    """
    path = CATALOG_DIR / "_assets" / PurePosixPath(urlparse(src).path).name
    if not path.is_file():
        msg = f"the README's wordmark {src} has no tracked copy at {path}"
        raise FileNotFoundError(msg)
    return path


def landing_document(header: Header, body: str) -> str:
    """Return the Quarto document for the PDF, given the parsed README."""
    return DOCUMENT.substitute(
        # JSON strings are valid YAML scalars, whatever the text holds.
        title=json.dumps(header.title, ensure_ascii=False),
        tagline=json.dumps(header.tagline, ensure_ascii=False),
        link_colour=LINK_COLOUR,
        wordmark=PurePosixPath(urlparse(header.wordmark).path).name,
        links=" ·\n".join(f"[{label}]({href})" for label, href in header.links),
        body=body,
    )


def default_output() -> Path:
    """Return ``.cache/landing/{catalog id}-{version}-README.pdf``."""
    catalog_id = json.loads((CATALOG_DIR / "catalog.json").read_text())["id"]
    return OUT_DIR / f"{catalog_id}-{DATASET_VERSION}-README.pdf"


def render_pdf(document: str, wordmark: Path, out: Path) -> None:
    """Typeset ``document`` with Quarto's Typst format and write it to ``out``."""
    quarto = shutil.which("quarto")
    if quarto is None:
        msg = "quarto is not installed; see https://quarto.org/docs/get-started/"
        raise SystemExit(msg)
    with tempfile.TemporaryDirectory(prefix="landing-") as work:
        work_dir = Path(work)
        (work_dir / "landing.qmd").write_text(document)
        shutil.copyfile(wordmark, work_dir / wordmark.name)
        # A fixed argument list with Quarto from PATH, never a shell.
        result = subprocess.run(  # noqa: S603
            [quarto, "render", "landing.qmd", "--to", "typst", "--output", "landing.pdf"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            msg = f"quarto failed:\n{result.stderr.strip()}"
            raise SystemExit(msg)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(work_dir / "landing.pdf", out)


def main() -> None:
    """Render ``catalog/README.md`` to the PDF path given, or the default."""
    parser = argparse.ArgumentParser(description="Render the root README as a landing-page PDF.")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="PDF to write; defaults to .cache/landing/{catalog id}-{version}-README.pdf",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    header_text, body = split_readme((CATALOG_DIR / "README.md").read_text())
    header = parse_header(header_text)
    out = args.out or default_output()
    render_pdf(landing_document(header, body), local_wordmark(header.wordmark), out)
    logger.info("Wrote %s", out)


if __name__ == "__main__":
    main()
