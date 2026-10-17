#!/usr/bin/env python3
"""Checks the tile grid: its fraction input and the archive built from it.

The build reads each tile's glacier fractions from a table that
deep-glacier-mapping writes once per scope (UPSTREAM.md D-1). The table pins
one inventory and one buffer, and a table computed with others would give the
items different values from the ones the catalog describes, so reading it
checks both against the scope.

Writes only into a temporary directory, reads no data and needs no network.

Run: uv run tests/test_grid.py
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from typing import TYPE_CHECKING

import pandas as pd

from glace_catalog.grid import read_glacier_fractions
from glace_catalog.scopes import get_scope

if TYPE_CHECKING:
    from collections.abc import Callable

SCOPE = get_scope("alps")


def _fractions(path: Path, *, rgi_version: str = "RGI7-C-11", buffer_m: float = 250.0) -> Path:
    """Write a two-tile fraction table as deep-glacier-mapping does."""
    pd.DataFrame(
        {
            "mgrs_code": ["32TMS55", "32TMS56"],
            "glacier_fraction": [0.1, 0.0],
            "glacier_fraction_buffered": [0.2, 0.05],
            "rgi_version": [rgi_version] * 2,
            "buffer_m": [buffer_m] * 2,
        },
    ).to_parquet(path)
    return path


def _raises(error: type[Exception], call: Callable[..., object], *args: object) -> str | None:
    """Return the message ``call(*args)`` raises as ``error``, or None if it does not."""
    try:
        call(*args)
    except error as raised:
        return str(raised)
    return None


def check_fraction_reader(checks: dict[str, bool], work: Path) -> None:
    """Check that the reader returns the fractions and refuses another inventory."""
    good = _fractions(work / "good.parquet")
    checks["fractions: values by tile"] = read_glacier_fractions(good, SCOPE) == {
        "32TMS55": (0.1, 0.2),
        "32TMS56": (0.0, 0.05),
    }

    message = _raises(
        ValueError,
        read_glacier_fractions,
        _fractions(work / "rgi6.parquet", rgi_version="RGI6"),
        SCOPE,
    )
    checks["fractions: another inventory is refused"] = message is not None
    checks["fractions: the refusal names the expected inventory"] = "RGI7-C-11" in (message or "")

    checks["fractions: another buffer is refused"] = (
        _raises(
            ValueError,
            read_glacier_fractions,
            _fractions(work / "buffer.parquet", buffer_m=500.0),
            SCOPE,
        )
        is not None
    )
    checks["fractions: a missing table is refused"] = (
        _raises(FileNotFoundError, read_glacier_fractions, work / "absent.parquet", SCOPE)
        is not None
    )


def main() -> int:
    """Run every check; print the failures."""
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        check_fraction_reader(checks, work)

    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        print(f"FAILED: {len(failed)} of {len(checks)} check(s)")
        return 1
    print(f"OK: {len(checks)} grid check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
