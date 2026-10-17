"""The MGRS tile grid and the glacier fractions it carries.

deep-glacier-mapping computes each tile's glacier fractions once per scope
(UPSTREAM.md D-1) and writes them to a table in the store. The build copies
them onto every tile item; the table itself is a build input and is not
published.

Finalization draws the grid from the item indexes into one vector PMTiles,
``tiles/grid.pmtiles``: a polygon per tile of any year, with both fractions as
properties. The grid does not depend on the year, so one archive serves all of
them, and a web map draws it without reading the item index.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyogrio
import shapely

if TYPE_CHECKING:
    from collections.abc import Sequence

    from upath import UPath

    from glace_catalog.scopes import Scope

GRID_ARCHIVE = "grid.pmtiles"
"""File name of the grid archive in the tiles collection."""

GRID_LAYER = "mgrs"
"""Vector layer of the grid archive."""

GRID_MIN_ZOOM = 0
"""Lowest zoom of the grid archive. GDAL keeps every cell down to zoom 0."""

GRID_MAX_ZOOM = 10
"""Highest zoom of the grid archive; clients overzoom beyond it."""

_INDEX_COLUMNS = {
    "glace:mgrs_tile": "mgrs_tile",
    "glace:glacier_fraction": "glacier_fraction",
    "glace:glacier_fraction_buffered": "glacier_fraction_buffered",
    "geometry": "geometry",
}
"""Item-index column of each grid column."""


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


def grid_frame(tables: Sequence[pa.Table]) -> gpd.GeoDataFrame:
    """Collect the tile grid from the per-year item indexes.

    Every tile item carries its cell's polygon and both glacier fractions, so
    the grid needs no other input. A tile present in any year is in the grid.

    Returns
    -------
    geopandas.GeoDataFrame
        One row per tile, sorted by ``mgrs_tile``, in EPSG:4326.

    Raises
    ------
    ValueError
        If a tile has no fraction, or different fractions or polygons in two
        years: the fraction table was rebuilt between years.
    """
    frames = [
        table.select(list(_INDEX_COLUMNS)).rename_columns(list(_INDEX_COLUMNS.values()))
        for table in tables
    ]
    rows = pa.concat_tables(frames).to_pandas()
    fractions = ["glacier_fraction", "glacier_fraction_buffered"]
    missing = sorted(rows.loc[rows[fractions].isna().any(axis=1), "mgrs_tile"].unique())
    if missing:
        msg = f"{len(missing)} tile(s) have no glacier fraction: {', '.join(missing[:5])}"
        raise ValueError(msg)
    distinct = rows.drop_duplicates()
    conflicting = sorted(distinct.loc[distinct["mgrs_tile"].duplicated(), "mgrs_tile"].unique())
    if conflicting:
        msg = (
            f"{len(conflicting)} tile(s) differ between years in fraction or polygon: "
            f"{', '.join(conflicting[:5])}; rebuild every year from one fraction table"
        )
        raise ValueError(msg)
    distinct = distinct.sort_values("mgrs_tile", ignore_index=True)
    return gpd.GeoDataFrame(
        distinct[["mgrs_tile", *fractions]].astype(dict.fromkeys(fractions, "float64")),
        geometry=shapely.from_wkb(distinct["geometry"]),
        crs="EPSG:4326",
    )


def write_grid(frame: gpd.GeoDataFrame, target: Path | UPath) -> None:
    """Write the grid as a vector PMTiles at ``target``.

    GDAL writes PMTiles to a local file only, so the archive is written to a
    temporary directory and then copied, also to object storage. It keeps
    every feature at every zoom: no simplification, no feature or size limit.
    """
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / GRID_ARCHIVE
        pyogrio.write_dataframe(
            frame,
            local,
            driver="PMTiles",
            layer=GRID_LAYER,
            dataset_options={
                "NAME": "GLACE tile grid",
                "DESCRIPTION": "MGRS tiles with their glacier fractions",
                "MINZOOM": GRID_MIN_ZOOM,
                "MAXZOOM": GRID_MAX_ZOOM,
                "SIMPLIFICATION": 0,
                "MAX_FEATURES": max(len(frame), 1),
                "MAX_SIZE": 100_000_000,
            },
        )
        target.write_bytes(local.read_bytes())
