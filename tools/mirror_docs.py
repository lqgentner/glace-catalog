#!/usr/bin/env python3
r"""Copy the store's catalog documents into a local directory for ``rashid``.

``rashid check`` reads a local directory, and the built store may sit on
object storage. The mirror holds every ``*.json`` and ``*.md``, about 26 500
files for the Alps. The collection assets' hrefs are absolute public URLs, so
``--data`` and ``--live`` check those published bytes and their server; the
item assets' relative hrefs do not resolve in the mirror and go unread. Run it
through ``make conformance`` or ``make conformance-live``.

``--drop-self`` leaves the root catalog's ``self`` link out of the mirror.
rashid reads the publish base from that link and, with ``--live``, then
requests every item asset under it one at a time: about ten hours for the
Alps. ``make links-live`` requests those hrefs in parallel instead.

Paths with a dot-prefixed part are build state and are skipped.
"""

from __future__ import annotations

import argparse
import json
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


def drop_self_link(out: Path) -> None:
    """Remove the ``self`` link from the mirrored root catalog."""
    path = out / "catalog.json"
    catalog = json.loads(path.read_text())
    catalog["links"] = [link for link in catalog["links"] if link.get("rel") != "self"]
    path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")


def main() -> None:
    """Mirror the configured store's documents to the directory given."""
    parser = argparse.ArgumentParser(description="Mirror the store's catalog documents locally.")
    parser.add_argument("out", type=Path, help="local directory to write the documents into")
    parser.add_argument(
        "--drop-self",
        action="store_true",
        help="leave the root catalog's self link out, so rashid --live probes no item asset",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = UPath(settings.store_root)
    count = mirror_documents(root, args.out)
    logger.info("Mirrored %d document(s) from %s to %s", count, root, args.out)
    if args.drop_self:
        drop_self_link(args.out)


if __name__ == "__main__":
    main()
