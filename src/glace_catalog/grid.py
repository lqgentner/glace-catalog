"""The MGRS tile grid and the glacier fractions it carries.

deep-glacier-mapping computes each tile's glacier fractions once per scope
(UPSTREAM.md D-1) and writes them to a table in the store. The build copies
them onto every tile item; the table itself is a build input and is not
published.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from pathlib import Path

    from upath import UPath

    from glace_catalog.scopes import Scope


def read_glacier_fractions(path: Path | UPath, scope: Scope) -> dict[str, tuple[float, float]]:
    """Read the per-tile glacier fractions of ``scope``.

    Returns
    -------
    dict[str, tuple[float, float]]
        ``(glacier_fraction, glacier_fraction_buffered)`` by MGRS tile.

    Raises
    ------
    FileNotFoundError
        If the table is absent. Every tile item carries its glacier fraction.
    ValueError
        If the table was computed from another inventory or buffer than the
        scope's.
    """
    if not path.exists():
        msg = (
            f"{path} is absent. Run deep-glacier-mapping's "
            "scripts/inventories/derive-glacier-fractions.py for this scope first; "
            "the fractions are pinned per scope so they cannot drift between years."
        )
        raise FileNotFoundError(msg)
    with path.open("rb") as handle:
        frame = pd.read_parquet(handle)
    inventories = sorted(set(frame["rgi_version"].astype(str)))
    buffers = sorted(set(frame["buffer_m"].astype(float)))
    if inventories != [scope.glacier_inventory] or buffers != [scope.glacier_buffer_m]:
        msg = (
            f"{path} holds fractions from inventory {inventories} with buffer {buffers} m, "
            f"but the {scope.key} scope uses {scope.glacier_inventory} with "
            f"{scope.glacier_buffer_m:g} m"
        )
        raise ValueError(msg)
    return {
        str(row["mgrs_code"]): (
            float(row["glacier_fraction"]),
            float(row["glacier_fraction_buffered"]),
        )
        for row in frame.to_dict("records")
    }
