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

import json
from pathlib import Path
import sys
import tempfile
from typing import TYPE_CHECKING

import pandas as pd
from pmtiles.reader import MmapSource, Reader
import pyarrow as pa
import pyogrio
import shapely
from upath import UPath

from glace_catalog.grid import (
    GRID_LAYER,
    GRID_MAX_ZOOM,
    GRID_MIN_ZOOM,
    grid_frame,
    read_glacier_fractions,
    write_grid,
)
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
    untagged = work / "untagged.parquet"
    pd.read_parquet(good).drop(columns=["rgi_version", "buffer_m"]).to_parquet(untagged)
    message = _raises(ValueError, read_glacier_fractions, untagged, SCOPE)
    checks["fractions: a table without inventory and buffer is refused"] = message is not None
    checks["fractions: the refusal names the missing columns"] = "buffer_m, rgi_version" in (
        message or ""
    )

    checks["fractions: a missing table is refused"] = (
        _raises(FileNotFoundError, read_glacier_fractions, work / "absent.parquet", SCOPE)
        is not None
    )


# Three Alpine MGRS cells as corner polygons: (lon, lat) of the south-west
# corner, about 0.13 degrees wide and 0.09 high, as the tile items draw them.
CELLS = {
    "32TMS55": (8.00, 46.40, 0.010, 0.050),
    "32TMS56": (8.00, 46.49, 0.200, 0.300),
    "32TMS65": (8.13, 46.40, 0.000, 0.000),
}


def _year(
    tiles: list[str],
    fractions: dict[str, tuple[float, float]] | None = None,
    shift: dict[str, float] | None = None,
) -> pa.Table:
    """One year's item index, reduced to the columns the grid reads.

    ``shift`` moves a tile's polygon east by that many degrees.
    """
    fractions = fractions or {}
    shift = shift or {}
    rows = []
    for tile in tiles:
        lon, lat, plain, buffered = CELLS[tile]
        plain, buffered = fractions.get(tile, (plain, buffered))
        lon += shift.get(tile, 0.0)
        polygon = shapely.box(lon, lat, lon + 0.13, lat + 0.09)
        rows.append(
            {
                "geometry": shapely.to_wkb(polygon),
                "glace:mgrs_tile": tile,
                "glace:glacier_fraction": plain,
                "glace:glacier_fraction_buffered": buffered,
                "id": f"{tile}-x",
            },
        )
    return pa.Table.from_pylist(rows)


def check_grid_frame(checks: dict[str, bool]) -> None:
    """Check that the grid holds every tile of any year, once."""
    frame = grid_frame([_year(["32TMS55", "32TMS56"]), _year(list(CELLS))])
    checks["frame: one row per tile of any year"] = list(frame["mgrs_tile"]) == sorted(CELLS)
    checks["frame: columns"] = list(frame.columns) == [
        "mgrs_tile",
        "glacier_fraction",
        "glacier_fraction_buffered",
        "geometry",
    ]
    checks["frame: WGS84"] = frame.crs is not None and frame.crs.to_epsg() == 4326
    checks["frame: fractions are floats"] = (
        frame["glacier_fraction"].dtype.kind == "f"
        and frame["glacier_fraction_buffered"].dtype.kind == "f"
    )


def check_grid_conflict(checks: dict[str, bool]) -> None:
    """Check that a tile with different fractions in two years is refused."""
    changed = _year(list(CELLS), {"32TMS56": (0.25, 0.300)})
    message = _raises(ValueError, grid_frame, [_year(list(CELLS)), changed])
    checks["conflict: refused"] = message is not None
    checks["conflict: names the tile"] = "32TMS56" in (message or "")

    noisy = _year(list(CELLS), shift={"32TMS56": 1e-12})
    checks["conflict: polygon noise between years is accepted"] = (
        _raises(ValueError, grid_frame, [_year(list(CELLS)), noisy]) is None
    )
    moved = _year(list(CELLS), shift={"32TMS56": 0.01})
    message = _raises(ValueError, grid_frame, [_year(list(CELLS)), moved])
    checks["conflict: a moved polygon is refused"] = message is not None
    checks["conflict: the refusal names the polygon, not the fractions"] = "polygon" in (
        message or ""
    ) and "fraction" not in (message or "")

    rows = _year(["32TMS55"]).to_pylist()
    rows[0]["glace:glacier_fraction"] = None
    checks["conflict: a missing fraction is refused"] = (
        _raises(ValueError, grid_frame, [pa.Table.from_pylist(rows)]) is not None
    )


def check_grid_archive(checks: dict[str, bool], work: Path) -> None:
    """Check the archive's zooms, its one layer and every tile at both ends."""
    target = UPath(work / "store" / "tiles" / "grid.pmtiles")
    target.parent.mkdir(parents=True)
    write_grid(grid_frame([_year(list(CELLS))]), target)
    path = Path(str(target))
    checks["archive: written to the target"] = path.is_file()
    if not path.is_file():
        return
    write_grid(grid_frame([_year(list(CELLS))]), target)
    checks["archive: rewriting it leaves no temporary file"] = [
        child.name for child in path.parent.iterdir()
    ] == ["grid.pmtiles"]

    with path.open("rb") as handle:
        reader = Reader(MmapSource(handle))
        header = reader.header()
        metadata = reader.metadata()
    checks["archive: min zoom"] = header["min_zoom"] == GRID_MIN_ZOOM
    checks["archive: max zoom 10"] = header["max_zoom"] == GRID_MAX_ZOOM == 10
    layers = metadata.get("vector_layers", [])
    if isinstance(layers, str):
        layers = json.loads(layers)
    checks["archive: one layer, mgrs"] = (
        [layer["id"] for layer in layers] == [GRID_LAYER] == ["mgrs"]
    )

    for zoom in (GRID_MIN_ZOOM, GRID_MAX_ZOOM):
        decoded = pyogrio.read_dataframe(path, layer=GRID_LAYER, ZOOM_LEVEL=zoom)
        checks[f"archive: every tile at z{zoom}"] = set(decoded["mgrs_tile"]) == set(CELLS)
        decoded_fractions = {
            row["mgrs_tile"]: (
                round(float(row["glacier_fraction"]), 6),
                round(float(row["glacier_fraction_buffered"]), 6),
            )
            for row in decoded.to_dict("records")
        }
        checks[f"archive: fractions at z{zoom}"] = decoded_fractions == {
            tile: (plain, buffered) for tile, (_lon, _lat, plain, buffered) in CELLS.items()
        }


def main() -> int:
    """Run every check; print the failures."""
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        check_fraction_reader(checks, work)
        check_grid_frame(checks)
        check_grid_conflict(checks)
        check_grid_archive(checks, work)

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
