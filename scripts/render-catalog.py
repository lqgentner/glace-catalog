"""Render ``catalog/`` from ``metadata/``.

``catalog/`` is generated output that is committed. Edit the source under
``metadata/`` and run this; ``tests/test_render.py`` fails when the two differ.

Usage:
    uv run scripts/render-catalog.py
"""

from __future__ import annotations

import logging
from pathlib import Path

from glace_catalog.documents import ROOT, render_tree

logger = logging.getLogger(Path(__file__).stem)


def main() -> None:
    """Render the tracked documents."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out = ROOT / "catalog"
    written = render_tree(out=out)
    logger.info("Rendered %d document(s) into %s", len(written), out)


if __name__ == "__main__":
    main()
