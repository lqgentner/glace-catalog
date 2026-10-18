#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright the portolan-catalog-template authors
#
# Taken from portolan-sdi/portolan-catalog-template and modified. See
# THIRD_PARTY_NOTICES.md; the deviations are marked where they occur.
"""Every relative link and asset href resolves to a file that exists.

This catches the most common hand-edit mistake: a link to a directory that
does not exist yet. It needs no dependencies or network.

CI has the metadata but not the data files, which .gitignore keeps out of git.
Set CI_LIGHT=1 there to skip asset hrefs that end in a data suffix. Every
structural link is still checked. Leave CI_LIGHT unset locally, where the data
is on disk, to check every href.

Run: uv run tests/test_links.py

Local deviation from upstream: ``--remote DIR`` checks a published store
instead. DIR holds the store's documents, as ``tools/mirror_docs.py`` copies
them. Each of those files, and every link and asset href they resolve to under
``public_base`` (``catalog.publish.yaml``), gets a HEAD request at its public
URL. An href that answers other than 200, or with a Content-Length other than
the asset's ``file:size`` or the mirrored file's size, is an error. Absolute
hrefs elsewhere are not requested. Run it after an upload, through
``make links-live``.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parent.parent
# Local deviation from upstream: the catalog directory is fixed here.
BASE = ROOT / "catalog"

errors: list[str] = []
skipped = 0

CI_LIGHT = os.environ.get("CI_LIGHT") == "1"
# The exemption matches file suffixes only, so it covers exactly these data
# formats however the catalog grows.
DATA_SUFFIXES = (
    ".parquet", ".pmtiles", ".tif", ".tiff", ".copc.laz", ".laz", ".gpkg",
    ".zarr", ".geojsonl", ".shp", ".zip",
)


def is_remote(href: str) -> bool:
    return "://" in href or href.startswith(("#", "mailto:"))


def is_unpublished_data(href: str) -> bool:
    """True when href points at a file kept out of git on purpose."""
    return CI_LIGHT and href.lower().endswith(DATA_SUFFIXES)


def stac_documents(base: Path = BASE) -> list[Path]:
    """Every STAC object under the published directory."""
    out = []
    for path in sorted(base.rglob("*.json")):
        if any(part.startswith(".") for part in path.relative_to(base).parts):
            continue
        if path.name.endswith(".style.json") or "styles" in path.parts:
            continue
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{path.relative_to(base)}: invalid JSON ({exc})")
            continue
        if isinstance(doc, dict) and doc.get("type") in {
            "Catalog", "Collection", "Feature"
        }:
            out.append(path)
    return out


# Local deviation from upstream: the remote mode, described in the docstring.
REMOTE_WORKERS = 32
REMOTE_ATTEMPTS = 4
REMOTE_TIMEOUT_S = 60
REMOTE_SHOWN = 100
USER_AGENT = "glace-catalog test_links.py"
_connections = threading.local()


def remote_targets(base: Path, public_base: str) -> dict[str, int | None]:
    """Every public URL the documents under ``base`` publish or name, with its size."""
    prefix = f"{public_base.rstrip('/')}/"
    targets: dict[str, int | None] = {}
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if path.is_file() and not any(part.startswith(".") for part in relative.parts):
            targets[prefix + relative.as_posix()] = path.stat().st_size
    for path in stac_documents(base):
        url = prefix + path.relative_to(base).as_posix()
        doc = json.loads(path.read_text())
        hrefs = [(link.get("href", ""), None) for link in doc.get("links", [])]
        hrefs += [
            (asset.get("href", ""), asset.get("file:size"))
            for asset in (doc.get("assets") or {}).values()
        ]
        for href, size in hrefs:
            if not href or href.startswith(("#", "mailto:")):
                continue
            target = urljoin(url, href)
            if target.startswith(prefix) and targets.get(target) is None:
                targets[target] = size
    return targets


def head(url: str) -> tuple[int, int | None]:
    """Status and Content-Length of a HEAD request; status 0 if it never answered.

    Each thread keeps one connection open. Dropped connections, 429 and 5xx
    answers are retried with a growing pause.
    """
    parts = urlsplit(url)
    for attempt in range(REMOTE_ATTEMPTS):
        if attempt:
            time.sleep(2**attempt)
        connection = getattr(_connections, parts.netloc, None)
        if connection is None:
            kind = HTTPSConnection if parts.scheme == "https" else HTTPConnection
            connection = kind(parts.netloc, timeout=REMOTE_TIMEOUT_S)
            setattr(_connections, parts.netloc, connection)
        try:
            connection.request("HEAD", parts.path, headers={"User-Agent": USER_AGENT})
            response = connection.getresponse()
            response.read()
        except (OSError, HTTPException):
            connection.close()
            setattr(_connections, parts.netloc, None)
            continue
        if response.status == 429 or response.status >= 500:
            continue
        length = response.getheader("Content-Length")
        return response.status, int(length) if length is not None else None
    return 0, None


def check_remote(base: Path, public_base: str, workers: int) -> list[str]:
    """HEAD every target of :func:`remote_targets`; return one line per failure."""
    targets = remote_targets(base, public_base)
    print(f"HEAD {len(targets)} URL(s) under {public_base}, {workers} at a time", flush=True)
    failures: list[str] = []
    step = max(len(targets) // 20, 1)
    started = time.monotonic()
    with ThreadPoolExecutor(workers) as pool:
        answers = pool.map(head, targets)
        answered = zip(targets, answers, strict=True)
        for done, (url, (status, length)) in enumerate(answered, start=1):
            expected = targets[url]
            if status == 0:
                failures.append(f"{url}: no answer after {REMOTE_ATTEMPTS} attempts")
            elif status != 200:
                failures.append(f"{url}: HTTP {status}")
            elif expected is not None and length != expected:
                failures.append(f"{url}: {length} bytes, expected {expected}")
            if done % step == 0:
                print(
                    f"       {done}/{len(targets)} after {time.monotonic() - started:.0f} s, "
                    f"{len(failures)} failure(s)",
                    flush=True,
                )
    return failures


def remote_main(argv: list[str]) -> int:
    """Check a published store; see the module docstring."""
    parser = argparse.ArgumentParser(description="Check every published href over HTTP.")
    parser.add_argument("--remote", type=Path, required=True, metavar="DIR",
                        help="local copy of the store's documents (tools/mirror_docs.py)")
    parser.add_argument("--base-url", help="public URL of DIR; default: public_base")
    parser.add_argument("--workers", type=int, default=REMOTE_WORKERS,
                        help="requests in flight")
    args = parser.parse_args(argv)
    base_url = args.base_url
    if base_url is None:
        import yaml

        config = yaml.safe_load((ROOT / "catalog.publish.yaml").read_text())
        base_url = str(config["public_base"])
    if not (args.remote / "catalog.json").is_file():
        print(f"error  {args.remote} holds no catalog.json; mirror the store into it first")
        return 1
    failures = check_remote(args.remote, base_url, args.workers)
    for failure in failures[:REMOTE_SHOWN]:
        print(f"error  {failure}")
    if len(failures) > REMOTE_SHOWN:
        print(f"error  ... and {len(failures) - REMOTE_SHOWN} more")
    if failures:
        print(f"FAILED: {len(failures)} published href(s) do not answer as recorded")
        return 1
    print(f"OK: every published href under {base_url} answers with its recorded size")
    return 0


if __name__ == "__main__" and len(sys.argv) > 1:
    raise SystemExit(remote_main(sys.argv[1:]))

documents = stac_documents()
checked = 0

for path in documents:
    doc = json.loads(path.read_text())
    rel_path = path.relative_to(ROOT)

    for link in doc.get("links", []):
        href = link.get("href", "")
        if not href or is_remote(href):
            continue
        checked += 1
        if not (path.parent / href).resolve().exists():
            errors.append(
                f"{rel_path}: rel:{link.get('rel')} -> {href} does not exist"
            )

    for key, asset in (doc.get("assets") or {}).items():
        href = asset.get("href", "")
        if not href or is_remote(href):
            continue
        if is_unpublished_data(href):
            skipped += 1
            continue
        checked += 1
        if not (path.parent / href).resolve().exists():
            errors.append(f"{rel_path}: asset {key} -> {href} does not exist")

if skipped:
    print(
        f"note   {skipped} data href(s) not checked: CI_LIGHT is set and the "
        "bytes live in\n       object storage, not git. Run without CI_LIGHT "
        "locally to check them."
    )

if errors:
    print("\n".join(f"error  {e}" for e in errors))
    raise SystemExit(1)

print(f"OK: {checked} relative href(s) across {len(documents)} object(s)")
