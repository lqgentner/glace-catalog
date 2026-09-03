"""Canonical grids and identifiers of the published GLACE stores.

A store is built over one *scope* — Switzerland as a rehearsal, then the Alps —
and everything that distinguishes the two lives here: the collection ids, the
composite perimeter, and above all the target grid its annual mosaics are
written on.

The extents are **hard-coded constants, not derived at build time**. An extent
recomputed from whatever tiles happen to exist is not canonical: it would move
whenever an acquisition gap moves, and two years of the same dataset would then
sit on two grids. Pinning them means every year has identical ``proj:shape`` and
``proj:transform``, so stacking and differencing are a read with no alignment
step. Changing one must be a visible commit; ``cache/stac-store-refactor/
derive-extents.py`` is what recomputes them.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

from rasterio.transform import Affine

if TYPE_CHECKING:
    from collections.abc import Mapping

WEB_MERCATOR = "EPSG:3857"

MERCATOR_ORIGIN = -20037508.342789244
"""Left and top edge of the WebMercatorQuad tile matrix, in metres."""

MERCATOR_SPAN_M = 2 * 20037508.342789244
"""Full width of the WebMercatorQuad tile matrix, in metres."""

TILE_PX = 256
"""Side of one WebMercatorQuad tile, in pixels."""

MOSAIC_ZOOM = 13
"""Zoom level the annual mosaics are written on.

Aligning the mosaic to the pyramid is what removes the second resampling: at max
zoom a tiler's warp is the identity, and the COG's own internal overview levels
land exactly on z12, z11, …, so the file *is* a tile pyramid.
"""


def zoom_resolution(zoom: int) -> float:
    """Ground resolution of one WebMercatorQuad pixel at ``zoom``, in metres."""
    return MERCATOR_SPAN_M / (TILE_PX * 2**zoom)


MOSAIC_RESOLUTION = zoom_resolution(MOSAIC_ZOOM)
"""19.109257071294063 m, which is 13.2 m on the ground at 46.5 degrees north."""


@dataclass(frozen=True)
class Grid:
    """A fixed target grid: a CRS, a square pixel size and an extent.

    Parameters
    ----------
    crs
        CRS the grid is defined in.
    resolution
        Pixel size, in units of ``crs``.
    bounds
        Extent as ``(left, bottom, right, top)``.
    """

    crs: str
    resolution: float
    bounds: tuple[float, float, float, float]

    @property
    def width(self) -> int:
        """Width of the grid, in pixels."""
        return round((self.bounds[2] - self.bounds[0]) / self.resolution)

    @property
    def height(self) -> int:
        """Height of the grid, in pixels."""
        return round((self.bounds[3] - self.bounds[1]) / self.resolution)

    @property
    def shape(self) -> tuple[int, int]:
        """Grid shape as ``(height, width)``, the order ``proj:shape`` uses."""
        return self.height, self.width

    @property
    def transform(self) -> Affine:
        """Affine transform of the grid's upper-left corner."""
        return Affine(
            self.resolution,
            0.0,
            self.bounds[0],
            0.0,
            -self.resolution,
            self.bounds[3],
        )


@dataclass(frozen=True)
class Scope:
    """One published store: what it covers, where it lives and on what grid.

    Parameters
    ----------
    key
        Short identifier, ``"ch"`` or ``"alps"``.
    title
        Human-readable region name, used in collection titles.
    root
        Default store root, as an ``s3://`` URI.
    grid
        Canonical grid the annual mosaics are written on.
    perimeter_tiles
        Number of MGRS tiles the scope's perimeter claims. The extent is derived
        from these rather than from the tiles that exist, so this is what a
        completeness check compares against.
    coverage
        Fraction of the grid those tiles occupy. The extent is a bounding box
        around a ragged perimeter, so most of it is empty; since nodata
        compresses to almost nothing under LERC, this is what prices a mosaic.
    """

    key: str
    title: str
    root: str
    grid: Grid
    perimeter_tiles: int
    coverage: float

    @property
    def tiles_collection_id(self) -> str:
        """Identifier of the per-tile collection, e.g. ``glace-ch-tiles``."""
        return f"glace-{self.key}-tiles"

    @property
    def mosaics_collection_id(self) -> str:
        """Identifier of the annual-mosaic collection, e.g. ``glace-ch-mosaics``."""
        return f"glace-{self.key}-mosaics"

    def mosaic_item_id(self, year: int) -> str:
        """Identifier of one year's mosaic item, e.g. ``ch-mosaic-2024``."""
        return f"{self.key}-mosaic-{year}"


SCOPES: Mapping[str, Scope] = {
    "ch": Scope(
        key="ch",
        title="Switzerland",
        root="s3://s1-bursts/data/glace-ch",
        grid=Grid(
            crs=WEB_MERCATOR,
            resolution=MOSAIC_RESOLUTION,
            bounds=(
                753363.3507786989,
                5752956.496855505,
                1164288.8148398064,
                5997554.98736807,
            ),
        ),
        # The Swiss run composites glacier tiles only (`glacier_buffer_m: 250`
        # in config/lrw-ch.yml), so the perimeter is the buffered SGI2023, not
        # the national boundary — which would quadruple the extent with nodata.
        # Confirmed by the data: the completed 2022-2024 runs hold exactly these
        # 143 tiles, every one carrying data and none falling outside.
        perimeter_tiles=143,
        coverage=0.307,
    ),
    "alps": Scope(
        key="alps",
        title="European Alps",
        root="s3://glace-alps",
        grid=Grid(
            crs=WEB_MERCATOR,
            resolution=MOSAIC_RESOLUTION,
            bounds=(
                528332.7395071387,
                5371382.851655904,
                1849164.5882749856,
                6173665.900537115,
            ),
        ),
        # The Alpine Convention perimeter, via MGRSGrid.
        perimeter_tiles=2209,
        coverage=0.454,
    ),
}
"""Every scope a store can be built over, keyed by :attr:`Scope.key`."""


def get_scope(key: str) -> Scope:
    """Look up a scope by key.

    Raises
    ------
    KeyError
        If ``key`` names no scope.
    """
    try:
        return SCOPES[key]
    except KeyError:
        msg = f"Unknown scope {key!r}; known scopes are {sorted(SCOPES)}."
        raise KeyError(msg) from None


def snap_to_tiles(
    bounds: tuple[float, float, float, float],
    zoom: int = MOSAIC_ZOOM,
) -> tuple[float, float, float, float]:
    """Grow ``bounds`` outward to whole 256 px tiles of the WebMercatorQuad grid.

    Snapping to a tile rather than to a pixel means the mosaic begins and ends on
    a tile boundary, so a tiler never has to pad a partial edge tile.
    """
    step = zoom_resolution(zoom) * TILE_PX
    left, bottom, right, top = bounds
    return (
        MERCATOR_ORIGIN + math.floor((left - MERCATOR_ORIGIN) / step) * step,
        MERCATOR_ORIGIN + math.floor((bottom - MERCATOR_ORIGIN) / step) * step,
        MERCATOR_ORIGIN + math.ceil((right - MERCATOR_ORIGIN) / step) * step,
        MERCATOR_ORIGIN + math.ceil((top - MERCATOR_ORIGIN) / step) * step,
    )
