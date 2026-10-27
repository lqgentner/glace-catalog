"""Collection thumbnails: the latest analysis COGs over a grey OpenStreetMap basemap.

Both thumbnails use a 1400 x 933 frame. The 3:2 shape matches the cards of
STAC catalog browsers, which Portolan's thumbnail guidance follows. The mosaic
and the example tile keep their projected proportions inside the frame, with
margin added along the shorter axis.

The width of 1400 px is twice the widest card (700 CSS px), so thumbnails stay
sharp on 2x displays.
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
from glace_catalog.stac.store import put_object
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
"""Figure width in inches. The dpi sets the pixel size.

Matplotlib sizes text in points. With a fixed width in inches, text keeps the
same size relative to the image at any :data:`THUMBNAIL_WIDTH`.
"""

ATTRIBUTION_SIZE = "small"
"""Credit font size, as a named size that scales with :data:`FIGURE_WIDTH_IN`."""

ASCII_COPYRIGHT = "(C)"
"""How xyzservices writes the copyright sign in a provider's credit."""

DEFAULT_BASEMAP = "OpenStreetMap.Mapnik"


def cache_dir() -> Path:
    """Per-user directory for contextily's downloaded tiles.

    It lives under ``XDG_CACHE_HOME``, which belongs to the current user, so
    other accounts cannot own it.
    """
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "glace-catalog" / "basemap-tiles"


def resolve_basemap(basemap: str) -> TileProvider | str:
    """Look ``basemap`` up as a contextily provider, else as a local raster.

    Raises
    ------
    ValueError
        If the name is neither.
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
    """Add a credited basemap; render standard OSM tiles in greyscale.

    Raises
    ------
    ValueError
        If a tile provider's credit is suppressed with ``attribution=""``. The
        basemap's licence requires the credit; only an attribution-free local
        raster may omit it.
    """
    source = resolve_basemap(basemap)
    if attribution is None and not isinstance(source, str):
        # Use the provider's credit, with xyzservices' ASCII "(C)" replaced by
        # the copyright sign.
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
    """Plot VV coherence over a basemap, keeping the raster's projected aspect.

    Only a thumbnail-sized overview of the source is read. Nodata and values
    outside [0, 1] are transparent; zero coherence is valid. A basemap failure
    or a raster without valid data raises an error, so every thumbnail shows
    data.

    ``basemap`` accepts a contextily provider name or a local georeferenced
    raster. Providers keep their credit; only an attribution-free local raster
    (for example Natural Earth) may drop it with ``attribution=""``.
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
            # `imshow` draws row 0 at `top` and column 0 at `left`, so any other
            # orientation would be drawn mirrored without an error.
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
    """Write the mosaic and Aletsch tile thumbnails from ``year``.

    Both are rendered before either is written, so the two thumbnails always
    come from the same run.

    The images are opaque, so they are saved as RGB lossy WebP at
    :data:`THUMBNAIL_QUALITY`, which keeps them small at no visible cost.
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
        put_object(buffer.getvalue(), destination)
        logger.info("Wrote %s from %d (%dx%d)", destination, year, image.width, image.height)
