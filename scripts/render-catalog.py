"""Render ``catalog/`` from ``metadata/``, or check that it is current.

``catalog/`` is generated output that is committed. Edit the source under
``metadata/`` and run this; ``tests/test_render.py`` fails when the two differ.

Usage:
    uv run scripts/render-catalog.py            # write catalog/
    uv run scripts/render-catalog.py --check    # exit 1 if catalog/ is stale
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from glace_catalog.documents import ROOT, render_tree, stale_documents
from glace_catalog.metadata import load_vocabulary

logger = logging.getLogger(Path(__file__).stem)


def main() -> None:
    """Render the tracked documents, or report which are stale."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "catalog")
    parser.add_argument("--check", action="store_true", help="compare rather than write")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.check:
        written = render_tree(out=args.out)
        logger.info("Rendered %d document(s) into %s", len(written), args.out)
        return

    stale = stale_documents(load_vocabulary(), args.out)
    for problem in stale:
        logger.error("%s", problem)
    if stale:
        raise SystemExit(1)
    logger.info("%s is current", args.out)


if __name__ == "__main__":
    main()
