#!/usr/bin/env python3
"""Checks value-encoded tiles: the codes, their MapLibre decoding, the tiles.

For a client to read values back out of an archive, three things must hold:

- encoding and decoding a value round-trips to within half a quantisation step;
- the factors the style declares decode a grey pixel to its value, and survive
  MapLibre's own packing of color-relief stops (``packDEMData`` in GL JS 6.9,
  ported below), which treats them as positional digits;
- the tiler writes tiles with a binary alpha, code 0 for nodata, and the
  source's codes at max zoom.

Writes only into a temporary directory and needs no network.

Run: uv run tests/test_encoding.py
"""

from __future__ import annotations

import io
import itertools
from pathlib import Path
import sys
import tempfile

import numpy as np
from PIL import Image
from pmtiles.reader import MmapSource, all_tiles
import rasterio

from glace_catalog.encoding import ValueEncoding
from glace_catalog.metadata import load_vocabulary
from glace_catalog.scopes import MOSAIC_ZOOM, TILE_PX, zoom_resolution
from glace_catalog.stac.naming import ASSET_KEYS
from glace_catalog.tiler import TileSpec, tile_bounds, write_archive

ORIGIN_X, ORIGIN_Y = 4252, 2872


def unpack(factors: dict[str, float], r: float, g: float, b: float) -> float:
    """MapLibre's ``DEMData.unpack`` for a custom encoding."""
    return (
        r * factors["redFactor"]
        + g * factors["greenFactor"]
        + b * factors["blueFactor"]
        - factors["baseShift"]
    )


def pack(factors: dict[str, float], value: float) -> tuple[int, int, int]:
    """MapLibre's ``packDEMData``, which encodes the colour-relief stops."""
    weights = (factors["redFactor"], factors["greenFactor"], factors["blueFactor"])
    smallest = min(weights)
    scaled = round((value + factors["baseShift"]) / smallest)
    r, g, b = (int(scaled * smallest / weight) % 256 for weight in weights)
    return r, g, b


def check_codes(checks: dict[str, bool]) -> None:
    """Quantisation, MapLibre decoding and stop packing of every transport range."""
    vocabulary = load_vocabulary()
    for key in ASSET_KEYS:
        display = vocabulary.display(key)
        encoding = display.encoding
        values = np.linspace(encoding.lo, encoding.hi, 1001)
        decoded = encoding.decode(encoding.encode(values))
        checks[f"{key}: round trip within half a step"] = bool(
            np.nanmax(np.abs(decoded - values)) <= encoding.scale / 2 + 1e-9
        )
        factors = encoding.maplibre()
        grey = [unpack(factors, n, n, n) for n in (1, 128, 255)]
        checks[f"{key}: a grey pixel decodes like decode()"] = bool(
            np.allclose(grey, encoding.decode(np.array([1, 128, 255])), atol=1e-9)
        )
        ramp = encoding.ramp(["#000000", "#808080", "#ffffff"], display.vmin, display.vmax)
        stops = ramp[3::2]
        checks[f"{key}: ramp stops ascend"] = all(a < b for a, b in itertools.pairwise(stops))
        checks[f"{key}: every stop survives MapLibre's packing"] = all(
            abs(unpack(factors, *pack(factors, stop)) - stop) < encoding.scale / 1000
            for stop in stops
        )
        nodata = unpack(factors, 0, 0, 0)
        checks[f"{key}: code 0 decodes below the transparent stop"] = (
            nodata < stops[0] < unpack(factors, 1, 1, 1)
        )
        band = encoding.stac_band(display.units)
        checks[f"{key}: the STAC band decodes like decode()"] = bool(
            np.isclose(128 * band["raster:scale"] + band["raster:offset"], grey[1])
        )
    checks["values outside the range clip, nan is nodata"] = ValueEncoding(0, 1).encode(
        np.array([-5.0, 5.0, np.nan])
    ).tolist() == [1, 255, 0]


def write_codes(path: Path) -> np.ndarray:
    """Four z13 tiles across of known codes 1..255; the lower-right quarter nodata."""
    side = 4 * TILE_PX
    rows, cols = np.indices((side, side))
    codes = (1 + (rows + 3 * cols) % 255).astype("uint8")
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
            dst.write(codes, band)
        dst.write(alpha, 4)
    return codes


def read_archive(
    source: Path, archive: Path, image_format: str
) -> dict[tuple[int, int, int], np.ndarray]:
    """Tile ``source`` in ``image_format`` and decode every tile as RGBA."""
    write_archive(
        source,
        archive,
        min_zoom=11,
        max_zoom=MOSAIC_ZOOM,
        spec=TileSpec(image_format=image_format),
        name="codes",
    )
    with archive.open("rb") as handle:
        return {
            zxy: np.asarray(Image.open(io.BytesIO(payload)).convert("RGBA"))
            for zxy, payload in all_tiles(MmapSource(handle))
        }


def check_exact_tiles(checks: dict[str, bool], work: Path) -> None:
    """Tile a known code raster losslessly and read the codes back."""
    codes = write_codes(work / "codes.tif")
    tiles = read_archive(work / "codes.tif", work / "codes.pmtiles", "PNG")
    exact = True
    for dx in range(4):
        for dy in range(4):
            tile = tiles.get((MOSAIC_ZOOM, ORIGIN_X + dx, ORIGIN_Y + dy))
            if dx >= 2 and dy >= 2:
                exact &= tile is None
                continue
            if tile is None:
                exact = False
                continue
            block = codes[dy * TILE_PX : (dy + 1) * TILE_PX, dx * TILE_PX : (dx + 1) * TILE_PX]
            exact &= all((tile[..., band] == block).all() for band in range(3))
    checks["max-zoom codes are the source's, grey"] = bool(exact)
    checks["alpha is binary at every zoom"] = all(
        set(np.unique(tile[..., 3]).tolist()) <= {0, 255} for tile in tiles.values()
    )
    z11 = tiles[(11, ORIGIN_X // 4, ORIGIN_Y // 4)]
    quarter = z11[TILE_PX // 2 :, TILE_PX // 2 :]
    checks["nodata is code 0 and transparent at z11, data is not"] = bool(
        (quarter == 0).all() and (z11[: TILE_PX // 2, :, :3] > 0).all()
    )


def check_lossy_tiles(checks: dict[str, bool], work: Path) -> None:
    """Check that the published WebP tiles keep a binary alpha over their codes."""
    codes = write_codes(work / "codes-lossy.tif")
    tiles = read_archive(work / "codes-lossy.tif", work / "codes-lossy.pmtiles", "WEBP")
    checks["lossy: alpha is binary at every zoom"] = all(
        set(np.unique(tile[..., 3]).tolist()) <= {0, 255} for tile in tiles.values()
    )
    z11 = tiles[(11, ORIGIN_X // 4, ORIGIN_Y // 4)]
    checks["lossy: the nodata quarter is transparent at z11"] = bool(
        (z11[TILE_PX // 2 :, TILE_PX // 2 :, 3] == 0).all()
    )
    tile = tiles[(MOSAIC_ZOOM, ORIGIN_X, ORIGIN_Y)]
    checks["lossy: max-zoom tiles are opaque where the source is"] = bool(
        (tile[..., 3] == 255).all()
    )
    # The synthetic pattern is a worst case (a sawtooth at the pixel scale), so
    # this bound catches gross corruption only; real scenes compress far better.
    error = np.abs(tile[..., 0].astype(int) - codes[:TILE_PX, :TILE_PX].astype(int))
    checks["lossy: the codes survive in the median"] = float(np.median(error)) < 16


def main() -> int:
    """Run every check; print the failures."""
    checks: dict[str, bool] = {}
    check_codes(checks)
    with tempfile.TemporaryDirectory() as tmp:
        check_exact_tiles(checks, Path(tmp))
        check_lossy_tiles(checks, Path(tmp))
    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        print(f"FAILED: {len(failed)} of {len(checks)} check(s)")
        return 1
    print(f"OK: {len(checks)} encoding check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
