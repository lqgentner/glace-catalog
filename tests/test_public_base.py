#!/usr/bin/env python3
"""Every Source Cooperative URL in the repository names this catalog's prefix.

``catalog.publish.yaml`` sets where the store is published: ``write_prefix``
for the upload, ``public_base`` over HTTPS and ``public_s3_base`` over
anonymous S3. A URL that still names an earlier prefix keeps resolving while
the old objects exist, so a stale link goes unnoticed until they are removed.

The gate reads every file git tracks and checks each URL on a Source
Cooperative host, and each ``s3://`` URL in the upload bucket. Such a URL
passes when it starts with its base, or when it is a parent of the base, such
as the bare endpoint ``https://data.source.coop``. The product page on
``source.coop`` is derived from ``public_base``. No URL may name a retired
prefix, on any host.

Reads only tracked files and needs no network.

Run: uv run tests/test_public_base.py
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent

RETIRED_PREFIXES = ("lqgentner/glace-ch",)
"""Prefixes the catalog was published under before; no URL may name them."""

URL_PATTERN = re.compile(r"(?:https?|s3)://[^\s\"'<>()\[\]`|\\]+")
"""A URL in prose, Markdown, YAML, JSON or code."""

TRAILING = ".,;:…*"
"""Punctuation that ends a sentence rather than the URL."""


def bases() -> dict[str, str]:
    """Read the base each Source Cooperative URL must name, by kind."""
    config = yaml.safe_load((ROOT / "catalog.publish.yaml").read_text())
    public = str(config["public_base"]).rstrip("/")
    product = urlsplit(public).path.strip("/")
    return {
        "https": public,
        "s3": str(config["public_s3_base"]).rstrip("/"),
        "upload": str(config["write_prefix"]).rstrip("/"),
        "page": f"https://source.coop/{product}",
    }


def base_for(url: str, known: dict[str, str]) -> str | None:
    """Return the base ``url`` must agree with, or None if it is not a Source Cooperative URL."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "s3":
        if host.endswith(".source.coop"):
            return known["s3"]
        return known["upload"] if host == urlsplit(known["upload"]).hostname else None
    if host == "data.source.coop":
        return known["https"]
    if host in {"source.coop", "www.source.coop"}:
        return known["page"]
    return None


def agrees(url: str, base: str) -> bool:
    """Return whether ``url`` lies under ``base`` or is one of its parents."""
    url = url.rstrip("/")
    return url == base or url.startswith(f"{base}/") or base.startswith(f"{url}/")


def tracked_files() -> list[Path]:
    """Every file git tracks, as an absolute path."""
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [ROOT / name for name in listing.split("\0") if name]


def main() -> int:
    """Run the gate."""
    known = bases()
    errors: list[str] = []
    checked = 0
    for path in tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for match in URL_PATTERN.finditer(line):
                url = match.group().rstrip(TRAILING)
                where = f"{path.relative_to(ROOT)}:{number}"
                retired = [prefix for prefix in RETIRED_PREFIXES if prefix in url]
                if retired:
                    errors.append(f"{where}: {url} names the retired prefix {retired[0]}")
                    continue
                base = base_for(url, known)
                if base is None:
                    continue
                checked += 1
                if not agrees(url, base):
                    errors.append(f"{where}: {url} is not under {base}")

    for error in errors:
        print(f"error  {error}")
    if errors:
        print(f"FAILED: {len(errors)} URL(s) name another prefix than catalog.publish.yaml")
        return 1
    print(f"OK: {checked} Source Cooperative URL(s) name {known['https']} or its siblings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
