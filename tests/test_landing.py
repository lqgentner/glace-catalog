#!/usr/bin/env python3
"""Checks the landing-page PDF's source: what ``tools/landing.py`` reads.

The tool sets the README's HTML header as the PDF's title block, so it must
find the wordmark, the title, the tagline and every link in the tracked
``catalog/README.md``, and must stop on a header it cannot read rather than
render a PDF without it. Typesetting needs Quarto, which the gates do not
install, so this gate checks everything up to the Quarto call.

Reads no data and needs no network.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

from glace_catalog.metadata import vocabulary

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import landing  # noqa: E402

README = ROOT / "catalog" / "README.md"


def check_tracked_readme(checks: dict[str, bool]) -> None:
    """Check that the tracked README's header parses into the catalog's own values."""
    text = README.read_text()
    header_text, body = landing.split_readme(text)
    header = landing.parse_header(header_text)
    catalog = vocabulary().catalog
    hrefs = re.findall(r'<a href="([^"]+)"', header_text)

    checks["the title is the catalog's"] = header.title == catalog.title
    checks["the tagline is the catalog's"] = header.tagline == " ".join(catalog.tagline.split())
    checks["every header link is kept, in order"] = [href for _, href in header.links] == hrefs
    checks["link labels drop the leading emoji"] = all(
        label[:1].isalnum() for label, _ in header.links
    )
    checks["the wordmark has a tracked copy"] = landing.local_wordmark(header.wordmark).is_file()
    checks["the body starts after the header"] = bool(body) and not body.lstrip().startswith("<")
    checks["header and body together are the README"] = f"{header_text}\n\n{body}" == text

    document = landing.landing_document(header, body)
    checks["the document carries the title block"] = (
        f'title: "{catalog.title}"' in document and "subtitle: " in document
    )
    checks["the document links every header link"] = all(
        f"[{label}]({href})" in document for label, href in header.links
    )
    checks["the document ends with the README body"] = document.endswith(body)


def check_unreadable_headers(checks: dict[str, bool]) -> None:
    """Check that a README the tool cannot read stops it before Quarto runs."""
    try:
        landing.split_readme("# GLACE\n\nNo HTML header.\n")
        checks["a README without an HTML header is refused"] = False
    except ValueError:
        checks["a README without an HTML header is refused"] = True
    try:
        landing.parse_header('<h1 align="center">GLACE</h1>')
        checks["a header without wordmark, tagline or links is refused"] = False
    except ValueError as error:
        checks["a header without wordmark, tagline or links is refused"] = all(
            name in str(error) for name in ("wordmark", "tagline", "links")
        )


def main() -> int:
    """Run every check, printing one line per failure."""
    checks: dict[str, bool] = {}
    check_tracked_readme(checks)
    check_unreadable_headers(checks)
    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        print(f"FAILED: {len(failed)} of {len(checks)} landing check(s)")
        return 1
    print(f"OK: {len(checks)} landing check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
