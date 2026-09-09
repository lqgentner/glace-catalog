"""Render ``catalog/`` from ``metadata/``, or check that it is current.

``catalog/`` is generated output that is committed. Edit the source under
``metadata/`` and run this; ``tests/test_render.py`` fails when the two differ.

Usage:
    uv run scripts/render-catalog.py            # write catalog/
    uv run scripts/render-catalog.py --check    # exit 1 if catalog/ is stale
"""

from __future__ import annotations

import argparse
import filecmp
import logging
from pathlib import Path
import tempfile

from glace_catalog.documents import ROOT, render_tree

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

    with tempfile.TemporaryDirectory() as tmp:
        stale = [
            path.relative_to(tmp)
            for path in render_tree(out=Path(tmp))
            if not (args.out / path.relative_to(tmp)).exists()
            or not filecmp.cmp(path, args.out / path.relative_to(tmp), shallow=False)
        ]
    for path in stale:
        logger.error("%s is stale", args.out / path)
    if stale:
        raise SystemExit(1)
    logger.info("%s is current", args.out)


if __name__ == "__main__":
    main()
