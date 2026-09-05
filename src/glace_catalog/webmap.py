"""Web-map tiling of the annual mosaics into PMTiles archives.

Colour-maps a display-grid mosaic into RGBA and packs it into one PMTiles
archive per layer. Nothing is written beside them: what a reader needs in order
to draw a legend lives in the committed MapLibre style (M-27), and what it needs
in order to find an archive is a ``rel="pmtiles"`` link on the mosaics
collection (M-26). The ``layers.json`` manifest both used to travel in is
retired.

Tiling goes through ``rio-pmtiles``, the rasterio plugin Protomaps recommends for
raster PMTiles (tippecanoe, its counterpart for vector data, cannot produce
raster tiles). It reprojects to WebMercator and writes the pyramid in one pass,
so the only intermediate is the colour-mapped RGBA raster — and that is itself a
VRT, built from per-band lookup tables, so nothing at continental scale is ever
materialised.

The source is the **VRT chain's canonical-grid mosaic**, which already sits on
the WebMercatorQuad grid at the maximum zoom. That retires the double resampling
of the old 40 m EPSG:3035 overview: at max zoom the tiler's warp is the identity,
and every level below it is a clean 2x decimation, which is why the resampling
kernel here is ``average`` rather than the ``bilinear`` an oversampled source
needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, Any

from freezebase.raster import rasterio_open
from freezebase.s3 import subprocess_s3_env
from freezebase.vrt import create_rgba_vrt
import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.warp import transform_bounds
from upath import UPath

from glace_catalog.mosaics import chain_env
from glace_catalog.scopes import MOSAIC_ZOOM
from glace_catalog.styles import (
    DEFAULT_STYLE_PATH,
    LayerStyle,
    RgbStyle,
    load_attribution,
    load_styles,
    ramp_colors,
    resolve_cmap,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    import geopandas as gpd

    from glace_catalog.mosaics import YearVrts

logger = logging.getLogger(__name__)

WEB_MERCATOR = "EPSG:3857"
# Full width of the WebMercatorQuad tile matrix, in metres.
MERCATOR_SPAN_M = 2 * 20037508.342789244
TILE_SIZE = 256

# Layer name inside tile-grid.pmtiles. Fixed rather than recorded in the
# manifest: unlike the inventories there is only ever one, and the viewer needs
# it as `source-layer` -- a vector layer with the wrong one renders nothing and
# reports no error.
TILE_GRID_LAYER = "grid"


@dataclass
class LayerInfo:
    """One built PMTiles archive, as the build reports it.

    A build-time record, not a published document: ``publish-year`` writes it
    into the year's build state so a later run can tell what changed. What a
    reader sees instead is the archive's own header, the collection's
    ``rel="pmtiles"`` link, and the style's legend.
    """

    id: str
    product: str
    polarization: str
    year: int
    url: str
    bounds: list[float]
    min_zoom: int
    max_zoom: int
    cmap: str
    vmin: float
    vmax: float
    units: str
    source: str
    size_bytes: int
    start_date: str
    end_date: str
    colors: list[str] = field(default_factory=list)


def zoom_resolution(zoom: int) -> float:
    """Ground resolution of a WebMercatorQuad zoom level at the equator, in metres."""
    return MERCATOR_SPAN_M / (TILE_SIZE * 2**zoom)


def native_max_zoom(res_m: float, lat_deg: float) -> int:
    """Smallest zoom level that does not undersample ``res_m`` at ``lat_deg``.

    WebMercator resolution is quoted at the equator; at latitude ``lat_deg`` a
    zoom level covers ``cos(lat)`` times as much ground per pixel, so the Alps
    reach their native detail one level earlier than the equatorial figure
    suggests.
    """
    scale = math.cos(math.radians(lat_deg))
    return max(0, math.ceil(math.log2(MERCATOR_SPAN_M * scale / (TILE_SIZE * res_m))))


def color_lookup_table(cmap: str, size: int = 256) -> np.ndarray:
    """Build a ``(size, 3)`` uint8 RGB lookup table from a colormap."""
    colormap = resolve_cmap(cmap)
    table = np.asarray(colormap(np.linspace(0.0, 1.0, size)))[:, :3]
    return (table * 255.0).round().astype("uint8")


def estimate_stretch(
    dataset: rasterio.DatasetReader,
    *,
    percentiles: tuple[float, float] = (2.0, 98.0),
    windows: int = 9,
    window_size: int = 1200,
) -> tuple[float, float]:
    """Percentile stretch measured on native-resolution samples of the first band.

    Sampling happens at full resolution, in a grid of windows across the raster,
    rather than as one decimated read of the whole thing. A decimated read
    averages SAR speckle away and reports a distribution far narrower than the
    one the max-zoom tiles are drawn from -- for Alpine RTC VV the 2-98 range
    narrows from about 14 dB to 5 dB -- which yields a stretch that clips a
    tenth of the scene to pure white.

    Parameters
    ----------
    dataset
        Open raster to sample.
    percentiles
        Low and high percentile the range is cut at.
    windows
        Approximate number of sample windows; they are spread over a square
        grid covering the raster.
    window_size
        Side length of each sample window, in pixels.

    Returns
    -------
    tuple[float, float]
        The low and high value of the stretch.
    """
    per_axis = max(1, math.isqrt(windows))
    width = min(window_size, dataset.width)
    height = min(window_size, dataset.height)
    samples: list[np.ndarray] = []
    for row in range(per_axis):
        for col in range(per_axis):
            # Offsets land at the centre of each grid cell, clamped so the
            # window stays inside the raster.
            x = int((col + 0.5) * dataset.width / per_axis - width / 2)
            y = int((row + 0.5) * dataset.height / per_axis - height / 2)
            window = rasterio.windows.Window(
                min(max(x, 0), dataset.width - width),
                min(max(y, 0), dataset.height - height),
                width,
                height,
            )
            block = dataset.read(1, window=window, masked=True)
            values = np.asarray(block.compressed(), dtype="float64")
            samples.append(values[np.isfinite(values)])

    pooled = np.concatenate(samples) if samples else np.empty(0)
    if pooled.size == 0:
        msg = "Cannot estimate a stretch: the sampled windows hold no valid pixels"
        raise ValueError(msg)
    low, high = np.percentile(pooled, percentiles)
    return float(low), float(high)


def _wgs84_bounds(dataset: rasterio.DatasetReader) -> list[float]:
    """Geographic bounding box of a dataset, as ``[west, south, east, north]``."""
    west, south, east, north = transform_bounds(dataset.crs, "EPSG:4326", *dataset.bounds)
    return [west, south, east, north]


def _centre_latitude(dataset: rasterio.DatasetReader) -> float:
    """Latitude of the dataset's centre, in degrees."""
    _, south, _, north = _wgs84_bounds(dataset)
    return (south + north) / 2.0


def build_luts(
    cmap: str,
    vmin: float,
    vmax: float,
    *,
    stops: int = 256,
) -> list[list[tuple[float, int]]]:
    """Sample a colour ramp as three per-band lookup tables for a VRT.

    GDAL's ``ComplexSource`` supports a per-band ``<LUT>``, a piecewise-linear
    value-to-value table — which is exactly a colour ramp sampled at a few
    hundred stops. Three of them plus a constant-255 alpha give an RGBA VRT, so
    the colour-mapped intermediate never has to be materialised: at continental
    z13 it would be 11.6 GB uncompressed.
    """
    table = color_lookup_table(cmap, stops)
    values = np.linspace(vmin, vmax, stops)
    return [
        [(float(value), int(table[index, band])) for index, value in enumerate(values)]
        for band in range(3)
    ]


def build_rgb_luts(
    ranges: Sequence[tuple[float, float]],
    *,
    stops: int = 256,
) -> list[list[tuple[float, int]]]:
    """Per-band linear stretches for a false-colour layer, as VRT lookup tables.

    Each band is stretched on its own range and written straight to the matching
    output channel, so no colour map is involved.
    """
    return [
        [
            (float(vmin + (vmax - vmin) * index / (stops - 1)), round(255 * index / (stops - 1)))
            for index in range(stops)
        ]
        for vmin, vmax in ranges
    ]


def rgba_vrt(
    src_vrt: Path | UPath,
    out_vrt: Path,
    *,
    luts: Sequence[Sequence[tuple[float, int]]],
    src_bands: Sequence[int] = (1, 1, 1),
) -> Path:
    """Write the tiler's direct input: a 4-band uint8 RGBA VRT over ``src_vrt``."""
    create_rgba_vrt(src_vrt, out_vrt, luts=luts, src_bands=src_bands)
    return out_vrt


def write_rgba(
    dataset: rasterio.DatasetReader,
    out_path: Path,
    *,
    cmap: str,
    vmin: float,
    vmax: float,
    overview_levels: Sequence[int] = (2, 4, 8, 16, 32, 64, 128),
) -> None:
    """Colour-map a single-band raster into a tiled RGBA GeoTIFF on the same grid.

    Nodata becomes a fully transparent alpha, so the tiler can drop tiles that
    fall outside the composite perimeter. Overviews are built because the tiler
    resamples every zoom level below the maximum out of this file.

    Kept as the fallback for :func:`rgba_vrt`, which is the path a normal build
    takes: materialising this raster at continental scale costs 11.6 GB, so it
    is worth having only where the virtual one cannot be used.
    """
    lut = color_lookup_table(cmap)
    scale = 255.0 / (vmax - vmin)
    profile = dataset.profile | {
        "driver": "GTiff",
        "dtype": "uint8",
        "count": 4,
        "nodata": None,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
        "predictor": 2,
        "BIGTIFF": "IF_SAFER",
    }
    profile.pop("photometric", None)

    with rasterio.open(out_path, "w", **profile) as dst:
        dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        for _, window in dataset.block_windows(1):
            block = dataset.read(1, window=window, masked=True)
            opaque = np.ma.getmaskarray(block) == False  # noqa: E712 (ndarray, not bool)
            values = np.asarray(block.filled(vmin), dtype="float32")
            opaque &= np.isfinite(values)
            index = np.clip((values - vmin) * scale, 0.0, 255.0).astype("uint8")
            rgb = lut[index]
            for band in range(3):
                dst.write(rgb[..., band], band + 1, window=window)
            dst.write((opaque * 255).astype("uint8"), 4, window=window)

    if overview_levels:
        with rasterio.open(out_path, "r+") as dst:
            dst.build_overviews(list(overview_levels), Resampling.average)


def _rio_executable() -> str:
    """Path to the ``rio`` CLI of the running interpreter's environment."""
    candidate = Path(sys.executable).with_name("rio")
    if candidate.exists():
        return str(candidate)
    found = shutil.which("rio")
    if found is None:
        msg = "The 'rio' CLI was not found; install rasterio and rio-pmtiles"
        raise FileNotFoundError(msg)
    return found


def run_rio_pmtiles(
    rgba_path: Path | UPath,
    out_path: Path,
    *,
    min_zoom: int,
    max_zoom: int,
    tile_format: str = "WEBP",
    quality: int = 80,
    resampling: str = "bilinear",
    workers: int | None = None,
    title: str = "",
    attribution: str = "",
    sources_at: Path | UPath | None = None,
) -> None:
    """Tile an RGBA raster into a PMTiles archive with ``rio pmtiles``.

    The tiler reprojects to WebMercator itself and skips tiles that are entirely
    transparent, so an archive follows the data perimeter rather than its
    bounding box.

    ``sources_at`` names one of the rasters the input reads. The tiler is a
    subprocess, so none of this process's S3 configuration reaches it; the child
    is given the profile name and the endpoint instead, and never an access key.
    """
    out_path.unlink(missing_ok=True)
    command = [
        _rio_executable(),
        "pmtiles",
        str(rgba_path),
        str(out_path),
        "--rgba",
        "--overlay",
        "--format",
        tile_format,
        "--co",
        f"QUALITY={quality}",
        "--zoom-levels",
        f"{min_zoom}..{max_zoom}",
        "--tile-size",
        str(TILE_SIZE),
        "--resampling",
        resampling,
        "--exclude-empty-tiles",
        "--silent",
    ]
    if workers:
        command += ["-j", str(workers)]
    if title:
        command += ["--name", title]
    if attribution:
        command += ["--attribution", attribution]
    env = os.environ | (subprocess_s3_env(UPath(sources_at)) if sources_at is not None else {})
    logger.debug("Running %s", " ".join(command))
    subprocess.run(command, check=True, env=env)  # noqa: S603  # our own argv, no shell


def build_pmtiles(
    src_vrt: Path | UPath,
    out_path: Path,
    *,
    layer_id: str,
    year: int,
    style: LayerStyle | RgbStyle,
    start_date: str,
    end_date: str,
    min_zoom: int = 5,
    max_zoom: int = MOSAIC_ZOOM,
    tile_format: str = "WEBP",
    quality: int = 80,
    resampling: str = "average",
    workers: int | None = None,
    materialize_rgba: bool = False,
    sources_at: Path | UPath | None = None,
    attribution: str = "",
) -> LayerInfo:
    """Colour-map and tile one display-grid mosaic into a PMTiles archive.

    Parameters
    ----------
    src_vrt
        A VRT from the chain: the linear mosaic, its decibel reading, or a
        false-colour composition of the two polarizations.
    out_path
        Destination ``.pmtiles`` file. Must be local — packing an archive needs
        random writes.
    layer_id
        Identifier of the layer, which is also the archive's filename stem. The
        two are the same by rule, so the viewer can derive one from the other.
    year
        Composite year, for the manifest entry.
    style
        Colour ramp and value range, or the per-band ranges of a false-colour
        layer. Never a percentile stretch: the published ranges are fixed per
        layer so that a year slider compares like with like.
    start_date, end_date
        Composite window the layer covers.
    min_zoom, max_zoom
        Zoom range of the pyramid. The default maximum is the zoom the source
        already sits on, so that level is a copy and every level below it a 2x
        decimation.
    tile_format, quality, resampling, workers
        Passed to the tiler. ``average`` is right here because the source is on
        the target grid at max zoom; the earlier bilinear-versus-nearest question
        was about oversampling a 40 m source and no longer applies.
    materialize_rgba
        Write the colour-mapped RGBA as a real GeoTIFF instead of a VRT. The
        fallback for a GDAL that cannot serve the lookup tables virtually.
    sources_at
        One of the rasters ``src_vrt`` reads, so the tiler subprocess can be
        given the object store's configuration.

    Returns
    -------
    LayerInfo
        Manifest entry describing the archive that was written.

    Raises
    ------
    ValueError
        If the value range is empty, or the zoom range inverted.
    """
    if min_zoom > max_zoom:
        msg = f"min_zoom {min_zoom} exceeds max_zoom {max_zoom}"
        raise ValueError(msg)

    if isinstance(style, RgbStyle):
        luts = build_rgb_luts(style.ranges)
        src_bands = (1, 2, 3)
        cmap, (vmin, vmax) = "", (style.ranges[0][0], style.ranges[0][1])
        colors: list[str] = []
    else:
        if style.vmin is None or style.vmax is None:
            msg = f"{layer_id} has no fixed value range; a per-layer stretch is not allowed"
            raise ValueError(msg)
        vmin, vmax = style.vmin, style.vmax
        if vmax <= vmin:
            msg = f"Empty value range for {layer_id}: [{vmin}, {vmax}]"
            raise ValueError(msg)
        luts = build_luts(style.cmap, vmin, vmax)
        src_bands = (1, 1, 1)
        cmap, colors = style.cmap, ramp_colors(style.cmap)

    with rasterio_open(src_vrt) as dataset:
        bounds = _wgs84_bounds(dataset)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        if materialize_rgba:
            rgba_path = Path(tmp) / f"{out_path.stem}_rgba.tif"
            with rasterio_open(src_vrt) as dataset:
                logger.info("Colour-mapping %s with %s", src_vrt.name, cmap or "band ranges")
                write_rgba(dataset, rgba_path, cmap=cmap, vmin=vmin, vmax=vmax)
        else:
            rgba_path = rgba_vrt(
                src_vrt,
                Path(tmp) / f"{out_path.stem}_rgba.vrt",
                luts=luts,
                src_bands=src_bands,
            )
        logger.info("Tiling %s to z%d-%d", out_path.name, min_zoom, max_zoom)
        run_rio_pmtiles(
            rgba_path,
            out_path,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            tile_format=tile_format,
            quality=quality,
            resampling=resampling,
            workers=workers,
            title=layer_id,
            attribution=attribution,
            sources_at=sources_at,
        )

    size = out_path.stat().st_size
    logger.info("Wrote %s (%.1f MB)", out_path.name, size / 1e6)
    product, polarization = _split_layer_id(layer_id)
    return LayerInfo(
        id=f"{layer_id}_{year}",
        product=product,
        polarization=polarization,
        year=year,
        url=out_path.name,
        bounds=bounds,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        cmap=cmap,
        vmin=vmin,
        vmax=vmax,
        units=style.units,
        source=str(src_vrt),
        size_bytes=size,
        colors=colors,
        start_date=start_date,
        end_date=end_date,
    )


def _split_layer_id(layer_id: str) -> tuple[str, str]:
    """Product and polarization of a layer id, for the manifest's grouping."""
    product, _, rest = layer_id.partition("_")
    return product.upper(), rest.upper()


def _pmtiles_source(
    layer_id: str,
    style: LayerStyle | RgbStyle,
    vrts: YearVrts,
) -> Path | UPath | None:
    """Select the VRT a layer is tiled from: linear, decibel, or false-colour."""
    if layer_id.endswith("_rgb"):
        return vrts.rgb.get(layer_id.removesuffix("_rgb"))
    if style.decibel:
        return vrts.decibel.get(layer_id)
    return vrts.linear.get(layer_id)


def build_layers(
    vrts: YearVrts,
    out_dir: Path,
    *,
    year: int,
    start_date: str,
    end_date: str,
    styles: Mapping[str, LayerStyle] | None = None,
    rgb_styles: Mapping[str, RgbStyle] | None = None,
    style_path: Path | None = None,
    layers: Collection[str] | None = None,
    min_zoom: int = 5,
    max_zoom: int = MOSAIC_ZOOM,
    tile_format: str = "WEBP",
    quality: int = 80,
    resampling: str = "average",
    workers: int | None = None,
    materialize_rgba: bool = False,
) -> list[LayerInfo]:
    """Build one PMTiles archive per layer of one year, from the VRT chain.

    Parameters
    ----------
    vrts
        Chain built by :func:`glace_catalog.mosaics.build_year_vrts`. Its
        paths are the inputs; nothing is globbed and no filename is parsed.
    out_dir
        Local directory the archives are written to.
    year, start_date, end_date
        The year being published and the composite window it covers.
    styles, rgb_styles
        Rendering per layer. Both default to the committed style at
        ``style_path``, which is where the published constants live (M-27).
    style_path
        The MapLibre style to read them from. Defaults to this repository's
        ``catalog/mosaics/styles/default.json``.
    layers
        Restrict to these layer ids. ``None`` builds all fourteen.
    min_zoom, max_zoom, tile_format, quality, resampling, workers,
    materialize_rgba
        Passed through to :func:`build_pmtiles`.

    Returns
    -------
    list[LayerInfo]
        Manifest entries for the archives written.
    """
    path = style_path or DEFAULT_STYLE_PATH
    attribution = load_attribution(path, year)
    if styles is None or rgb_styles is None:
        read_single, read_rgb = load_styles(path)
        styles = dict(read_single if styles is None else styles)
        rgb_styles = dict(read_rgb if rgb_styles is None else rgb_styles)
    else:
        styles, rgb_styles = dict(styles), dict(rgb_styles)
    wanted: dict[str, LayerStyle | RgbStyle] = {**styles, **rgb_styles}
    if layers is not None:
        wanted = {key: value for key, value in wanted.items() if key in layers}

    provisional = sorted(key for key, style in wanted.items() if not style.measured)
    if provisional:
        logger.warning(
            "Stretches not yet measured on the published archive: %s. They are fixed and "
            "comparable across years, but check them against rendered tiles before the "
            "archives are considered final.",
            ", ".join(provisional),
        )

    built: list[LayerInfo] = []
    # The VRTs are local but read remote tiles, so this process needs the object
    # store's credentials established around the reads; the tiler subprocess is
    # given its own configuration through `sources_at`.
    with chain_env(vrts):
        for layer_id, style in sorted(wanted.items()):
            source = _pmtiles_source(layer_id, style, vrts)
            if source is None:
                logger.info("No VRT for %s; skipping", layer_id)
                continue
            built.append(
                build_pmtiles(
                    source,
                    out_dir / f"{layer_id}.pmtiles",
                    layer_id=layer_id,
                    year=year,
                    style=style,
                    start_date=start_date,
                    end_date=end_date,
                    min_zoom=min_zoom,
                    max_zoom=max_zoom,
                    tile_format=tile_format,
                    quality=quality,
                    resampling=resampling,
                    workers=workers,
                    materialize_rgba=materialize_rgba,
                    sources_at=vrts.sources_at,
                    attribution=attribution,
                ),
            )
    return built


INVENTORY_OVERLAYS: dict[str, dict[str, Any]] = {
    "pauletal2020": {
        "title": "Alpine Glacier Inventory",
        "color": "#ff2d95",
        "name_column": None,
        "year_column": "date",
        # The inventory ships no names, so its own identifier is what a reader
        # can look a feature up by.
        "extra_columns": {"glacier_nr": "glacier_nr"},
    },
    "sgi2016": {
        "title": None,  # taken from the dataset's own metadata
        "color": "#22d3ee",
        "name_column": "name",
        "year_column": "year_acq",
    },
    "sgi2023": {
        "title": None,
        "color": "#facc15",
        "name_column": "name",
        "year_column": "year_acq",
    },
}
"""How each glacier inventory is drawn as a vector overlay, keyed by dataset.

Not part of the catalog: the inventories are third-party datasets a viewer shows
*beside* GLACE, and this is the presentation each one is given. It travels with
:func:`export_inventory` because that is what consumes it.
"""


def build_vector_pmtiles(
    geojson_path: Path,
    out_path: Path,
    *,
    layer: str,
    min_zoom: int = 4,
    max_zoom: int = 14,
    tippecanoe: str = "tippecanoe",
    extra_args: Sequence[str] = (),
) -> int:
    """Convert a GeoJSON overlay to a vector PMTiles archive with tippecanoe.

    The archive is larger on disk than the gzipped GeoJSON -- the pyramid stores
    each geometry once per zoom level -- but a view only pulls the tiles it
    covers, so the opening view transfers roughly a sixteenth as much and nothing
    is parsed up front.

    Parameters
    ----------
    geojson_path
        Overlay written by :func:`export_inventory`.
    out_path
        Destination ``.pmtiles`` archive; overwritten if it exists.
    layer
        Name of the layer inside the archive, which is what the viewer's
        ``source-layer`` must match.
    min_zoom, max_zoom
        Zoom range to build. Lowering ``max_zoom`` is the main lever on size.
    tippecanoe
        Path to the tippecanoe binary.
    extra_args
        Further tippecanoe flags, appended before the input path.

    Returns
    -------
    int
        Size of the written archive in bytes.

    Raises
    ------
    FileNotFoundError
        If the tippecanoe binary cannot be found.
    """
    if shutil.which(tippecanoe) is None and not Path(tippecanoe).is_file():
        msg = (
            f"tippecanoe not found at {tippecanoe!r}. Run under "
            "`uv run --with tippecanoe` for the pre-built wheel, build it from "
            "https://github.com/felt/tippecanoe, or pass --tippecanoe."
        )
        raise FileNotFoundError(msg)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        tippecanoe,
        "-o",
        str(out_path),
        "-Z",
        str(min_zoom),
        "-z",
        str(max_zoom),
        "-l",
        layer,
        "--force",
        "--quiet",
        *extra_args,
        str(geojson_path),
    ]
    logger.debug("running %s", " ".join(command))
    subprocess.run(command, check=True)  # noqa: S603  # our own argv, no shell
    size = out_path.stat().st_size
    logger.info("Wrote %s: %.1f MB (z%d-%d)", out_path.name, size / 1e6, min_zoom, max_zoom)
    return size


def export_inventory(
    frame: gpd.GeoDataFrame,
    out_path: Path,
    *,
    name_column: str | None,
    year_column: str,
    extra_columns: Mapping[str, str] | None = None,
    simplify: float = 10.0,
    precision: int = 5,
) -> tuple[int, int, int]:
    """Write a glacier inventory as a trimmed, simplified GeoJSON overlay.

    Each feature keeps ``name`` and ``year`` plus whatever ``extra_columns``
    names; everything else is dropped, since the overlay exists only to be
    looked at. Geometries
    are simplified in a metric CRS and coordinates rounded. The default 10 m
    tolerance is finer than the 26.5 m the raster layers resolve at their
    maximum zoom, so the outlines stay crisper than the imagery underneath.

    Parameters
    ----------
    frame
        Inventory as a :class:`geopandas.GeoDataFrame`.
    out_path
        Destination ``.geojson`` file.
    name_column
        Column holding the glacier name, or ``None`` for an inventory that
        ships none, in which case ``name`` is written as null.
    year_column
        Column holding the acquisition year or date; dates are reduced to
        their year.
    extra_columns
        Further attributes to carry through, as ``{property: column}``. Missing
        columns are written as null rather than raising, so an inventory that
        lacks one still exports.
    simplify
        Douglas-Peucker tolerance in metres, applied in EPSG:3035. Zero keeps
        the full geometry.
    precision
        Decimal places kept in the output coordinates; 5 is about a metre.

    Returns
    -------
    tuple[int, int, int]
        Feature count, and the first and last acquisition year present.
    """
    from pandas.api.types import is_datetime64_any_dtype  # noqa: PLC0415

    export = frame.copy()
    years = export[year_column]
    export["year"] = (years.dt.year if is_datetime64_any_dtype(years) else years).astype("int32")
    if name_column is None:
        export["name"] = None
    else:
        export["name"] = export[name_column].astype("object").where(export[name_column].notna())

    kept = ["name", "year"]
    for prop, column in (extra_columns or {}).items():
        export[prop] = export[column].where(export[column].notna()) if column in export else None
        kept.append(prop)
    export = export[[*kept, "geometry"]]
    if simplify > 0:
        export["geometry"] = (
            export.to_crs("EPSG:3035").geometry.simplify(simplify).to_crs("EPSG:4326")
        )
    else:
        export = export.to_crs("EPSG:4326")

    payload = json.loads(export.to_json(drop_id=True))
    for feature in payload["features"]:
        feature["geometry"]["coordinates"] = _round_coords(
            feature["geometry"]["coordinates"],
            precision,
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, separators=(",", ":")))
    # Debug, not info: for the web map this file is a staging step on the way to
    # a PMTiles archive and is deleted straight after, so announcing it as
    # written is misleading.
    logger.debug(
        "Wrote %s: %d features, %.1f MB",
        out_path.name,
        len(export),
        out_path.stat().st_size / 1e6,
    )
    return len(export), int(export["year"].min()), int(export["year"].max())


def _round_coords(coords: Any, precision: int) -> Any:
    """Round a GeoJSON coordinate tree to ``precision`` decimals."""
    if coords and isinstance(coords[0], (int, float)):
        return [round(coords[0], precision), round(coords[1], precision)]
    return [_round_coords(part, precision) for part in coords]


def export_tile_grid(
    items_parquet: Path | UPath,
    out_path: Path,
    *,
    simplify: float = 1e-3,
    max_zoom: int = 10,
    tippecanoe: str = "tippecanoe",
) -> Path:
    """Export the catalog's MGRS tile footprints as a vector PMTiles overlay.

    Reads the stac-geoparquet item table, dedupes it to one polygon per MGRS
    tile and writes an archive holding the tile code, its glacier fraction and
    the number of catalogued years, under the layer name
    :data:`TILE_GRID_LAYER`.

    Parameters
    ----------
    items_parquet
        stac-geoparquet export of the composite collection.
    out_path
        Destination ``.pmtiles`` file.
    simplify
        Douglas-Peucker tolerance in degrees applied to the footprints.
    max_zoom
        Highest zoom to build. The footprints are 110 km squares, so a low
        maximum is plenty -- MapLibre overzooms a vector source past it, for
        rendering and for hit-testing alike.
    tippecanoe
        Path to the tippecanoe binary.
    """
    import geopandas as gpd  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    columns = ["geometry", "glace:mgrs_tile", "glace:glacier_fraction", "datetime"]
    with items_parquet.open("rb") as handle:
        table = pq.read_table(handle, columns=columns)
    frame = gpd.GeoDataFrame.from_arrow(table).rename(
        columns={"glace:mgrs_tile": "tile", "glace:glacier_fraction": "glacier_fraction"},
    )
    # The index this reads is the tiles collection's, so every row must name a
    # tile. Dropping the null ones and then checking the count is what turns a
    # mosaic row leaking in from a silent `drop_duplicates` no-op into a failure.
    tiled = frame.dropna(subset=["tile"])
    years = tiled.groupby("tile")["datetime"].nunique().rename("years")
    grid = (
        tiled.drop_duplicates("tile")
        .set_index("tile")[["geometry", "glacier_fraction"]]
        .join(years)
        .reset_index()
    )
    if len(grid) != tiled["tile"].nunique():
        msg = (
            f"{items_parquet} yielded {len(grid)} footprints for "
            f"{tiled['tile'].nunique()} distinct tiles."
        )
        raise ValueError(msg)
    grid["geometry"] = grid.geometry.simplify(simplify)
    grid = gpd.GeoDataFrame(grid, geometry="geometry", crs="EPSG:4326")
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "tile-grid.geojson"
        grid.to_file(staged, driver="GeoJSON")
        # -r1 disables tippecanoe's density-based feature dropping: the grid is a
        # complete index of the catalog, so a footprint missing at low zoom would
        # read as a gap in coverage rather than as decluttering. All 2209 survive
        # from z2 up; at z0 the whole world is one tile and 110 km squares fall
        # below its coordinate resolution, which no flag can help.
        build_vector_pmtiles(
            staged,
            out_path,
            layer=TILE_GRID_LAYER,
            min_zoom=0,
            max_zoom=max_zoom,
            tippecanoe=tippecanoe,
            extra_args=("-r1",),
        )
    logger.info("Wrote %s with %d tile footprints", out_path, len(grid))
    return out_path
