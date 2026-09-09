"""Web-map tiling of the annual mosaics into PMTiles archives.

Colour-maps a display-grid mosaic into RGBA and packs it into one PMTiles
archive per layer, named ``{layer}_viz.pmtiles`` so it sits beside the COG it
renders without sharing its stem. Nothing is written beside them: what a reader
needs in order to draw a legend lives in the committed MapLibre style (M-27),
and what it needs in order to find an archive is a ``rel="pmtiles"`` link on the
mosaics collection (M-26) and an asset with the ``visual`` role on the year
item, which ``publish-year`` writes from the record this module returns. The
``layers.json`` manifest both used to travel in is retired.

The source is the **VRT chain's canonical-grid mosaic**, which already sits on
the WebMercatorQuad grid at the maximum zoom. That retires the double resampling
of the old 40 m EPSG:3035 overview: at max zoom the tiler's warp is the identity,
and every level below it is a clean 2x decimation, which is why the resampling
kernel here is ``average`` rather than the ``bilinear`` an oversampled source
needed.

**The warp happens once per layer.** The colour-mapped RGBA VRT over the chain
is copied to a local tiled GeoTIFF before anything is tiled, and the tiler
(:mod:`glace_catalog.tiler`) reads that file. Tiling straight from the VRT was
measured at 174 s per Swiss layer against 10 s from the local copy: every tile
of every zoom level warped the 1 716 remote UTM composites over its own ground
again, nine zoom levels deep, so the object store was read nine times per
archive. The local copy is written **without overviews**: the levels below z13
are decimated out of the full-resolution copy, which costs a few seconds per
archive and keeps the intermediate a third smaller — and disk is the constraint
at Alpine scale, where the fourteen copies of one year are 15 GB.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
import logging
import math
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

from freezebase.raster import rasterio_open
from freezebase.vrt import create_rgba_vrt
import numpy as np
import rasterio
from rasterio.enums import Resampling
import rasterio.shutil
from rasterio.warp import transform_bounds

from glace_catalog.mosaics import chain_env
from glace_catalog.scopes import MOSAIC_ZOOM, TILE_PX
from glace_catalog.stac.naming import viz_filename
from glace_catalog.stac.store import multihash_sha256_file
from glace_catalog.styles import (
    DEFAULT_STYLE_PATH,
    LayerStyle,
    RgbStyle,
    load_attribution,
    load_styles,
    ramp_colors,
    resolve_cmap,
)
from glace_catalog.tiler import TileSpec, spawn_pool, write_archive

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from concurrent.futures import ProcessPoolExecutor

    from upath import UPath

    from glace_catalog.mosaics import YearVrts

logger = logging.getLogger(__name__)

WEB_MERCATOR = "EPSG:3857"
# Full width of the WebMercatorQuad tile matrix, in metres.
MERCATOR_SPAN_M = 2 * 20037508.342789244
TILE_SIZE = TILE_PX

RGBA_CREATION = {
    "driver": "GTiff",
    "TILED": "YES",
    "BLOCKXSIZE": TILE_PX,
    "BLOCKYSIZE": TILE_PX,
    "COMPRESS": "DEFLATE",
    "ZLEVEL": 6,
    "BIGTIFF": "IF_SAFER",
}
"""Creation options of the warped-once RGBA intermediate.

256 px blocks so a max-zoom tile is one block read; DEFLATE because the file is
written once and read once. No overviews: see the module docstring.
"""

DEFAULT_CONCURRENT_LAYERS = 4
"""Layers warped at once by :func:`build_layers`, absent a better-informed choice.

Four Alpine intermediates are under 5 GB of scratch. The tiling spike measured
the Swiss year at 5.5 min with all fourteen layers warping at once; at four the
warps run in four rounds, which projects to 9-10 min. Raise it where the disk
has room.
"""


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
    checksum: str = ""


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


def rgba_raster(
    src_vrt: Path | UPath,
    out_path: Path,
    *,
    luts: Sequence[Sequence[tuple[float, int]]],
    src_bands: Sequence[int] = (1, 1, 1),
) -> Path:
    """Warp and colour-map one layer into a local RGBA GeoTIFF, once.

    The lookup-table VRT of :func:`rgba_vrt` is written beside ``out_path`` and
    copied through GDAL's ``CreateCopy`` — what ``gdal_translate`` does — so the
    whole chain below it runs exactly once, as one streaming pass. This process
    must be inside the chain's environment (:func:`~glace_catalog.mosaics.chain_env`)
    when the sources are remote.

    Parameters
    ----------
    src_vrt
        A VRT from the chain: linear, decibel or false-colour.
    out_path
        The GeoTIFF written, on the tiler's grid, four uint8 bands with alpha.
    luts, src_bands
        The colour mapping, as :func:`rgba_vrt` takes it.

    Returns
    -------
    pathlib.Path
        ``out_path``.
    """
    lut_vrt = rgba_vrt(
        src_vrt,
        out_path.with_suffix(".vrt"),
        luts=luts,
        src_bands=src_bands,
    )
    with rasterio_open(lut_vrt) as src:
        rasterio.shutil.copy(src, str(out_path), **RGBA_CREATION)
    return out_path


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
    attribution: str = "",
    scratch: Path | None = None,
    pool: ProcessPoolExecutor | None = None,
) -> LayerInfo:
    """Colour-map, warp once and tile one display-grid mosaic into a PMTiles archive.

    Parameters
    ----------
    src_vrt
        A VRT from the chain: the linear mosaic, its decibel reading, or a
        false-colour composition of the two polarizations.
    out_path
        Destination ``.pmtiles`` file. Must be local — packing an archive needs
        random writes.
    layer_id
        Identifier of the layer. The archive is named ``viz_filename(layer_id)``,
        so a reader derives the data stem by stripping the suffix.
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
    tile_format, quality, resampling
        Tile encoding and warp kernel. ``average`` is right here because the
        source is on the target grid at max zoom; the earlier bilinear-versus-
        nearest question was about oversampling a 40 m source and no longer
        applies.
    workers
        Worker processes of the pool started for this call, one per core by
        default. Ignored when ``pool`` is given.
    attribution
        Archive attribution, as a viewer displays it.
    scratch
        Directory the warped-once RGBA raster is written under, in a temporary
        subdirectory removed on return. Defaults to the system temporary
        directory; at Alpine scale the raster is about 1.1 GB, so point this at
        a volume that has room for it.
    pool
        A running :func:`~glace_catalog.tiler.spawn_pool` to render the tiles
        in, so several archives can share one.

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
    if scratch is not None:
        scratch.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(dir=scratch, prefix=f"{layer_id}-") as tmp,
        nullcontext(pool) if pool is not None else spawn_pool(workers) as executor,
    ):
        logger.info("Warping %s once, colour-mapped with %s", layer_id, cmap or "band ranges")
        rgba_path = rgba_raster(
            src_vrt,
            Path(tmp) / f"{layer_id}_rgba.tif",
            luts=luts,
            src_bands=src_bands,
        )
        logger.info("Tiling %s to z%d-%d", out_path.name, min_zoom, max_zoom)
        written = write_archive(
            rgba_path,
            out_path,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            spec=TileSpec(
                max_zoom=max_zoom,
                tile_size=TILE_SIZE,
                image_format=tile_format,
                quality=quality,
                resampling=Resampling[resampling],
            ),
            name=layer_id,
            attribution=attribution,
            pool=executor,
        )

    size = out_path.stat().st_size
    checksum = multihash_sha256_file(out_path)
    logger.info("Wrote %s: %d tiles, %.1f MB", out_path.name, written, size / 1e6)
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
        checksum=checksum,
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
    concurrent_layers: int = DEFAULT_CONCURRENT_LAYERS,
    scratch: Path | None = None,
) -> list[LayerInfo]:
    """Build one PMTiles archive per layer of one year, from the VRT chain.

    The layers are pipelined: ``concurrent_layers`` of them warp at once, each
    in its own thread, and every warped raster is tiled in one shared worker
    pool as soon as it is ready.
    The warp is single-threaded inside GDAL and bound by the object store, so
    running several is what keeps the cores busy; the tiler scales linearly
    with its workers once the source is local.

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
    min_zoom, max_zoom, tile_format, quality, resampling
        Passed through to :func:`build_pmtiles`.
    workers
        Worker processes of the shared tiling pool, one per core by default.
    concurrent_layers
        Layers warped at the same time. This bounds the scratch disk: each
        holds one RGBA raster of the whole scope while it is being written and
        tiled — about 100 MB for Switzerland, about 1.1 GB for the Alps.
    scratch
        Where those rasters are written; the system temporary directory by
        default.

    Returns
    -------
    list[LayerInfo]
        Manifest entries for the archives written, in layer order.
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

    def build_one(layer_id: str, style: LayerStyle | RgbStyle, source: Path | UPath) -> LayerInfo:
        return build_pmtiles(
            source,
            out_dir / viz_filename(layer_id),
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
            attribution=attribution,
            scratch=scratch,
            pool=pool,
        )

    sources = {
        layer_id: (style, source)
        for layer_id, style in sorted(wanted.items())
        if (source := _pmtiles_source(layer_id, style, vrts)) is not None
    }
    for layer_id in sorted(set(wanted) - set(sources)):
        logger.info("No VRT for %s; skipping", layer_id)

    # The VRTs are local but read remote tiles, so the store's credentials are
    # established here, in the main thread, around every warp. That placement
    # is load-bearing: rasterio sets a main thread's options process-wide and
    # any other thread's thread-locally, where GDAL's own reader threads (a
    # VRT reads its sources in parallel) cannot see them — a warp that enters
    # the environment inside its worker thread opens the first sources and
    # then fails with "No valid AWS credentials". The tiler pool reads the
    # local raster and needs no environment at all.
    with (
        chain_env(vrts),
        spawn_pool(workers) as pool,
        ThreadPoolExecutor(max_workers=max(1, concurrent_layers)) as warps,
    ):
        futures = {
            layer_id: warps.submit(build_one, layer_id, style, source)
            for layer_id, (style, source) in sources.items()
        }
        return [futures[layer_id].result() for layer_id in sources]
