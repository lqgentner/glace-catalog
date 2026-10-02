#!/usr/bin/env python3
"""Offline checks of COG thumbnails, using real georeferenced basemap fixtures."""

from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import numpy as np
from PIL import Image
import rasterio
from rasterio.transform import from_origin

from glace_catalog.stac.naming import published_name
from glace_catalog.stac.store import StoreLayout
from glace_catalog.styles import resolve_cmap
from glace_catalog.thumbnails import (
    ATTRIBUTION_SIZE,
    DEFAULT_BASEMAP,
    THUMBNAIL_HEIGHT,
    THUMBNAIL_LAYER,
    THUMBNAIL_TILE,
    THUMBNAIL_WIDTH,
    render_from_cog,
    write_collection_thumbnails,
)


def write_raster(path: Path, values: np.ndarray, *, pixel_size: float = 100) -> None:
    """Write a projected fixture with a known upper-left corner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    bands = values[np.newaxis, ...] if values.ndim == 2 else values
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=bands.shape[2],
        height=bands.shape[1],
        count=bands.shape[0],
        dtype=bands.dtype,
        crs="EPSG:32632",
        transform=from_origin(400000, 5200000, pixel_size, pixel_size),
        nodata=np.nan if bands.dtype.kind == "f" else None,
    ) as dst:
        dst.write(bands)


def invalid_checks(source: Path, backdrop: Path) -> dict[str, bool]:
    """Reject out-of-range and nonfinite coherence before adding a basemap."""
    checks: dict[str, bool] = {}
    for invalid in (-1.0, 2.0, np.nan, np.inf):
        write_raster(source, np.full((40, 40), invalid, dtype="float32"))
        try:
            render_from_cog(source, basemap=str(backdrop), width=200)
        except ValueError as error:
            checks[f"rejects empty data {invalid}"] = "no valid" in str(error)
        else:
            checks[f"rejects empty data {invalid}"] = False
    write_raster(source, np.zeros((40, 40), dtype="float32"))
    checks["zero coherence remains valid"] = render_from_cog(
        source, basemap=str(backdrop), attribution=""
    ).size == (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
    return checks


def guard_checks(source: Path, backdrop: Path) -> dict[str, bool]:
    """Refuse a mirrored raster, an unknown provider and an uncredited provider."""
    checks: dict[str, bool] = {}
    write_raster(source, np.full((40, 40), 0.5, dtype="float32"), pixel_size=-100)
    try:
        render_from_cog(source, basemap=str(backdrop), attribution="")
    except ValueError as error:
        checks["rejects a south-up raster"] = "north-up" in str(error)
    else:
        checks["rejects a south-up raster"] = False

    write_raster(source, np.full((40, 40), 0.5, dtype="float32"))
    try:
        render_from_cog(source, basemap="OpenStreetMap.Maponik")
    except ValueError as error:
        checks["names an unknown provider"] = "neither a contextily provider" in str(error)
    else:
        checks["names an unknown provider"] = False

    try:
        render_from_cog(source, basemap=DEFAULT_BASEMAP, attribution="")
    except ValueError as error:
        checks["refuses to drop a provider's credit"] = "cannot be suppressed" in str(error)
    else:
        checks["refuses to drop a provider's credit"] = False
    return checks


def footprint_span(line: np.ndarray, colour: np.ndarray) -> int:
    """How many pixels of one row or column carry the fixture's uniform data colour.

    Within a tolerance, since lossy WebP renders a flat area as nearly one colour.
    """
    return int((np.abs(line - colour).max(axis=1) <= 12).sum())


def store_checks(root: Path, backdrop: Path) -> dict[str, bool]:
    """Write both collection thumbnails from a store and read them back."""
    checks: dict[str, bool] = {}
    layout = StoreLayout(root / "store")
    mosaic = Path(str(layout.mosaic_year(2024))) / published_name(THUMBNAIL_LAYER)
    tile = Path(str(layout.tile(2024, THUMBNAIL_TILE))) / published_name(THUMBNAIL_LAYER)
    write_raster(mosaic, np.full((40, 80), 0.1, dtype="float32"))
    write_raster(tile, np.full((40, 40), 0.6, dtype="float32"))
    write_collection_thumbnails(layout, 2024, basemap=str(backdrop), attribution="")
    with (
        Image.open(str(layout.mosaics_thumbnail)) as mosaics,
        Image.open(str(layout.tiles_thumbnail)) as tiles,
    ):
        checks["both WebP previews are 3:2 at the frame size"] = (
            mosaics.size == tiles.size == (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
            and mosaics.format == tiles.format == "WEBP"
        )
        mid_x, mid_y = THUMBNAIL_WIDTH // 2, THUMBNAIL_HEIGHT // 2
        checks["tile reads its own COG"] = mosaics.getpixel((mid_x, mid_y)) != tiles.getpixel(
            (mid_x, mid_y)
        )
        tile_pixels = np.asarray(tiles.convert("RGB")).astype(int)
        centre = tile_pixels[mid_y, mid_x]
        horizontal = footprint_span(tile_pixels[mid_y], centre)
        vertical = footprint_span(tile_pixels[:, mid_x], centre)
        checks["landscape frame preserves the square tile footprint"] = bool(
            abs(horizontal - vertical) <= 2
        )
    with patch("glace_catalog.thumbnails.cx.add_basemap") as add:
        render_from_cog(tile)
        credit = add.call_args.kwargs["attribution"]
        checks["provider credit uses the copyright sign"] = (
            credit == "\N{COPYRIGHT SIGN} OpenStreetMap contributors"
        )
        checks["contextily keeps the credit and the higher zoom"] = (
            add.call_args.kwargs["zoom_adjust"] == 1
            and add.call_args.kwargs["attribution_size"] == ATTRIBUTION_SIZE
        )
    previous = layout.mosaics_thumbnail.read_bytes()
    tile.unlink()
    try:
        write_collection_thumbnails(layout, 2024, basemap=str(backdrop), attribution="")
    except rasterio.errors.RasterioIOError:
        checks["missing tile leaves thumbnails unchanged"] = (
            layout.mosaics_thumbnail.read_bytes() == previous
        )
    else:
        checks["missing tile leaves thumbnails unchanged"] = False
    return checks


def main() -> int:
    """Check geometry, colour, transparency, attribution and source selection."""
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        backdrop = root / "basemap.tif"
        # Uniform RGB green covers the fixtures and their margins.
        rgb = np.zeros((3, 300, 300), dtype="uint8")
        rgb[1] = 100
        write_raster(backdrop, rgb)
        with rasterio.open(backdrop, "r+") as dst:
            dst.transform = from_origin(390000, 5210000, 100, 100)
        source = root / "coh.tif"
        data = np.full((40, 80), 0.1, dtype="float32")
        data[:, :40] = np.nan
        write_raster(source, data)
        image = render_from_cog(
            source, basemap=str(backdrop), attribution="", width=400, height=200
        )
        pixels = np.asarray(image)
        checks["native 2:1 aspect retained"] = image.size == (400, 200)
        checks["basemap visible through nodata"] = tuple(pixels[100, 100, :3]) == (0, 100, 0)
        checks["basemap visible outside footprint"] = tuple(pixels[100, 2, :3]) == (0, 100, 0)
        expected = np.array(resolve_cmap("cmc.lipari")(0)[:3]) * 255
        checks["coherence rendered linearly with the PMTiles style"] = bool(
            np.max(np.abs(pixels[100, 300, :3] - expected)) < 2
        )
        checks["footprint boundary aligned"] = tuple(pixels[100, 380, :3]) != (
            0,
            100,
            0,
        ) and tuple(pixels[100, 384, :3]) == (0, 100, 0)
        checks.update(invalid_checks(source, backdrop))
        checks.update(guard_checks(source, backdrop))

        checks.update(store_checks(root, backdrop))

    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    print(f"{len(checks) - len(failed)}/{len(checks)} thumbnail checks passed")
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
