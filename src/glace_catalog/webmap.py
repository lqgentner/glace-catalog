"""Tile the annual display mosaics into PMTiles web-map archives.

Each layer's display-grid mosaic is encoded into 8-bit value codes over the
layer's fixed transport range (:mod:`glace_catalog.encoding`) and tiled into
one archive, ``{layer}_viz.pmtiles``, beside the COG it shows. The archive
header and the year item's ``visual`` asset declare how to decode the codes,
and the MapLibre style decodes and colours them. ``build-year`` writes the
item asset from the :class:`LayerInfo` this module returns.

The source is the VRT chain's display mosaic, which is already on the
WebMercatorQuad grid at the maximum zoom. The top zoom level is therefore cut
without resampling, and each lower level halves the resolution, so tiles are
resampled with ``average``.

Each layer is warped once: the encoded RGBA VRT is copied to a local tiled
GeoTIFF, and the tiler (:mod:`glace_catalog.tiler`) reads that file, so the
remote composites are read once per layer. The local copy has no overviews,
which keeps it small on scratch disk; lower zoom levels are computed from full
resolution.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

from freezebase.raster import rasterio_open
from freezebase.vrt import create_decibel_vrt, create_rgba_vrt
from rasterio.enums import Resampling
import rasterio.shutil
from rasterio.warp import transform_bounds

from glace_catalog.metadata import vocabulary
from glace_catalog.mosaics import sources_env
from glace_catalog.scopes import MOSAIC_ZOOM, TILE_PX, VIZ_MIN_ZOOM
from glace_catalog.stac.naming import ASSET_KEYS, data_key, viz_filename
from glace_catalog.stac.store import multihash_sha256_file
from glace_catalog.styles import ATTRIBUTION
from glace_catalog.tiler import TileSpec, spawn_pool, write_archive

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from concurrent.futures import ProcessPoolExecutor
    from typing import Any

    from upath import UPath

    from glace_catalog.mosaics import YearVrts
    from glace_catalog.styles import LayerStyle

logger = logging.getLogger(__name__)

RGBA_CREATION = {
    "driver": "GTiff",
    "TILED": "YES",
    "BLOCKXSIZE": TILE_PX,
    "BLOCKYSIZE": TILE_PX,
    "COMPRESS": "DEFLATE",
    "ZLEVEL": 6,
    "BIGTIFF": "IF_SAFER",
}
"""Creation options of the local RGBA copy each layer is warped into.

With 256 px blocks, a max-zoom tile is one block read. DEFLATE suits a file
that is written once and read once. The file has no overviews (see the module
docstring).
"""

TILE_FORMAT = "WEBP"
"""Tile image format of every archive."""

WEBP_QUALITY = 90
"""WebP quality of every archive.

For speckled SAR data, lossy tiles at this quality are less than half the size
of lossless ones, with small value errors.
"""

DEFAULT_CONCURRENT_LAYERS = 4
"""Default number of layers :func:`build_layers` warps at once.

Four Alpine RGBA rasters take under 5 GB of scratch disk. More concurrent
layers finish a year faster; raise the number when the disk has room.
"""


@dataclass
class LayerInfo:
    """One built PMTiles archive, as ``build-year`` records it in the build state.

    The year item's ``visual`` asset is written from it, and finalization
    checks the encoding against ``metadata/assets.yaml``.
    """

    url: str
    bounds: list[float]
    min_zoom: int
    max_zoom: int
    scale: float
    offset: float
    size_bytes: int
    checksum: str


def encoding_mismatch(key: str, detail: Mapping[str, Any]) -> str | None:
    """Describe an archive whose encoding differs from the metadata; ``None`` if it matches.

    ``key`` names the archive as the build state does, e.g. ``coh12_vv_viz``,
    and ``detail`` is its build-state record, which holds the scale and offset
    the tiler used. The styles and the item assets decode with the ones from
    ``metadata/assets.yaml``, so a difference means the archive predates a
    change of its transport range.
    """
    encoding = vocabulary().display(data_key(key)).encoding
    built = (float(detail["scale"]), float(detail["offset"]))
    expected = (encoding.scale, encoding.offset)
    if built == expected:
        return None
    return (
        f"{key}: the archive was encoded with scale and offset {built}, "
        f"but metadata/assets.yaml says {expected}"
    )


def _wgs84_bounds(dataset: rasterio.DatasetReader) -> list[float]:
    """Geographic bounding box of a dataset, as ``[west, south, east, north]``."""
    west, south, east, north = transform_bounds(dataset.crs, "EPSG:4326", *dataset.bounds)
    return [west, south, east, north]


def rgba_raster(
    src_vrt: Path | UPath,
    out_path: Path,
    *,
    luts: Sequence[Sequence[tuple[float, int]]],
) -> Path:
    """Warp and encode one layer into a local RGBA GeoTIFF.

    A 4-band uint8 RGBA lookup-table VRT is written beside ``out_path`` and
    copied with GDAL's ``CreateCopy`` (as ``gdal_translate`` does), so the
    whole VRT chain runs once, in one streaming pass. When the sources are
    remote, call this inside :func:`~glace_catalog.mosaics.sources_env`.

    Parameters
    ----------
    src_vrt
        A VRT from the chain: linear or decibel.
    out_path
        The GeoTIFF written, on the tiler's grid, four uint8 bands with alpha.
    luts
        The encoding, as :func:`freezebase.vrt.create_rgba_vrt` takes it.

    Returns
    -------
    pathlib.Path
        ``out_path``.
    """
    lut_vrt = out_path.with_suffix(".vrt")
    create_rgba_vrt(src_vrt, lut_vrt, luts=luts, src_bands=(1, 1, 1))
    with rasterio_open(lut_vrt) as src:
        rasterio.shutil.copy(src, str(out_path), **RGBA_CREATION)
    return out_path


def build_pmtiles(
    src_vrt: Path | UPath,
    out_path: Path,
    *,
    layer_id: str,
    style: LayerStyle,
    min_zoom: int = VIZ_MIN_ZOOM,
    max_zoom: int = MOSAIC_ZOOM,
    attribution: str = "",
    scratch: Path | None = None,
    bounds: Sequence[float] | None = None,
    pool: ProcessPoolExecutor,
) -> LayerInfo:
    """Encode, warp and tile one display-grid mosaic into a PMTiles archive.

    Parameters
    ----------
    src_vrt
        A VRT from the chain: the linear mosaic or its decibel reading.
    out_path
        Destination ``.pmtiles`` file. Must be local, because writing an
        archive needs random access.
    layer_id
        Identifier of the layer. The archive is named ``viz_filename(layer_id)``,
        so a reader derives the data stem by stripping the suffix.
    style
        The layer's display record. The codes span its transport range, which
        is the same for every year, so years are directly comparable.
    min_zoom, max_zoom
        Zoom range of the pyramid. The default maximum is the source's own
        zoom, so that level is copied unchanged and each lower level halves
        the resolution; tiles are therefore resampled with ``average``.
    attribution
        Archive attribution, as a viewer displays it.
    scratch
        Directory for the local RGBA raster, written to a temporary
        subdirectory that is removed on return. Defaults to the system
        temporary directory. At Alpine scale the raster is about 1.1 GB, so
        choose a volume with room for it.
    bounds
        Extent of the data, ``[west, south, east, north]`` in WGS84, which
        the archive's header declares. Defaults to the VRT's extent.
    pool
        A running :func:`~glace_catalog.tiler.spawn_pool` to render the tiles
        in, so several archives can share one.

    Returns
    -------
    LayerInfo
        Manifest entry describing the archive that was written.
    """
    encoding = style.encoding

    if bounds is None:
        with rasterio_open(src_vrt) as dataset:
            bounds = _wgs84_bounds(dataset)
    bounds = list(bounds)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if scratch is not None:
        scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch, prefix=f"{layer_id}-") as tmp:
        logger.info("Warping %s once, encoded over %s", layer_id, encoding)
        rgba_path = rgba_raster(src_vrt, Path(tmp) / f"{layer_id}_rgba.tif", luts=encoding.luts())
        logger.info("Tiling %s to z%d-%d", out_path.name, min_zoom, max_zoom)
        written = write_archive(
            rgba_path,
            out_path,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            spec=TileSpec(
                tile_size=TILE_PX,
                image_format=TILE_FORMAT,
                quality=WEBP_QUALITY,
                resampling=Resampling.average,
            ),
            name=layer_id,
            attribution=attribution,
            # Record the raster-dem decoding parameters, as terrain archives do.
            extra_metadata={**encoding.maplibre(), "unit": style.units},
            bounds=bounds,
            pool=pool,
        )

    size = out_path.stat().st_size
    checksum = multihash_sha256_file(out_path)
    logger.info("Wrote %s: %d tiles, %.1f MB", out_path.name, written, size / 1e6)
    return LayerInfo(
        url=out_path.name,
        bounds=bounds,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        scale=encoding.scale,
        offset=encoding.offset,
        size_bytes=size,
        checksum=checksum,
    )


def build_layers(
    vrts: YearVrts,
    out_dir: Path,
    *,
    year: int,
    styles: Mapping[str, LayerStyle] | None = None,
    layers: Collection[str] | None = None,
    min_zoom: int = VIZ_MIN_ZOOM,
    max_zoom: int = MOSAIC_ZOOM,
    workers: int | None = None,
    concurrent_layers: int = DEFAULT_CONCURRENT_LAYERS,
    scratch: Path | None = None,
    bounds: Sequence[float] | None = None,
) -> list[LayerInfo]:
    """Build one PMTiles archive per layer of one year, from the VRT chain.

    The layers are pipelined: ``concurrent_layers`` of them warp at once, each
    in its own thread, and each warped raster is tiled in one shared worker
    pool as soon as it is ready. A warp is single-threaded and waits on the
    object store, so running several keeps the cores busy. Tiling a local
    raster scales with the number of workers.

    Parameters
    ----------
    vrts
        Chain built by :func:`glace_catalog.mosaics.build_year_vrts`. Its
        paths are the inputs.
    out_dir
        Local directory the archives are written to.
    year
        The year being built, named in each archive's attribution.
    styles
        Display record per layer, whose transport range the archive is
        encoded over. A layer whose record is in decibels is tiled from a
        decibel VRT over its linear mosaic, written beside it. Defaults to the
        vocabulary's, from ``metadata/assets.yaml``.
    layers
        Restrict to these layer ids. ``None`` builds all twelve.
    min_zoom, max_zoom
        Passed through to :func:`build_pmtiles`.
    workers
        Worker processes of the shared tiling pool, one per core by default.
    concurrent_layers
        Layers warped at the same time. This bounds scratch disk use: each
        layer holds one RGBA raster of the whole scope while it is written and
        tiled, about 100 MB for Switzerland or 1.1 GB for the Alps.
    scratch
        Where those rasters are written; the system temporary directory by
        default.
    bounds
        Extent of the year's data in WGS84, which every archive's header
        declares: the mosaic item's bbox. Defaults to each VRT's extent.

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
            style=style,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            attribution=attribution,
            scratch=scratch,
            bounds=bounds,
            pool=pool,
        )

    sources: dict[str, tuple[LayerStyle, Path | UPath]] = {}
    for layer_id, style in sorted(wanted.items()):
        linear = vrts.linear.get(layer_id)
        if linear is None:
            logger.info("No VRT for %s; skipping", layer_id)
        elif style.decibel:
            decibel = Path(str(linear)).with_name(f"{layer_id}_webm{MOSAIC_ZOOM}_db.vrt")
            create_decibel_vrt(linear, decibel)
            sources[layer_id] = (style, decibel)
        else:
            sources[layer_id] = (style, linear)

    # The VRTs are local but read remote files, so the store's credentials are
    # set here, in the main thread, around all warps. rasterio applies options
    # set in the main thread to the whole process, but options set in another
    # thread only to that thread. GDAL reads a VRT's sources in its own
    # threads, so credentials set inside a warp thread would be missing there
    # ("No valid AWS credentials"). The tiler pool reads local rasters only.
    with (
        sources_env(vrts.sources_at),
        spawn_pool(workers) as pool,
        ThreadPoolExecutor(max_workers=max(1, concurrent_layers)) as warps,
    ):
        futures = {
            layer_id: warps.submit(build_one, layer_id, style, source)
            for layer_id, (style, source) in sources.items()
        }
        return [futures[layer_id].result() for layer_id in sources]
