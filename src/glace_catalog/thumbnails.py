"""Collection thumbnails from the latest analysis COGs over a muted OpenStreetMap basemap.

Both collections use a 1400 x 933 landscape frame. 3:2 is what a catalog
browser's card is shaped like, so it is what Portolan's thumbnail guidance
frames to; no conformance rule fixes an aspect (rashid checks only that a
geospatial collection carries a thumbnail asset whose type is PNG, JPEG or
WebP), but a thumbnail drawn at any other shape is letterboxed or cropped by
the reader that matters most. The mosaic and the actual MGRS tile keep their
projected proportions inside that frame: the short axis grows, nothing is cut.

1400 px wide is twice the widest card, so the image is still sharp where the
card is laid out at 700 CSS px on a 2x display — the case where a smaller
thumbnail is upscaled and reads as soft. A browser downscales for everyone
else at no visible cost.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import contextily as cx
from freezebase.raster import rasterio_open
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import numpy as np
from PIL import Image
from rasterio.enums import Resampling
from xyzservices.providers import providers

from glace_catalog.metadata import vocabulary
from glace_catalog.stac.naming import published_name
from glace_catalog.styles import resolve_cmap

if TYPE_CHECKING:
    from matplotlib.axes import Axes
    from rasterio.crs import CRS
    from upath import UPath
    from xyzservices import TileProvider

    from glace_catalog.stac.store import StoreLayout

logger = logging.getLogger(__name__)

THUMBNAIL_LAYER = "coh12_vv"
THUMBNAIL_TILE = "32TMS24"
THUMBNAIL_WIDTH = 1400
THUMBNAIL_HEIGHT = 933
THUMBNAIL_MARGIN = 0.05
THUMBNAIL_QUALITY = 90
FIGURE_WIDTH_IN = 6.0
"""Figure width in inches, held constant so the dpi carries the pixel size.

Matplotlib sizes text in points, so a figure whose inches grow with the output
draws the credit at a fixed pixel height that shrinks against an ever-larger
picture. Fixing the inches and deriving the dpi instead makes every typographic
size a constant fraction of the image, at any :data:`THUMBNAIL_WIDTH`.
"""

ATTRIBUTION_SIZE = "small"
"""Credit size, named rather than numeric so it scales with :data:`FIGURE_WIDTH_IN`."""

ASCII_COPYRIGHT = "(C)"
"""What xyzservices spells the copyright sign as in a provider's own credit."""

DEFAULT_BASEMAP = "OpenStreetMap.Mapnik"


def cache_dir() -> Path:
    """Per-user directory contextily keeps downloaded tiles in.

    Under ``XDG_CACHE_HOME`` rather than the system temporary directory: a
    fixed name in a world-writable ``/tmp`` is a directory another account can
    already own, which turns the first cache write into a ``PermissionError``
    that has nothing to do with the catalog.
    """
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "glace-catalog" / "basemap-tiles"


def resolve_basemap(basemap: str) -> TileProvider | str:
    """Look ``basemap`` up as a contextily provider, else as a local raster.

    Raises
    ------
    ValueError
        If the name is neither. Falling through to contextily would hand it a
        provider name as a file path, and the run would fail with a
        "no such file or directory" that names the wrong problem.
    """
    known = providers.flatten()
    if basemap in known:
        return known[basemap]
    if Path(basemap).exists():
        return basemap
    msg = (
        f"{basemap!r} is neither a contextily provider nor an existing raster; "
        f"provider names look like {DEFAULT_BASEMAP!r}"
    )
    raise ValueError(msg)


def add_background(
    ax: Axes,
    basemap: str,
    attribution: str | None,
    crs: CRS,
    *,
    attribution_size: str | float = ATTRIBUTION_SIZE,
) -> None:
    """Add a credited basemap; render standard OSM tiles in grayscale.

    Raises
    ------
    ValueError
        If a tile provider's credit is suppressed with ``attribution=""``. The
        published thumbnails carry the basemap's cartography into a CC-BY-4.0
        release, so the credit is the licence term, not a caption: only an
        attribution-free local raster may go without one.
    """
    source = resolve_basemap(basemap)
    if attribution is None and not isinstance(source, str):
        # The provider's own wording, with the copyright sign it spells out in
        # ASCII: xyzservices carries "(C)" for portability, and the thumbnail is
        # a picture, where the sign itself is what a reader expects to see.
        declared = str(source.get("attribution") or "")
        attribution = declared.replace(ASCII_COPYRIGHT, "\N{COPYRIGHT SIGN}") or None
    if attribution == "" and not isinstance(source, str):
        msg = (
            f"{basemap!r} is a tile provider, whose credit cannot be suppressed; "
            "an empty attribution is only for an attribution-free local raster"
        )
        raise ValueError(msg)
    if basemap == DEFAULT_BASEMAP:
        # Reuse downloaded tiles between runs, as required by OSM's tile policy.
        cache = cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        cx.set_cache_dir(str(cache))
    cx.add_basemap(
        ax,
        source=source,
        crs=crs,
        attribution=False if attribution == "" else attribution,
        zoom_adjust=1,
        attribution_size=attribution_size,
        headers={"user-agent": "glace-catalog-thumbnail/0.1"},
        timeout=30,
        zorder=0,
    )
    if basemap == DEFAULT_BASEMAP:
        for artist in ax.images:
            rgba = np.asarray(artist.get_array()).copy()
            gray = rgba[..., :3] @ np.array([0.2126, 0.7152, 0.0722])
            rgba[..., :3] = gray[..., None].astype("uint8")
            artist.set_data(rgba)


def render_from_cog(
    source: Path | UPath,
    *,
    basemap: str = DEFAULT_BASEMAP,
    attribution: str | None = None,
    width: int = THUMBNAIL_WIDTH,
    height: int = THUMBNAIL_HEIGHT,
    attribution_size: str | float = ATTRIBUTION_SIZE,
) -> Image.Image:
    """Plot coherence VV over a basemap, preserving native projected aspect.

    Read only a thumbnail-sized overview of the source. Nodata and values outside [0, 1]
    are transparent; zero coherence is valid. Basemap failures and empty data fail the render, so a
    basemap alone cannot masquerade as a successful thumbnail.

    ``basemap`` accepts a contextily provider name or a local georeferenced
    raster. Providers retain their credit, which only an attribution-free local
    raster (for example Natural Earth) may drop with ``attribution=""``.
    """
    if width < 1 or height < 1:
        msg = "Thumbnail dimensions must be positive"
        raise ValueError(msg)
    with rasterio_open(source) as src:
        if src.crs is None or not src.crs.is_projected:
            msg = f"{source} must have a projected CRS"
            raise ValueError(msg)
        if src.transform.b != 0 or src.transform.d != 0:
            msg = f"{source} must be an axis-aligned raster"
            raise ValueError(msg)
        if src.transform.a <= 0 or src.transform.e >= 0:
            # `imshow` draws row 0 at `top` and column 0 at `left`. A south-up
            # or east-left raster satisfies both checks above and comes out
            # mirrored -- a wrong picture that is neither blank nor an error,
            # so nothing downstream would catch it.
            msg = f"{source} must be north-up and east-right"
            raise ValueError(msg)
        left, bottom, right, top = src.bounds
        span_x, span_y = right - left, top - bottom
        read_height = max(1, round(width * span_y / span_x))
        data = src.read(
            1,
            out_shape=(read_height, width),
            masked=True,
            resampling=Resampling.average,
        )
        crs = src.crs
    data = np.ma.masked_invalid(data)
    data = np.ma.masked_outside(data, 0, 1)
    if data.count() == 0:
        msg = f"{source} has no valid coherence"
        raise ValueError(msg)
    style = vocabulary().display(THUMBNAIL_LAYER)

    figure = Figure(
        figsize=(FIGURE_WIDTH_IN, FIGURE_WIDTH_IN * height / width),
        dpi=width / FIGURE_WIDTH_IN,
    )
    canvas = FigureCanvasAgg(figure)
    ax = figure.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    frame_x = span_x * (1 + 2 * THUMBNAIL_MARGIN)
    frame_y = span_y * (1 + 2 * THUMBNAIL_MARGIN)
    aspect = width / height
    frame_x = max(frame_x, frame_y * aspect)
    frame_y = max(frame_y, frame_x / aspect)
    center_x, center_y = (left + right) / 2, (bottom + top) / 2
    xlim = (center_x - frame_x / 2, center_x + frame_x / 2)
    ylim = (center_y - frame_y / 2, center_y + frame_y / 2)
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    add_background(ax, basemap, attribution, crs, attribution_size=attribution_size)
    ax.imshow(
        data,
        extent=(left, right, bottom, top),
        cmap=resolve_cmap(style.cmap),
        vmin=style.vmin,
        vmax=style.vmax,
        interpolation="nearest",
        zorder=1,
    )
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)
    canvas.draw()
    image = Image.frombuffer("RGBA", canvas.get_width_height(), bytes(canvas.buffer_rgba())).copy()
    figure.clear()
    return image


def write_collection_thumbnails(
    layout: StoreLayout,
    year: int,
    *,
    basemap: str = DEFAULT_BASEMAP,
    attribution: str | None = None,
) -> None:
    """Write the mosaic and Aletsch tile thumbnails from the selected latest year.

    Render both before writing either, so a missing tile or failed basemap does
    not leave a newly rendered mosaic paired with an older tile thumbnail.

    Written as lossy WebP at :data:`THUMBNAIL_QUALITY`, flattened to RGB: the
    frame is opaque, the basemap covers it edge to edge, and the losslessly
    stored copy of these pixels is the COG the picture is drawn from. Lossless
    here would roughly treble the bytes a card loads to preserve detail no
    reader sees, and at this quality the only visible cost is a trace of
    smoothing in the speckle.
    """
    images = [
        (
            destination,
            render_from_cog(
                source,
                basemap=basemap,
                attribution=attribution,
            ),
        )
        for destination, source in (
            (layout.mosaics_thumbnail, layout.mosaic_year(year) / published_name(THUMBNAIL_LAYER)),
            (
                layout.tiles_thumbnail,
                layout.tile(year, THUMBNAIL_TILE) / published_name(THUMBNAIL_LAYER),
            ),
        )
    ]
    for destination, image in images:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="WEBP", quality=THUMBNAIL_QUALITY)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(buffer.getvalue())
        logger.info("Wrote %s from %d (%dx%d)", destination, year, image.width, image.height)
