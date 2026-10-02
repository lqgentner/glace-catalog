"""Web-map tiling of the annual mosaics into PMTiles archives.

Encodes a display-grid mosaic into 8-bit value codes over the layer's fixed
transport range (:mod:`glace_catalog.encoding`) and packs them into one PMTiles
archive per layer, named ``{layer}_viz.pmtiles`` so it sits beside the COG it
renders without sharing its stem. No colour is baked in: the archive's header
and the year item's ``visual`` asset declare the decoding, and the committed
MapLibre style decodes and colours it. ``publish-year`` writes the asset from
the record this module returns.

The source is the **VRT chain's canonical-grid mosaic**, which already sits on
the WebMercatorQuad grid at the maximum zoom. That retires the double resampling
of the old 40 m EPSG:3035 overview: at max zoom the tiler's warp is the identity,
and every level below it is a clean 2x decimation, which is why the resampling
kernel here is ``average`` rather than the ``bilinear`` an oversampled source
needed.

**The warp happens once per layer.** The encoded RGBA VRT over the chain
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
from dataclasses import dataclass
import logging
import math
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

from freezebase.raster import rasterio_open
from freezebase.vrt import create_rgba_vrt
from rasterio.enums import Resampling
import rasterio.shutil
from rasterio.warp import transform_bounds

from glace_catalog.metadata import vocabulary
from glace_catalog.mosaics import chain_env
from glace_catalog.scopes import MOSAIC_ZOOM, TILE_PX, VIZ_MIN_ZOOM
from glace_catalog.stac.naming import ASSET_KEYS, viz_filename
from glace_catalog.stac.store import multihash_sha256_file
from glace_catalog.styles import ATTRIBUTION
from glace_catalog.tiler import TileSpec, spawn_pool, write_archive

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from concurrent.futures import ProcessPoolExecutor

    from upath import UPath

    from glace_catalog.mosaics import YearVrts
    from glace_catalog.styles import LayerStyle

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

TILE_FORMAT = "WEBP"
"""Tile image format of every archive."""

WEBP_QUALITY = 90
"""WebP quality of every archive.

Lossless value tiles were measured at 1.7-2.1x the colour-mapped archives for
speckled SAR; lossy grey tiles at 90 are smaller than those archives and carry
no more value error than the colour mapping did.
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
    reader sees instead is the archive's own header, the year item's
    ``visual`` asset, and the year style.
    """

    id: str
    product: str
    polarization: str
    year: int
    url: str
    bounds: list[float]
    min_zoom: int
    max_zoom: int
    scale: float
    offset: float
    units: str
    source: str
    size_bytes: int
    start_date: str
    end_date: str
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


def _wgs84_bounds(dataset: rasterio.DatasetReader) -> list[float]:
    """Geographic bounding box of a dataset, as ``[west, south, east, north]``."""
    west, south, east, north = transform_bounds(dataset.crs, "EPSG:4326", *dataset.bounds)
    return [west, south, east, north]


def _centre_latitude(dataset: rasterio.DatasetReader) -> float:
    """Latitude of the dataset's centre, in degrees."""
    _, south, _, north = _wgs84_bounds(dataset)
    return (south + north) / 2.0


def rgba_vrt(
    src_vrt: Path | UPath,
    out_vrt: Path,
    *,
    luts: Sequence[Sequence[tuple[float, int]]],
) -> Path:
    """Write the tiler's direct input: a 4-band uint8 RGBA VRT over ``src_vrt``."""
    create_rgba_vrt(src_vrt, out_vrt, luts=luts, src_bands=(1, 1, 1))
    return out_vrt


def rgba_raster(
    src_vrt: Path | UPath,
    out_path: Path,
    *,
    luts: Sequence[Sequence[tuple[float, int]]],
) -> Path:
    """Warp and encode one layer into a local RGBA GeoTIFF, once.

    The lookup-table VRT of :func:`rgba_vrt` is written beside ``out_path`` and
    copied through GDAL's ``CreateCopy`` — what ``gdal_translate`` does — so the
    whole chain below it runs exactly once, as one streaming pass. This process
    must be inside the chain's environment (:func:`~glace_catalog.mosaics.chain_env`)
    when the sources are remote.

    Parameters
    ----------
    src_vrt
        A VRT from the chain: linear or decibel.
    out_path
        The GeoTIFF written, on the tiler's grid, four uint8 bands with alpha.
    luts
        The encoding, as :func:`rgba_vrt` takes it.

    Returns
    -------
    pathlib.Path
        ``out_path``.
    """
    lut_vrt = rgba_vrt(src_vrt, out_path.with_suffix(".vrt"), luts=luts)
    with rasterio_open(lut_vrt) as src:
        rasterio.shutil.copy(src, str(out_path), **RGBA_CREATION)
    return out_path


def build_pmtiles(
    src_vrt: Path | UPath,
    out_path: Path,
    *,
    layer_id: str,
    year: int,
    style: LayerStyle,
    start_date: str,
    end_date: str,
    min_zoom: int = VIZ_MIN_ZOOM,
    max_zoom: int = MOSAIC_ZOOM,
    workers: int | None = None,
    attribution: str = "",
    scratch: Path | None = None,
    pool: ProcessPoolExecutor | None = None,
) -> LayerInfo:
    """Encode, warp once and tile one display-grid mosaic into a PMTiles archive.

    Parameters
    ----------
    src_vrt
        A VRT from the chain: the linear mosaic or its decibel reading.
    out_path
        Destination ``.pmtiles`` file. Must be local — packing an archive needs
        random writes.
    layer_id
        Identifier of the layer. The archive is named ``viz_filename(layer_id)``,
        so a reader derives the data stem by stripping the suffix.
    year
        Composite year, for the manifest entry.
    style
        The layer's display record; its transport range is what the codes
        span. Fixed per layer, so that a year slider compares like with like.
    start_date, end_date
        Composite window the layer covers.
    min_zoom, max_zoom
        Zoom range of the pyramid. The default maximum is the zoom the source
        already sits on, so that level is a copy and every level below it a 2x
        decimation, which is why the tiles are warped with ``average``.
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
        If the zoom range is inverted.
    """
    if min_zoom > max_zoom:
        msg = f"min_zoom {min_zoom} exceeds max_zoom {max_zoom}"
        raise ValueError(msg)
    encoding = style.encoding

    with rasterio_open(src_vrt) as dataset:
        bounds = _wgs84_bounds(dataset)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if scratch is not None:
        scratch.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(dir=scratch, prefix=f"{layer_id}-") as tmp,
        nullcontext(pool) if pool is not None else spawn_pool(workers) as executor,
    ):
        logger.info("Warping %s once, encoded over %s", layer_id, encoding)
        rgba_path = rgba_raster(src_vrt, Path(tmp) / f"{layer_id}_rgba.tif", luts=encoding.luts())
        logger.info("Tiling %s to z%d-%d", out_path.name, min_zoom, max_zoom)
        written = write_archive(
            rgba_path,
            out_path,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            spec=TileSpec(
                max_zoom=max_zoom,
                tile_size=TILE_SIZE,
                image_format=TILE_FORMAT,
                quality=WEBP_QUALITY,
                resampling=Resampling.average,
            ),
            name=layer_id,
            attribution=attribution,
            # The decoding, as a raster-dem source declares it, the way a
            # terrain archive's metadata names its encoding.
            extra_metadata={**encoding.maplibre(), "unit": style.units},
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
        scale=encoding.scale,
        offset=encoding.offset,
        units=style.units,
        source=str(src_vrt),
        size_bytes=size,
        checksum=checksum,
        start_date=start_date,
        end_date=end_date,
    )


def _split_layer_id(layer_id: str) -> tuple[str, str]:
    """Product and polarization of a layer id, for the manifest's grouping."""
    product, _, rest = layer_id.partition("_")
    return product.upper(), rest.upper()


def _pmtiles_source(
    layer_id: str,
    style: LayerStyle,
    vrts: YearVrts,
) -> Path | UPath | None:
    """Select the VRT a layer is tiled from: linear or decibel."""
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
    layers: Collection[str] | None = None,
    min_zoom: int = VIZ_MIN_ZOOM,
    max_zoom: int = MOSAIC_ZOOM,
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
    styles
        Display record per layer, whose transport range the archive is
        encoded over. Defaults to the vocabulary's, from
        ``metadata/assets.yaml``.
    layers
        Restrict to these layer ids. ``None`` builds all twelve.
    min_zoom, max_zoom
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
    attribution = ATTRIBUTION.format(year=year)
    if styles is None:
        styles = {key: vocabulary().display(key) for key in ASSET_KEYS}
    wanted = {key: style for key, style in styles.items() if layers is None or key in layers}

    def build_one(layer_id: str, style: LayerStyle, source: Path | UPath) -> LayerInfo:
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
