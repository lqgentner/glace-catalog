#!/usr/bin/env python3
"""Checks the web-map build: encode, warp once, tile.

``webmap.build_layers`` runs the whole display path: a lookup-table VRT that
turns values into 8-bit codes, one warp of it into a local RGBA raster, the
tiler over that raster, and the archive's manifest entry. In production its
inputs live on object storage, so this gate is where CI runs it. Its failures
are silent: an intermediate raster with overviews, an alpha band the tiler
misses, a value encoded at the wrong end of the range, nodata that comes out
opaque.

The test writes a synthetic float32 mosaic on the display grid (a linear ramp
across the transport range, with one NaN tile) and builds two layers from it at
once, as ``build-year`` does with a local source. It reads the archive back,
compares the max-zoom codes with the ramp encoded by hand, and inspects the
intermediate raster along the way.

Writes only into a temporary directory, reads no data and needs no network.

Run: uv run tests/test_webmap.py
"""

from __future__ import annotations

import io
from pathlib import Path
import sys
import tempfile

import numpy as np
from PIL import Image
from pmtiles.reader import MmapSource, Reader
import rasterio
from rasterio.enums import ColorInterp

from glace_catalog.encoding import ValueEncoding
from glace_catalog.mosaics import YearVrts
from glace_catalog.scopes import MOSAIC_ZOOM, TILE_PX, zoom_resolution
from glace_catalog.stac.store import multihash_sha256_file
from glace_catalog.styles import LayerStyle
from glace_catalog.tiler import tile_bounds
from glace_catalog.webmap import build_layers, rgba_raster

ORIGIN_X, ORIGIN_Y = 4276, 2884
"""Upper-left z13 tile of the synthetic mosaic, just south of Interlaken."""

TILES_ACROSS = 2
VMIN, VMAX = 0.0, 1.0
ENCODING = ValueEncoding(VMIN, VMAX)
MAX_CODE_ERROR = 3
"""Largest code change lossy WebP may cause on a smooth ramp at the published quality."""


def write_mosaic(path: Path) -> None:
    """Write a float32 ramp on the z13 grid; its lower-right tile is NaN."""
    side = TILES_ACROSS * TILE_PX
    ramp = np.linspace(VMIN, VMAX, side, dtype="float32")
    values = np.tile(ramp, (side, 1))
    values[TILE_PX:, TILE_PX:] = np.nan
    left, _, _, top = tile_bounds(MOSAIC_ZOOM, ORIGIN_X, ORIGIN_Y)
    resolution = zoom_resolution(MOSAIC_ZOOM)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        dtype="float32",
        count=1,
        width=side,
        height=side,
        crs="EPSG:3857",
        transform=rasterio.transform.from_origin(left, top, resolution, resolution),
        nodata=float("nan"),
        tiled=True,
        blockxsize=TILE_PX,
        blockysize=TILE_PX,
    ) as dst:
        dst.write(values, 1)


def read_tile(archive: Path, x: int, y: int) -> np.ndarray | None:
    """Decode one max-zoom tile of an archive, or ``None`` if it is absent."""
    with archive.open("rb") as handle:
        payload = Reader(MmapSource(handle)).get(MOSAIC_ZOOM, x, y)
    if payload is None:
        return None
    return np.asarray(Image.open(io.BytesIO(payload)).convert("RGBA"))


def check_intermediate(checks: dict[str, bool], mosaic: Path, work: Path) -> None:
    """Check the warped-once raster: RGBA, one tile per block, alpha declared, no overviews."""
    out = rgba_raster(mosaic, work / "rgba.tif", luts=ENCODING.luts())
    with rasterio.open(out) as src:
        checks["intermediate is four uint8 bands"] = src.count == 4 and src.dtypes[0] == "uint8"
        checks["intermediate blocks are one tile"] = src.block_shapes[0] == (TILE_PX, TILE_PX)
        checks["intermediate declares its alpha"] = src.colorinterp[3] == ColorInterp.alpha
        checks["intermediate carries no overviews"] = src.overviews(1) == []
        checks["intermediate keeps the source grid"] = src.width == TILES_ACROSS * TILE_PX and (
            src.transform.a == zoom_resolution(MOSAIC_ZOOM)
        )
        alpha = src.read(4)
        checks["nodata is transparent, data opaque"] = bool(
            (alpha[TILE_PX:, TILE_PX:] == 0).all() and (alpha[:TILE_PX, :] == 255).all()
        )
        checks["the LUT VRT is written beside the raster"] = out.with_suffix(".vrt").exists()
        side = TILES_ACROSS * TILE_PX
        expected = ENCODING.encode(np.linspace(VMIN, VMAX, side))
        checks["the intermediate holds the codes, grey"] = all(
            np.abs(src.read(band)[0].astype(int) - expected.astype(int)).max() <= 1
            for band in (1, 2, 3)
        )


def check_build(checks: dict[str, bool], mosaic: Path, work: Path) -> None:
    """Check the build over a two-layer chain, warped concurrently into one shared pool."""
    style = LayerStyle(cmap="cmc.batlow", vmin=0.1, vmax=0.9, encoding=ENCODING)
    chain = YearVrts(
        linear={"coh12_vv": mosaic, "coh12_vh": mosaic},
        decibel={},
        sources_at=mosaic,
    )
    built = build_layers(
        chain,
        work / "pmtiles",
        year=2024,
        start_date="2024-06-01",
        end_date="2024-09-30",
        styles={"coh12_vv": style, "coh12_vh": style},
        min_zoom=11,
        workers=2,
        concurrent_layers=2,
        scratch=work / "scratch",
    )
    checks["one archive per layer, in layer order"] = [info.id for info in built] == [
        "coh12_vh_2024",
        "coh12_vv_2024",
    ]
    checks["the scratch rasters are gone"] = not any((work / "scratch").iterdir())
    checks["nothing but archives is staged"] = sorted(
        path.name for path in (work / "pmtiles").iterdir()
    ) == ["coh12_vh_viz.pmtiles", "coh12_vv_viz.pmtiles"]

    info = built[1]
    archive = work / "pmtiles" / info.url
    checks["the manifest entry names the archive and its encoding"] = (
        info.url == "coh12_vv_viz.pmtiles"
        and info.size_bytes == archive.stat().st_size
        and (info.scale, info.offset) == (ENCODING.scale, ENCODING.offset)
        and info.min_zoom == 11
        and info.max_zoom == MOSAIC_ZOOM
    )
    checks["the manifest entry carries the archive's digest"] = info.checksum == (
        multihash_sha256_file(archive)
    )
    west, south, east, north = info.bounds
    checks["the manifest bounds are the mosaic's, near Interlaken"] = (
        7.9 < west < east < 8.0 and 46.8 < south < north < 47.0
    )

    # The upper-left tile holds the first half of the ramp: its pixel at column
    # c is the code of value c / (side - 1), grey, and it is fully opaque.
    tile = read_tile(archive, ORIGIN_X, ORIGIN_Y)
    side = TILES_ACROSS * TILE_PX
    expected = ENCODING.encode(np.arange(TILE_PX) / (side - 1))
    checks["max-zoom pixels are the encoded ramp"] = tile is not None and bool(
        all(
            np.abs(tile[0, :, band].astype(int) - expected.astype(int)).max() <= MAX_CODE_ERROR
            for band in range(3)
        )
        and (tile[..., 3] == 255).all()
    )
    with archive.open("rb") as handle:
        header_metadata = Reader(MmapSource(handle)).metadata()
    checks["the archive metadata declares its decoding"] = (
        header_metadata.get("encoding") == "custom"
        and header_metadata.get("baseShift") == ENCODING.maplibre()["baseShift"]
    )
    checks["the NaN tile is not in the archive"] = (
        read_tile(archive, ORIGIN_X + 1, ORIGIN_Y + 1) is None
    )
    sibling = read_tile(work / "pmtiles" / "coh12_vh_viz.pmtiles", ORIGIN_X, ORIGIN_Y)
    checks["the layer warped alongside renders the same pixels"] = (
        tile is not None and sibling is not None and bool((tile == sibling).all())
    )


def main() -> int:
    """Run every check; print the failures."""
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        mosaic = work / "coh12_vv_webm13.tif"
        write_mosaic(mosaic)
        check_intermediate(checks, mosaic, work)
        check_build(checks, mosaic, work)

    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        print(f"FAILED: {len(failed)} of {len(checks)} check(s)")
        return 1
    print(f"OK: {len(checks)} web-map check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
