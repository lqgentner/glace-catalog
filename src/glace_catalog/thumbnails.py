"""Collection thumbnails, cut out of the published web-map archives.

A thumbnail is a picture of the data, so it must be drawn the way the data is
drawn. The one rendering of GLACE that is already reviewed is the committed
MapLibre style (M-27), and the PMTiles archives ``publish-year`` bakes are that
style applied to the pixels — colour-mapped, stretched and resampled once, in
the build. Reading the framed area back out of an archive therefore obeys the
published style *by construction*: no stretch, ramp or range is repeated here,
and there is nothing to keep in sync (R-9).

That is also why there is no headless map renderer in this module. A raster
PMTiles archive already holds rendered tiles; chiitiler or MapLibre would only
re-render what the tiler has drawn.

Both images come from the latest year's ``coh12_rgb`` archive: the mosaics
collection gets its scope's whole display extent, the tiles collection gets one
MGRS tile. Each is framed to 3:2 — the shape a catalog browser's card wants —
by *widening* the short side about the centre, never by cropping data away.
"""

from __future__ import annotations

from contextlib import contextmanager
import gzip
import io
import logging
import math
from typing import TYPE_CHECKING

from freezebase.mgrs import MGRSGeoBox
from PIL import Image
from pmtiles.reader import Reader
from pmtiles.tile import Compression, TileType
from upath import UPath

from glace_catalog.scopes import MERCATOR_ORIGIN, TILE_PX, zoom_resolution

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from pmtiles.tile import HeaderDict

    from glace_catalog.scopes import Scope
    from glace_catalog.stac.store import StoreLayout

logger = logging.getLogger(__name__)

Bbox = tuple[float, float, float, float]
"""A bounding box as ``(west, south, east, north)``."""

THUMBNAIL_LAYER = "coh12_rgb"
"""Archive both thumbnails are cut out of.

The false-colour coherence composition is the flagship product — it is what the
viewer opens on and what the dataset is about — and it is the one layer that
shows all three bands at once, so a single image says what the collection holds.
"""

THUMBNAIL_TILE = "32TMS24"
"""MGRS tile the per-tile collection is pictured by.

Aletsch: the largest glacier in the Alps, entirely inside one 10 km tile, and
the scene a reader is most likely to recognise.
"""

THUMBNAIL_WIDTH = 1200
"""Width of a written thumbnail, in pixels; the height follows from the 3:2 frame."""

FRAME_ASPECT = 3.0 / 2.0
"""Width-to-height ratio a thumbnail is framed to, in projected space."""

EARTH_RADIUS_M = 6378137.0
"""Sphere radius WebMercator is defined on."""

MERCATOR_TOP_M = -MERCATOR_ORIGIN
"""Northern edge of the WebMercatorQuad matrix; pixel rows count down from it."""

_RASTER_TILE_TYPES = frozenset({TileType.PNG, TileType.JPEG, TileType.WEBP, TileType.AVIF})
"""Tile types Pillow can decode. A vector archive is refused rather than drawn empty."""


def to_mercator(lon: float, lat: float) -> tuple[float, float]:
    """Project one WGS84 coordinate to WebMercator metres."""
    return (
        math.radians(lon) * EARTH_RADIUS_M,
        math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0)) * EARTH_RADIUS_M,
    )


def to_wgs84(x: float, y: float) -> tuple[float, float]:
    """Unproject one WebMercator coordinate, in metres, to WGS84 degrees."""
    return (
        math.degrees(x / EARTH_RADIUS_M),
        math.degrees(2.0 * math.atan(math.exp(y / EARTH_RADIUS_M)) - math.pi / 2.0),
    )


def frame_3_2(bbox: Bbox) -> Bbox:
    """Widen the short side of a bounding box until its projected aspect is 3:2.

    The aspect that matters is the one the picture is drawn at, which is
    WebMercator's, not the degree box's: at 46 degrees north a square kilometre
    is 1.45 times wider in degrees of longitude than in degrees of latitude.
    So the box is converted, grown about its centre, and converted back.

    Growing, never cutting: a thumbnail that cropped the collection's extent
    would show less than the collection holds, and the empty margin a widened
    box gains is transparent rather than wrong.

    Parameters
    ----------
    bbox
        Bounding box in WGS84 degrees, ``(west, south, east, north)``.

    Returns
    -------
    tuple of float
        The framed box, in WGS84 degrees.
    """
    west, south, east, north = bbox
    left, bottom = to_mercator(west, south)
    right, top = to_mercator(east, north)
    span_x, span_y = right - left, top - bottom
    if span_x <= 0.0 or span_y <= 0.0:
        msg = f"Cannot frame the empty bounding box {bbox}."
        raise ValueError(msg)

    if span_x < span_y * FRAME_ASPECT:
        grow = (span_y * FRAME_ASPECT - span_x) / 2.0
        left, right = left - grow, right + grow
    else:
        grow = (span_x / FRAME_ASPECT - span_y) / 2.0
        bottom, top = bottom - grow, top + grow

    west, south = to_wgs84(left, bottom)
    east, north = to_wgs84(right, top)
    return (west, south, east, north)


def mosaics_bbox(scope: Scope) -> Bbox:
    """WGS84 bounds of a scope's display grid, which the mosaics cover exactly.

    The committed extent, not one measured off the archive: the grid is what the
    collection publishes, and an archive drops its empty tiles, so its own header
    describes the data perimeter rather than the collection's footprint.
    """
    left, bottom, right, top = scope.display_grid.bounds
    west, south = to_wgs84(left, bottom)
    east, north = to_wgs84(right, top)
    return (west, south, east, north)


def tile_bbox(tile: str) -> Bbox:
    """WGS84 bounds of one MGRS tile, from the same geobox the items are built on."""
    extent = MGRSGeoBox.from_mgrs(tile).geographic_extent
    west, south, east, north = extent.boundingbox.bbox
    return (float(west), float(south), float(east), float(north))


def pixel_bounds(bbox: Bbox, zoom: int) -> tuple[float, float, float, float]:
    """Position of a WGS84 box on the WebMercatorQuad pixel grid at ``zoom``.

    Returns
    -------
    tuple of float
        ``(left, top, right, bottom)`` in pixels from the matrix origin, as
        fractions rather than whole pixels: the crop is resampled, so rounding
        it to a pixel would shift the frame off centre for no gain.
    """
    resolution = zoom_resolution(zoom)
    west, south, east, north = bbox
    left, bottom = to_mercator(west, south)
    right, top = to_mercator(east, north)
    return (
        (left - MERCATOR_ORIGIN) / resolution,
        (MERCATOR_TOP_M - top) / resolution,
        (right - MERCATOR_ORIGIN) / resolution,
        (MERCATOR_TOP_M - bottom) / resolution,
    )


def choose_zoom(bbox: Bbox, *, width: int, min_zoom: int, max_zoom: int) -> int:
    """Highest zoom in an archive at which ``bbox`` is no wider than ``width`` pixels.

    Fetching a level finer than the output would download tiles only to throw
    their detail away. If even the coarsest level in the archive is wider than
    the output, that level is used and the crop is downsampled.
    """
    for zoom in range(max_zoom, min_zoom, -1):
        left, _, right, _ = pixel_bounds(bbox, zoom)
        if right - left <= width:
            return zoom
    return min_zoom


@contextmanager
def _range_reader(archive: Path | UPath) -> Iterator[Callable[[int, int], bytes]]:
    """Open an archive as the ``get_bytes(offset, length)`` callable the reader wants.

    One handle, seeked per read. On S3 that is a range request per call, which is
    what keeps a thumbnail to a few dozen kilobytes off an archive of gigabytes.
    """
    with UPath(archive).open("rb") as handle:

        def get_bytes(offset: int, length: int) -> bytes:
            handle.seek(offset)
            return bytes(handle.read(length))

        yield get_bytes


def _decode_tile(payload: bytes, compression: Compression) -> Image.Image:
    """Decode one tile's bytes into an RGBA image."""
    if compression == Compression.GZIP:
        payload = gzip.decompress(payload)
    with Image.open(io.BytesIO(payload)) as tile:
        return tile.convert("RGBA")


def _paste_tiles(
    reader: Reader,
    header: HeaderDict,
    zoom: int,
    bounds: tuple[float, float, float, float],
) -> tuple[Image.Image, tuple[int, int]]:
    """Composite every archive tile covering ``bounds`` onto a transparent canvas.

    Returns
    -------
    tuple
        The canvas, and its own upper-left corner in pixels from the matrix
        origin, so a caller can place ``bounds`` on it.
    """
    left, top, right, bottom = bounds
    last = 2**zoom - 1
    x_range = range(
        min(max(math.floor(left / TILE_PX), 0), last),
        min(max(math.ceil(right / TILE_PX) - 1, 0), last) + 1,
    )
    y_range = range(
        min(max(math.floor(top / TILE_PX), 0), last),
        min(max(math.ceil(bottom / TILE_PX) - 1, 0), last) + 1,
    )
    origin = (x_range.start * TILE_PX, y_range.start * TILE_PX)
    canvas = Image.new(
        "RGBA",
        (len(x_range) * TILE_PX, len(y_range) * TILE_PX),
        (0, 0, 0, 0),
    )
    found = 0
    for x in x_range:
        for y in y_range:
            payload = reader.get(zoom, x, y)
            if payload is None:
                # The tiler drops fully transparent tiles, so a hole in the
                # archive is nodata and stays transparent on the canvas.
                continue
            canvas.paste(
                _decode_tile(payload, header["tile_compression"]),
                (x * TILE_PX - origin[0], y * TILE_PX - origin[1]),
            )
            found += 1
    logger.debug(
        "z%d: %d of %d tile(s) present",
        zoom,
        found,
        len(x_range) * len(y_range),
    )
    return canvas, origin


def render_from_pmtiles(
    archive: Path | UPath,
    bbox_wgs84: Bbox,
    *,
    width: int = THUMBNAIL_WIDTH,
) -> Image.Image:
    """Cut one WGS84 box out of a raster PMTiles archive as an RGBA image.

    Parameters
    ----------
    archive
        The ``.pmtiles`` file, local or on object storage. It is read through
        range requests and never downloaded whole.
    bbox_wgs84
        Area to draw, ``(west, south, east, north)``. Frame it with
        :func:`frame_3_2` first if the output shape matters.
    width
        Output width in pixels. The height follows from the box's projected
        aspect, so a 3:2 box at the default width gives 1200x800.

    Returns
    -------
    PIL.Image.Image
        RGBA image; the archive's missing tiles stay fully transparent.

    Raises
    ------
    ValueError
        If the archive holds vector tiles, which are not an image.
    """
    with _range_reader(archive) as get_bytes:
        reader = Reader(get_bytes)
        header = reader.header()
        tile_type = header["tile_type"]
        if tile_type not in _RASTER_TILE_TYPES:
            msg = f"{archive} holds {tile_type.name} tiles, which cannot be decoded as an image."
            raise ValueError(msg)
        zoom = choose_zoom(
            bbox_wgs84,
            width=width,
            min_zoom=header["min_zoom"],
            max_zoom=header["max_zoom"],
        )
        bounds = pixel_bounds(bbox_wgs84, zoom)
        canvas, origin = _paste_tiles(reader, header, zoom, bounds)

    left, top, right, bottom = bounds
    height = max(1, round(width * (bottom - top) / (right - left)))
    # Crop and rescale in one resampling pass: `box` takes the fractional crop
    # in source pixels, so the frame is not first rounded to a whole pixel.
    return canvas.resize(
        (width, height),
        Image.Resampling.LANCZOS,
        box=(left - origin[0], top - origin[1], right - origin[0], bottom - origin[1]),
    )


def write_collection_thumbnails(
    layout: StoreLayout,
    year: int,
    *,
    scope: Scope,
) -> None:
    """Render and write both collection thumbnails from one year's archive.

    The mosaics collection is pictured by the whole scope, the tiles collection
    by :data:`THUMBNAIL_TILE`. Both are framed to 3:2 and written as PNG, which
    is what the ``thumbnail`` asset on each collection document declares.

    Parameters
    ----------
    layout
        The store being finalized.
    year
        Published year to draw; normally the most recent one.
    scope
        Scope the store covers, for the mosaics collection's extent.
    """
    archive = layout.pmtiles(year) / f"{THUMBNAIL_LAYER}.pmtiles"
    if not archive.exists():
        logger.warning("%s does not exist; thumbnails not rendered", archive)
        return

    for destination, bbox in (
        (layout.mosaics_thumbnail, mosaics_bbox(scope)),
        (layout.tiles_thumbnail, tile_bbox(THUMBNAIL_TILE)),
    ):
        image = render_from_pmtiles(archive, frame_3_2(bbox))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        payload = buffer.getvalue()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        logger.info(
            "Wrote %s from %d (%dx%d, %.0f kB)",
            destination,
            year,
            image.width,
            image.height,
            len(payload) / 1e3,
        )
