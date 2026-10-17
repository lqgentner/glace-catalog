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
from secrets import token_hex
import tempfile
from typing import TYPE_CHECKING

from freezebase.s3 import LOCAL_PROTOCOLS
import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyogrio
import shapely
from upath import UPath

if TYPE_CHECKING:
    from collections.abc import Sequence

    from glace_catalog.scopes import Scope

GRID_ARCHIVE = "grid.pmtiles"
"""File name of the grid archive in the tiles collection."""

GRID_LAYER = "mgrs"
"""Vector layer of the grid archive."""

GRID_MIN_ZOOM = 0
"""Lowest zoom of the grid archive. GDAL keeps every cell down to zoom 0."""

GRID_MAX_ZOOM = 10
"""Highest zoom of the grid archive; clients overzoom beyond it."""

POLYGON_TOLERANCE_DEG = 1e-9
"""Largest coordinate difference, in degrees, between one tile's polygons in two years."""

FRACTION_COLUMNS = (
    "mgrs_code",
    "glacier_fraction",
    "glacier_fraction_buffered",
    "rgi_version",
    "buffer_m",
)
"""Columns the fraction table must have."""

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
        If the table lacks one of :data:`FRACTION_COLUMNS`, or was computed
        from another inventory or buffer than the scope's.
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
    missing = sorted(set(FRACTION_COLUMNS) - set(frame.columns))
    if missing:
        msg = (
            f"{path} has no column {', '.join(missing)}. Rerun deep-glacier-mapping's "
            "scripts/inventories/derive-glacier-fractions.py, which records the "
            "inventory and buffer the fractions were computed with."
        )
        raise ValueError(msg)
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
    ``tables`` run from the earliest year to the latest, and each tile keeps
    its latest polygon.

    Returns
    -------
    geopandas.GeoDataFrame
        One row per tile, sorted by ``mgrs_tile``, in EPSG:4326.

    Raises
    ------
    ValueError
        If a tile has no fraction, or different fractions in two years (the
        fraction table was rebuilt between years), or polygons that differ
        between years by more than :data:`POLYGON_TOLERANCE_DEG`.
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

    values = rows[["mgrs_tile", *fractions]].drop_duplicates()
    conflicting = sorted(values.loc[values["mgrs_tile"].duplicated(), "mgrs_tile"].unique())
    if conflicting:
        msg = (
            f"{len(conflicting)} tile(s) have different glacier fractions in different "
            f"years: {', '.join(conflicting[:5])}; rebuild every year from one fraction table"
        )
        raise ValueError(msg)

    # Each year computes a tile's polygon anew, so a library update between
    # builds can change its last digits.
    polygons = shapely.from_wkb(rows["geometry"].to_numpy())
    latest = rows.reset_index(drop=True).drop_duplicates("mgrs_tile", keep="last")
    reference = dict(zip(latest["mgrs_tile"], polygons[latest.index], strict=True))
    moved = ~shapely.equals_exact(
        polygons,
        [reference[tile] for tile in rows["mgrs_tile"]],
        tolerance=POLYGON_TOLERANCE_DEG,
    )
    if moved.any():
        tiles = sorted(rows.loc[moved, "mgrs_tile"].unique())
        msg = (
            f"{len(tiles)} tile(s) have polygons that differ between years: "
            f"{', '.join(tiles[:5])}; rebuild the items of the years that disagree"
        )
        raise ValueError(msg)

    latest = latest.sort_values("mgrs_tile")
    return gpd.GeoDataFrame(
        latest[["mgrs_tile", *fractions]].astype(dict.fromkeys(fractions, "float64")),
        geometry=polygons[latest.index],
        crs="EPSG:4326",
    ).reset_index(drop=True)


def write_grid(frame: gpd.GeoDataFrame, target: Path | UPath) -> None:
    """Write the grid as a vector PMTiles at ``target``.

    GDAL writes PMTiles to a local file only, so the archive is written to a
    temporary directory and then copied, also to object storage. It keeps
    every feature at every zoom: no simplification, no feature or size limit.

    On object storage one put replaces the archive at once. On a local disk
    the copy goes to a sibling temporary file that is renamed over the
    target, so a reader never sees a partial archive.
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
        _replace_bytes(target, local.read_bytes())


def _replace_bytes(target: Path | UPath, payload: bytes) -> None:
    """Write ``payload`` to ``target`` in one step, as :func:`write_grid` describes."""
    if UPath(target).protocol not in LOCAL_PROTOCOLS:
        target.write_bytes(payload)
        return
    local = Path(UPath(target).path)
    # The leading dot keeps a file left by a killed run out of the upload.
    partial = local.with_name(f".{local.name}.{token_hex(8)}.partial")
    try:
        partial.write_bytes(payload)
        partial.replace(local)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
