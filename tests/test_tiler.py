#!/usr/bin/env python3
"""Checks the in-tree PMTiles tiler.

The tiler is adapted from rio-pmtiles. This gate checks which tiles it
enumerates, that it skips empty tiles and keeps edge tiles, and that it reads
each tile at the right resolution. The last matters most. The tiler picks an
overview level for each tile, and a choice carried over from one tile to the
finer tiles after it would read max-zoom tiles from a coarser overview and
upsample them. The archive would be valid, only blurred.

The test builds a synthetic RGBA raster with known pixels: a 0/255
checkerboard on the z13 pixel grid, with one 2x2 block of tiles transparent. It
tiles the raster twice, once as written and once with average overviews, which
turn the checkerboard flat grey. Every z13 tile of both archives must be the
checkerboard, the transparent block must be absent, and the enumeration must
match the scope's grid. Tiles are PNG, so comparisons are exact.

Writes only into a temporary directory, reads no data and needs no network.

Run: uv run tests/test_tiler.py
"""

from __future__ import annotations

import io
from pathlib import Path
import shutil
import sys
import tempfile

import numpy as np
from PIL import Image
from pmtiles.reader import MmapSource, Reader, all_tiles
from pmtiles.tile import HeaderDict, TileType, tileid_to_zxy
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds

from glace_catalog.scopes import MOSAIC_ZOOM, TILE_PX, get_scope, zoom_resolution
from glace_catalog.tiler import (
    WORKER_GDAL_CACHE_MB,
    TileSpec,
    spawn_pool,
    tile_bounds,
    tiles_covering,
    worker_gdal_cache,
    write_archive,
)

ORIGIN_X, ORIGIN_Y = 4252, 2872
"""Upper-left z13 tile of the synthetic raster; multiples of 4, so z11 is one tile."""

TILES_ACROSS = 4
"""The raster is this many z13 tiles on a side."""

MIN_ZOOM = 9
"""Deep enough below z13 that the tiler selects an overview where one exists."""


def write_source(path: Path, *, overviews: bool) -> None:
    """Write the checkerboard raster; its lower-right 2x2 tile block is transparent."""
    side = TILES_ACROSS * TILE_PX
    rows, cols = np.indices((side, side))
    checker = (((rows + cols) % 2) * 255).astype("uint8")
    alpha = np.full((side, side), 255, dtype="uint8")
    alpha[2 * TILE_PX :, 2 * TILE_PX :] = 0
    left, _, _, top = tile_bounds(MOSAIC_ZOOM, ORIGIN_X, ORIGIN_Y)
    resolution = zoom_resolution(MOSAIC_ZOOM)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        dtype="uint8",
        count=4,
        width=side,
        height=side,
        crs="EPSG:3857",
        transform=rasterio.transform.from_origin(left, top, resolution, resolution),
        tiled=True,
        blockxsize=TILE_PX,
        blockysize=TILE_PX,
    ) as dst:
        for band in (1, 2, 3):
            dst.write(checker, band)
        dst.write(alpha, 4)
    if overviews:
        with rasterio.open(path, "r+") as dst:
            dst.build_overviews([2, 4], Resampling.average)


def read_archive(path: Path) -> tuple[HeaderDict, dict[tuple[int, int, int], np.ndarray]]:
    """Header and decoded RGBA pixels per ``(z, x, y)`` of an archive."""
    with path.open("rb") as handle:
        source = MmapSource(handle)
        header = Reader(source).header()
        tiles = {
            zxy: np.asarray(Image.open(io.BytesIO(payload)).convert("RGBA"))
            for zxy, payload in all_tiles(source)
        }
    return header, tiles


def expected_z13_tiles() -> set[tuple[int, int, int]]:
    """List the opaque z13 tiles: the 4x4 block minus its transparent 2x2 corner."""
    return {
        (MOSAIC_ZOOM, ORIGIN_X + dx, ORIGIN_Y + dy)
        for dx in range(TILES_ACROSS)
        for dy in range(TILES_ACROSS)
        if not (dx >= 2 and dy >= 2)
    }


def check_archive(checks: dict[str, bool], label: str, archive: Path) -> None:
    """Run the checks one archive must pass, whether or not its source had overviews."""
    header, tiles = read_archive(archive)
    checks[f"{label}: header is clustered PNG z{MIN_ZOOM}..z{MOSAIC_ZOOM}"] = (
        header["clustered"]
        and header["tile_type"] == TileType.PNG
        and header["min_zoom"] == MIN_ZOOM
        and header["max_zoom"] == MOSAIC_ZOOM
    )
    at_max = {zxy for zxy in tiles if zxy[0] == MOSAIC_ZOOM}
    checks[f"{label}: the transparent block is dropped and nothing else"] = (
        at_max == expected_z13_tiles()
    )
    # The source is 0/255; a tile read from an overview would be flat 128 grey.
    checks[f"{label}: every max-zoom tile is the checkerboard, not an overview"] = all(
        set(np.unique(tiles[zxy][..., 0]).tolist()) == {0, 255} for zxy in at_max
    )
    checks[f"{label}: max-zoom tiles are fully opaque"] = all(
        (tiles[zxy][..., 3] == 255).all() for zxy in at_max
    )
    # The raster is four z13 tiles across, so at z11 it is exactly one tile:
    # the checkerboard averaged to grey, a quarter of it transparent.
    z11 = tiles.get((11, ORIGIN_X // 4, ORIGIN_Y // 4))
    checks[f"{label}: the z11 tile is the whole raster, averaged"] = z11 is not None and bool(
        np.isin(z11[..., 0][z11[..., 3] == 255], (127, 128)).all()
        and (z11[..., 3] == 255).mean() == 0.75
        and (z11[: TILE_PX // 2, : TILE_PX // 2, 3] == 255).all()
        and (z11[TILE_PX // 2 :, TILE_PX // 2 :, 3] == 0).all()
    )
    left, _, _, top = tile_bounds(MOSAIC_ZOOM, ORIGIN_X, ORIGIN_Y)
    _, bottom, right, _ = tile_bounds(
        MOSAIC_ZOOM, ORIGIN_X + TILES_ACROSS - 1, ORIGIN_Y + TILES_ACROSS - 1
    )
    west, south, east, north = transform_bounds("EPSG:3857", "EPSG:4326", left, bottom, right, top)
    checks[f"{label}: header bounds are the raster's, to 1e-7 degrees"] = all(
        abs(header[key] / 1e7 - value) < 2e-7
        for key, value in (
            ("min_lon_e7", west),
            ("min_lat_e7", south),
            ("max_lon_e7", east),
            ("max_lat_e7", north),
        )
    )


def check_enumeration(checks: dict[str, bool]) -> None:
    """Tile enumeration against the scope's committed grid arithmetic."""
    grid = get_scope("ch").display_grid
    at_13 = tiles_covering(grid.bounds, [MOSAIC_ZOOM])
    checks["ch at z13 is one tile per 256 px block"] = len(at_13) == (grid.width // TILE_PX) * (
        grid.height // TILE_PX
    )
    checks["ids come out ascending"] = at_13 == sorted(at_13)
    expected = {
        (MOSAIC_ZOOM, 4250 + dx, 2870 + dy)
        for dx in range(grid.width // TILE_PX)
        for dy in range(grid.height // TILE_PX)
    }
    checks["ch at z13 is the block from (4250, 2870)"] = {
        tileid_to_zxy(tile_id) for tile_id in at_13
    } == expected
    left, bottom, _, top = tile_bounds(MOSAIC_ZOOM, 4250, 2870)
    checks["tile_bounds of the origin tile touches the grid corner"] = (
        abs(left - grid.bounds[0]) < 1e-6 and abs(top - grid.bounds[3]) < 1e-6 and bottom < top
    )
    # Per zoom, the tile counts `mercantile.tiles` gives for the same box: one
    # tile at z5 and z6, then 4, 12, 24, 77, 286, 1050, 4200.
    per_zoom = [len(tiles_covering(grid.bounds, [zoom])) for zoom in range(5, MOSAIC_ZOOM + 1)]
    checks["ch at z5..z13 enumerates what mercantile does"] = per_zoom == [
        1,
        1,
        4,
        12,
        24,
        77,
        286,
        1050,
        4200,
    ]
    checks["a zero-width extent still yields the tile it sits in"] = (
        len(tiles_covering((left, top, left, top), [MOSAIC_ZOOM])) == 1
    )


def check_all_transparent_refused(work: Path) -> bool:
    """Check that a source with no visible pixels is refused: zero tiles means a broken build."""
    blank = work / "blank.tif"
    write_source(blank, overviews=False)
    with rasterio.open(blank, "r+") as dst:
        dst.write(np.zeros((TILES_ACROSS * TILE_PX,) * 2, dtype="uint8"), 4)
    try:
        write_archive(
            blank,
            work / "blank.pmtiles",
            min_zoom=MIN_ZOOM,
            max_zoom=MOSAIC_ZOOM,
            spec=TileSpec(max_zoom=MOSAIC_ZOOM, image_format="PNG"),
            name="blank",
        )
    except ValueError:
        return True
    return False


def main() -> int:
    """Run every check; print the failures."""
    checks: dict[str, bool] = {}
    check_enumeration(checks)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        plain = work / "plain.tif"
        write_source(plain, overviews=False)
        pyramid = work / "pyramid.tif"
        shutil.copy(plain, pyramid)
        with rasterio.open(pyramid, "r+") as dst:
            dst.build_overviews([2, 4], Resampling.average)

        for label, source in (("plain", plain), ("with overviews", pyramid)):
            archive = work / f"{source.stem}.pmtiles"
            written = write_archive(
                source,
                archive,
                min_zoom=MIN_ZOOM,
                max_zoom=MOSAIC_ZOOM,
                spec=TileSpec(max_zoom=MOSAIC_ZOOM, image_format="PNG"),
                name=label,
                attribution="test",
            )
            # 12 at z13, 3 at z12 (the transparent block is one z12 tile), then
            # one each at z11, z10 and z9.
            checks[f"{label}: 18 tiles written"] = written == 18
            check_archive(checks, label, archive)

        checks["a fully transparent source is refused"] = check_all_transparent_refused(work)

    # Each worker sets a bounded GDAL cache. GDAL's default of 5 % of RAM per
    # process adds up quickly across a pool of workers.
    with spawn_pool(2) as pool:
        caches = set(pool.map(worker_gdal_cache, range(4)))
    checks["every tiler worker bounds its GDAL cache"] = caches == {str(WORKER_GDAL_CACHE_MB)}

    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        print(f"FAILED: {len(failed)} of {len(checks)} check(s)")
        return 1
    print(f"OK: {len(checks)} tiler check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
