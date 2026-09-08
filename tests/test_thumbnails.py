#!/usr/bin/env python3
"""Gate on the thumbnails cut out of the published web-map archives.

The collection thumbnails are the one image a browser shows before anything is
downloaded, and they are read back out of a PMTiles archive rather than
re-rendered (R-9), so what can go wrong is arithmetic: the wrong zoom, the frame
off by a tile, north and south swapped, an aspect that is right in degrees and
wrong on the map. All four fail silently — the image is still an image.

So this builds a synthetic archive whose every pixel is known: two zoom levels
over one 2x2 tile block, each quadrant a different solid colour. Rendering it
back has to put those colours where the map says they are, at the zoom the
output width calls for, with the framed margins transparent.

The framing is checked in **projected** space, which is the property that
matters: at 46 degrees north a 3:2 box in degrees is not a 3:2 picture.

Writes only into a temporary directory, reads no data and needs no network.

Run: python3 tests/test_thumbnails.py
"""

from __future__ import annotations

import io
import math
from pathlib import Path
import sys
import tempfile
from typing import cast

from PIL import Image
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from glace_catalog.scopes import TILE_PX, get_scope
from glace_catalog.thumbnails import (
    THUMBNAIL_TILE,
    Bbox,
    choose_zoom,
    frame_3_2,
    mosaics_bbox,
    render_from_pmtiles,
    tile_bbox,
    to_mercator,
)

ZOOM = 12
"""Finest zoom of the synthetic archive."""

TILE_X, TILE_Y = 2138, 1442
"""Upper-left tile of the 2x2 block the archive covers: the Bernese Alps at z12.

Both even, so the block is exactly one tile at ``ZOOM - 1`` and the coarse level
covers the same ground as the fine one.
"""

QUADRANTS: tuple[tuple[int, int, int], ...] = (
    (220, 30, 40),  # north-west
    (40, 180, 60),  # north-east
    (30, 70, 220),  # south-west
    (240, 210, 20),  # south-east
)
"""Solid colour of each quadrant of the block, clockwise from the north-west."""

OPAQUE = 255
TOLERANCE = 3
"""Slack per channel, in 8-bit codes; the resampler touches even a solid area's edges."""


def slippy_bounds(zoom: int, x: int, y: int) -> Bbox:
    """WGS84 bounds of one WebMercatorQuad tile, from the textbook formula.

    Deliberately not :func:`~glace_catalog.thumbnails.pixel_bounds` inverted:
    this is the independent statement of where a tile is, so an error in the
    module's own projection cannot agree with itself.
    """
    count = 2**zoom
    west = x / count * 360.0 - 180.0
    east = (x + 1) / count * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / count))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / count))))
    return (west, south, east, north)


def block_bbox() -> Bbox:
    """WGS84 bounds of the whole 2x2 block the synthetic archive covers."""
    west, _, _, north = slippy_bounds(ZOOM, TILE_X, TILE_Y)
    _, south, east, _ = slippy_bounds(ZOOM, TILE_X + 1, TILE_Y + 1)
    return (west, south, east, north)


def quadrant_tile(colors: tuple[tuple[int, int, int], ...], size: int) -> bytes:
    """One lossless WEBP tile painted with the four quadrant colours."""
    image = Image.new("RGBA", (size, size))
    half = size // 2
    for index, color in enumerate(colors):
        box = (
            (index % 2) * half,
            (index // 2) * half,
            (index % 2 + 1) * half,
            (index // 2 + 1) * half,
        )
        image.paste((*color, OPAQUE), box)
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", lossless=True)
    return buffer.getvalue()


def solid_tile(color: tuple[int, int, int]) -> bytes:
    """One lossless WEBP tile of a single colour."""
    return quadrant_tile((color,) * 4, TILE_PX)


def write_archive(path: Path) -> None:
    """Write the synthetic archive: the block at ``ZOOM`` and at ``ZOOM - 1``.

    At the fine level each quadrant of the block is its own tile; at the coarse
    level the whole block is one tile carrying the same four quadrants, so the
    same picture is available at both zooms and only the zoom choice decides
    which one a render reads.
    """
    west, south, east, north = block_bbox()
    tiles = {
        zxy_to_tileid(ZOOM - 1, TILE_X // 2, TILE_Y // 2): quadrant_tile(QUADRANTS, TILE_PX),
    }
    for index, color in enumerate(QUADRANTS):
        tiles[zxy_to_tileid(ZOOM, TILE_X + index % 2, TILE_Y + index // 2)] = solid_tile(color)

    with path.open("wb") as handle:
        writer = Writer(handle)
        for tile_id in sorted(tiles):
            writer.write_tile(tile_id, tiles[tile_id])
        writer.finalize(
            {
                "version": 3,
                "tile_type": TileType.WEBP,
                "tile_compression": Compression.NONE,
                "min_lon_e7": round(west * 1e7),
                "min_lat_e7": round(south * 1e7),
                "max_lon_e7": round(east * 1e7),
                "max_lat_e7": round(north * 1e7),
                "center_zoom": ZOOM,
                "center_lon_e7": round((west + east) / 2 * 1e7),
                "center_lat_e7": round((south + north) / 2 * 1e7),
            },
            {"name": "synthetic"},
        )


def sample(image: Image.Image, xy: tuple[int, int]) -> tuple[int, int, int, int]:
    """One RGBA pixel of an image, as four channels."""
    red, green, blue, alpha = cast("tuple[int, int, int, int]", image.getpixel(xy))
    return red, green, blue, alpha


def close_to(image: Image.Image, xy: tuple[int, int], color: tuple[int, int, int]) -> bool:
    """Whether a sampled pixel is the expected opaque colour."""
    pixel = sample(image, xy)
    return pixel[3] == OPAQUE and all(
        abs(pixel[band] - color[band]) <= TOLERANCE for band in range(3)
    )


def projected_aspect(bbox: Bbox) -> float:
    """Width-to-height ratio of a WGS84 box once projected to WebMercator."""
    left, bottom = to_mercator(bbox[0], bbox[1])
    right, top = to_mercator(bbox[2], bbox[3])
    return (right - left) / (top - bottom)


def contains(outer: Bbox, inner: Bbox) -> bool:
    """Whether ``outer`` covers ``inner`` entirely."""
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def refuses_empty_coverage(archive: Path) -> bool:
    """Whether a box outside the archive's tiles is refused rather than drawn blank."""
    west, south, east, north = block_bbox()
    span = east - west
    elsewhere: Bbox = (west + 4 * span, south, east + 4 * span, north)
    try:
        render_from_pmtiles(archive, elsewhere, width=2 * TILE_PX)
    except ValueError:
        return True
    return False


def _rendering_checks(archive: Path) -> dict[str, bool]:
    """Render the synthetic archive at three widths and check what came back."""
    bbox = block_bbox()
    native = render_from_pmtiles(archive, bbox, width=2 * TILE_PX)
    coarse = render_from_pmtiles(archive, bbox, width=TILE_PX)
    framed = render_from_pmtiles(archive, frame_3_2(bbox), width=1200)

    # The block is square in projected space, so at its own zoom it renders one
    # output pixel per tile pixel and the quadrants land on exact quarters.
    quarter, three_quarters = TILE_PX // 2, 3 * TILE_PX // 2
    centres = ((quarter, quarter), (three_quarters, quarter))
    centres += ((quarter, three_quarters), (three_quarters, three_quarters))

    # 1200 px of 3:2 frame around an 800 px square block: 200 px of transparent
    # margin each side, and the quadrant centres at 400 and 800.
    framed_centres = ((400, 200), (800, 200), (400, 600), (800, 600))

    return {
        "the archive renders at its own zoom without scaling": native.size
        == (2 * TILE_PX, 2 * TILE_PX),
        "a half-width render reads the coarse level, which already fills it": choose_zoom(
            bbox,
            width=TILE_PX,
            min_zoom=ZOOM - 1,
            max_zoom=ZOOM,
        )
        == ZOOM - 1,
        "a width the coarse level cannot fill reads the fine one": choose_zoom(
            bbox,
            width=TILE_PX + 44,
            min_zoom=ZOOM - 1,
            max_zoom=ZOOM,
        )
        == ZOOM,
        "a width no level reaches reads the finest there is": choose_zoom(
            bbox,
            width=4 * TILE_PX,
            min_zoom=ZOOM - 1,
            max_zoom=ZOOM,
        )
        == ZOOM,
        "every quadrant renders its own colour": all(
            close_to(native, centre, color)
            for centre, color in zip(centres, QUADRANTS, strict=True)
        ),
        "the coarse level draws the same quadrants": all(
            close_to(coarse, (x // 2, y // 2), color)
            for (x, y), color in zip(centres, QUADRANTS, strict=True)
        ),
        "a framed render is 1200x800": framed.size == (1200, 800),
        "framing moves no data": all(
            close_to(framed, centre, color)
            for centre, color in zip(framed_centres, QUADRANTS, strict=True)
        ),
        "a box the archive does not cover is refused": refuses_empty_coverage(archive),
        "the margins a frame adds are transparent": all(
            sample(framed, xy)[3] == 0 for xy in ((20, 400), (1180, 400))
        ),
        # A margin sampled only at its middle would tolerate the frame being a
        # hundred pixels out; sampling either side of the boundary pins it.
        "the frame's edge is where the arithmetic puts it": all(
            sample(framed, (x, 300))[3] == 0 for x in (195, 1005)
        )
        and all(sample(framed, (x, 300))[3] == OPAQUE for x in (205, 995)),
    }


def main() -> int:
    """Run every thumbnail check and report."""
    tall: Bbox = (8.0, 46.0, 8.1, 47.0)
    wide: Bbox = (7.0, 46.4, 9.0, 46.5)
    aletsch = tile_bbox(THUMBNAIL_TILE)

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "coh12_rgb.pmtiles"
        write_archive(archive)
        checks = _rendering_checks(archive)

    checks["a tall box is framed to 3:2"] = math.isclose(
        projected_aspect(frame_3_2(tall)), 1.5, rel_tol=1e-9
    ) and contains(frame_3_2(tall), tall)
    checks["a wide box is framed to 3:2"] = math.isclose(
        projected_aspect(frame_3_2(wide)), 1.5, rel_tol=1e-9
    ) and contains(frame_3_2(wide), wide)
    checks["framing a 3:2 box changes nothing"] = math.isclose(
        projected_aspect(frame_3_2(frame_3_2(wide))),
        1.5,
        rel_tol=1e-9,
    )
    # The tiles thumbnail must picture a tile the store actually publishes.
    checks[f"{THUMBNAIL_TILE} lies inside the Swiss display extent"] = contains(
        mosaics_bbox(get_scope("ch")),
        aletsch,
    )
    checks[f"{THUMBNAIL_TILE} is the Aletsch tile"] = (
        7.9 < aletsch[0] < 8.1 and 46.4 < aletsch[1] < 46.5
    )

    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        print(f"FAILED: {len(failed)} of {len(checks)} check(s)")
        return 1
    print(f"OK: {len(checks)} thumbnail check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
