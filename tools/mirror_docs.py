#!/usr/bin/env python3
"""Mirror the store's catalog documents into a local directory for ``rashid``.

``rashid check`` takes a local catalog directory and nothing else, while the
built tree lives on the object store. The documents are small — every
``*.json`` and ``*.md`` under the root, a few hundred files — and their asset
hrefs are absolute public URLs, so a local copy of the documents alone is enough
for ``--schema``, ``--data`` and ``--live`` to check what is actually published::

    uv run --env-file .env tools/mirror_docs.py /tmp/glace-docs
    uv run rashid check /tmp/glace-docs --schema --data --live
        --live-base-url https://data.source.coop/giuz/glace-alps

Build state (anything whose name starts with a dot) is not mirrored: it is not
part of the published tree.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from upath import UPath

from glace_catalog.config import settings

logger = logging.getLogger(Path(__file__).stem)

DOCUMENT_SUFFIXES = (".json", ".md")


def mirror_documents(root: UPath, out: Path) -> int:
    """Copy every catalog document under ``root`` into ``out``; return the count."""
    count = 0
    for suffix in DOCUMENT_SUFFIXES:
        for path in root.rglob(f"*{suffix}"):
            relative = path.path.removeprefix(root.path).lstrip("/")
            if any(part.startswith(".") for part in relative.split("/")):
                continue
            target = out / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            count += 1
    return count


def main() -> None:
    """Mirror the configured store's documents to the directory given."""
    parser = argparse.ArgumentParser(description="Mirror the store's catalog documents locally.")
    parser.add_argument("out", type=Path, help="local directory to write the documents into")
    parser.add_argument("--root", default=None, help="store root; defaults to GLACE_STORE_ROOT")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = UPath(settings.resolve_path(args.root) if args.root else settings.store_root)
    count = mirror_documents(root, args.out)
    logger.info("Mirrored %d document(s) from %s to %s", count, root, args.out)


if __name__ == "__main__":
    main()
