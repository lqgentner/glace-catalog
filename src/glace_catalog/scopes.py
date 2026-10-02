"""Grids and identifiers of the published GLACE stores.

A store is built over one *scope*: Switzerland (a rehearsal) or the Alps. This
module defines each scope's collection ids, its tile perimeter and its two
target grids: an equal-area grid for the annual analysis mosaics and a
WebMercatorQuad grid for the web-map archives.

The grid extents are fixed constants, the same for every year. All years
therefore share ``proj:shape`` and ``proj:transform`` and can be stacked or
differenced directly.

The WebMercator extents cover the scope's perimeter. The LAEA extents are the
WebMercator extents reprojected with densified edges and snapped outward to
:data:`ANALYSIS_ALIGNMENT`, so both grids cover the same area.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

from rasterio.transform import Affine

if TYPE_CHECKING:
    from collections.abc import Mapping

WEB_MERCATOR = "EPSG:3857"

LAEA = "EPSG:3035"
"""ETRS89-LAEA, the standard European equal-area grid.

The annual analysis mosaics use it so that pixel counts measure area directly.
On WebMercator, a nominal 19.109 m pixel is 13.2 m on the ground at 46.5
degrees north, and the factor varies with latitude across the perimeter.
"""

COMPOSITE_RESOLUTION = 10.0
"""Pixel size of the per-tile composites on the MGRS grid, in metres."""

ANALYSIS_RESOLUTION = 40.0
"""Pixel size of the annual analysis mosaics, in metres."""

ANALYSIS_ALIGNMENT = 1000.0
"""Analysis extents are snapped outward to multiples of this, in metres.

Whole-kilometre edges follow the European reference-grid convention, and the
40 m pixel divides them evenly.
"""

MERCATOR_ORIGIN = -20037508.342789244
"""Left and top edge of the WebMercatorQuad tile matrix, in metres."""

MERCATOR_SPAN_M = 2 * 20037508.342789244
"""Full width of the WebMercatorQuad tile matrix, in metres."""

TILE_PX = 256
"""Side of one WebMercatorQuad tile, in pixels."""

MOSAIC_ZOOM = 13
"""Zoom level of the WebMercatorQuad display grid, and the web maps' top zoom.

The display grid has this zoom's pixel size and is aligned to its tiles, so
tiles at this zoom are cut from it without resampling.
"""


def zoom_resolution(zoom: int) -> float:
    """Ground resolution of one WebMercatorQuad pixel at ``zoom``, in metres."""
    return MERCATOR_SPAN_M / (TILE_PX * 2**zoom)


VIZ_MIN_ZOOM = 5
"""Coarsest zoom level a web-map archive holds."""

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
    def step(self) -> float:
        """Granularity an extent is snapped to, in CRS units.

        One whole tile on the WebMercatorQuad grid, so a mosaic begins and ends
        on tile boundaries. One pixel on any other grid, so a warped zone lands
        on the target's pixel centres.
        """
        return self.resolution * TILE_PX if self.crs == WEB_MERCATOR else self.resolution

    def snap(self, bounds: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        """Grow ``bounds`` outward until they land on this grid.

        Steps are counted from the grid's own origin. A warped zone then shares
        the target's pixel centres, so mosaicking the zones resamples nothing.
        """
        step = self.step
        left, bottom, right, top = bounds
        ox, oy = self.bounds[0], self.bounds[1]
        return (
            ox + math.floor((left - ox) / step) * step,
            oy + math.floor((bottom - oy) / step) * step,
            ox + math.ceil((right - ox) / step) * step,
            oy + math.ceil((top - oy) / step) * step,
        )

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
    analysis_grid
        Equal-area grid the annual mosaics are written on.
    display_grid
        WebMercatorQuad grid at :data:`MOSAIC_ZOOM` that the web-map archives
        are tiled on. Both grids are resampled directly from the UTM
        composites, so each output is resampled only once.
    perimeter_tiles
        Number of MGRS tiles in the scope's perimeter. Completeness checks
        compare the tiles present against this number.
    utm_zones
        UTM zones the perimeter's MGRS tiles fall in, northern hemisphere, as
        the prose names them: ``("32N",)`` for Switzerland.
    """

    key: str
    title: str
    analysis_grid: Grid
    display_grid: Grid
    perimeter_tiles: int
    utm_zones: tuple[str, ...]

    @property
    def catalog_title(self) -> str:
        """Title of the root catalog, as ``root`` links to it must repeat.

        Portolan requires a title on every ``child`` and ``item`` link. Clients
        show link titles without fetching the target, so the link carries it.
        """
        return f"GLACE {self.title}"

    @property
    def tiles_collection_id(self) -> str:
        """Identifier of the per-tile collection, e.g. ``glace-ch-tiles``."""
        return f"glace-{self.key}-tiles"

    @property
    def mosaics_collection_id(self) -> str:
        """Identifier of the annual-mosaic collection, e.g. ``glace-ch-mosaics``."""
        return f"glace-{self.key}-mosaics"

    def mosaic_item_id(self, year: int) -> str:
        """Build the identifier of one year's mosaic item, e.g. ``ch-mosaic-2024``."""
        return f"{self.key}-mosaic-{year}"


SCOPES: Mapping[str, Scope] = {
    "ch": Scope(
        key="ch",
        title="Switzerland",
        analysis_grid=Grid(
            crs=LAEA,
            resolution=ANALYSIS_RESOLUTION,
            bounds=(4069000.0, 2524000.0, 4357000.0, 2698000.0),
        ),
        display_grid=Grid(
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
        # in config/lrw-ch.yml), so the perimeter is the SGI2023 glacier
        # inventory buffered by 250 m.
        perimeter_tiles=143,
        utm_zones=("32N",),
    ),
    "alps": Scope(
        key="alps",
        title="Alps",
        analysis_grid=Grid(
            crs=LAEA,
            resolution=ANALYSIS_RESOLUTION,
            bounds=(3894000.0, 2253000.0, 4858000.0, 2832000.0),
        ),
        display_grid=Grid(
            crs=WEB_MERCATOR,
            resolution=MOSAIC_RESOLUTION,
            bounds=(
                528332.7395071387,
                5371382.851655904,
                1849164.5882749856,
                6173665.900537115,
            ),
        ),
        # The Alpine Convention perimeter, 2025 edition, as MGRS tiles.
        perimeter_tiles=2210,
        # The perimeter spans about 5°E to 17°E.
        utm_zones=("31N", "32N", "33N"),
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

    The mosaic then begins and ends on a tile boundary, so every edge tile is
    complete.
    """
    step = zoom_resolution(zoom) * TILE_PX
    left, bottom, right, top = bounds
    return (
        MERCATOR_ORIGIN + math.floor((left - MERCATOR_ORIGIN) / step) * step,
        MERCATOR_ORIGIN + math.floor((bottom - MERCATOR_ORIGIN) / step) * step,
        MERCATOR_ORIGIN + math.ceil((right - MERCATOR_ORIGIN) / step) * step,
        MERCATOR_ORIGIN + math.ceil((top - MERCATOR_ORIGIN) / step) * step,
    )
