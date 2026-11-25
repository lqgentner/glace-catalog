"""The VRT chain, and the annual mosaic COGs written from it.

Each mosaicking and reprojection step is an in-memory VRT: the 10 m tiles of
one UTM zone form a virtual mosaic, which is warped onto the target grid, and
the zones are then combined on the scope's fixed extent. Only the final step
writes data, as a streaming windowed copy.

The chain is built for two grids, each directly from the UTM composites: the
equal-area ETRS89-LAEA 20 m grid for the annual analysis mosaics, and the
WebMercatorQuad z13 grid for the web-map archives. Each output is therefore
resampled once.

The chain, per year and band, for each UTM zone ``Z``::

    1  10 m UTM mosaic of the tiles in zone Z
    2  (1) warped onto the target grid, average
    3  (2) for every zone, on the scope's fixed extent
    4  (3) in decibels, for the web maps of the layers
       ``metadata/assets.yaml`` displays in decibels

All published rasters are linear, so warps and COG overviews average in the
linear domain, where averages are physically meaningful.

Step (3) feeds the mosaic COGs; (3) or (4) feeds the web-map tiles; the web-map
build adds (4) itself. The chain is held in memory and rebuilt for each run, in
about two minutes for the Alps. The published record of a run is its
manifest, configuration and products.
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import as_completed
from contextlib import nullcontext
from dataclasses import dataclass
import logging
import math
from typing import TYPE_CHECKING, Any

from freezebase import vrt
from freezebase.mgrs import MGRSGeoBox, mgrs_to_crs
from freezebase.raster import get_utm_zone_string, rasterio_open
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds

from glace_catalog.scopes import TILE_PX
from glace_catalog.stac.items import GEOMETRY_SIMPLIFY_DEG
from glace_catalog.stac.naming import published_name
from glace_catalog.stac.tags import BandStatistics
from glace_catalog.tiler import spawn_pool

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path

    from freezebase.vrt import VRT
    from rasterio.io import DatasetReader

    from glace_catalog.scopes import Grid, Scope
    from glace_catalog.stac.naming import CompositeFile

logger = logging.getLogger(__name__)

DENSIFY_PTS = 51
"""Points per edge when reprojecting a bounding box; four corners underestimate."""

MAX_Z_ERROR: dict[str, float] = {
    "rtc": 1e-5,
    "coh12": 1e-4,
    "qa_cqm": 1e-3,
    "qa_num": 0.0,
}
"""LERC error bound per quantity, in the quantity's own units.

Each bound is below 1.3 % of the quantity's per-pixel noise: for coherence the
Touzi spread at about 96 looks, measured over lake water; for RTC the measured
equivalent number of looks. The bounds depend on the data alone, so they hold
for any display stretch. Quantisation error stays constant when pixels are
aggregated while estimator noise averages down, hence the small fraction.

The tiles and the mosaics use the same bound. Neighbouring 10 m pixels are
strongly correlated through their estimation windows, so averaging them into a
mosaic pixel reduces the noise only slightly.
"""

COG_CREATION = {
    "blocksize": TILE_PX,
    "compress": "LERC_ZSTD",
    "level": 9,
    "overview_resampling": "AVERAGE",
}
"""COG creation options of an annual mosaic, as :func:`freezebase.raster.write_cog` takes them.

``AVERAGE`` overviews make each reduced level the mean of the level above.
"""

MOSAIC_WORKERS = 4
"""Mosaic layers written at the same time by default.

A worker peaks at about 2.7 GB on the Alps, so four suit a 16 GB machine.
"""

MOSAIC_GDAL_CACHE_MB = 512
"""GDAL block cache of one mosaic worker, in megabytes.

A worker warps one layer block by block, reading a few composites per block.
"""

VRT_WORKERS = 12
"""Layers whose VRT chain is built at the same time, one process each.

Building a layer's chain reads the header of every composite of that layer,
which is mostly Python work, so threads would not run it in parallel.
"""


STATISTICS_TAGS: dict[str, str] = {
    "minimum": "STATISTICS_MINIMUM",
    "maximum": "STATISTICS_MAXIMUM",
    "mean": "STATISTICS_MEAN",
    "stddev": "STATISTICS_STDDEV",
    "valid_percent": "STATISTICS_VALID_PERCENT",
}
"""GDAL band tag for each :class:`~glace_catalog.stac.tags.BandStatistics` field.

Portolan rule ``PTL-DAT-009`` requires the first four in the file's own
``GDAL_METADATA``, and ``PTL-DAT-010`` requires the fifth for bands with a
nodata value, which every mosaic band has (float32 with NaN). rashid checks
that the tags exist.
"""


def max_z_error(asset_key: str) -> float:
    """Quantisation bound of one band, from :data:`MAX_Z_ERROR`."""
    for suffix in ("qa_num", "qa_cqm"):
        if asset_key.endswith(suffix):
            return MAX_Z_ERROR[suffix]
    return MAX_Z_ERROR["coh12" if asset_key.startswith("coh") else "rtc"]


def zone_token(tile: str) -> str:
    """UTM zone token of an MGRS tile code, e.g. ``"32TMS25"`` -> ``"utm32n"``."""
    return f"utm{get_utm_zone_string(mgrs_to_crs(tile))}".lower()


def zone_bounds(tiles: Sequence[str], grid: Grid) -> tuple[float, float, float, float]:
    """Snapped target extent covering every tile of one zone, on ``grid``.

    Computed once per year and zone and shared by all bands. Bands with
    different tile sets (an RTC-only tile, say) then still share one grid and
    can be compared pixel by pixel.
    """
    left = bottom = math.inf
    right = top = -math.inf
    for tile in tiles:
        geobox = MGRSGeoBox.from_mgrs(tile)
        bounds = transform_bounds(
            geobox.crs,
            grid.crs,
            *geobox.boundingbox,
            densify_pts=DENSIFY_PTS,
        )
        left, bottom = min(left, bounds[0]), min(bottom, bounds[1])
        right, top = max(right, bounds[2]), max(top, bounds[3])
    return grid.snap((left, bottom, right, top))


@dataclass(frozen=True)
class YearVrts:
    """The VRT chain of one year.

    Parameters
    ----------
    linear
        Step (3) per asset key: the fixed-grid mosaic every published product
        is written from.
    """

    linear: dict[str, VRT]

    def env(self) -> AbstractContextManager[None]:
        """Return the environment that reads the chain's sources.

        Every layer of a year reads the same store, so one layer's environment
        serves them all. Enter it in the main thread: rasterio applies options
        set there to the whole process, but options set in another thread only
        to that thread, and GDAL reads a VRT's sources in threads of its own.
        """
        return next(iter(self.linear.values())).env() if self.linear else nullcontext()


def _build_band_vrt(
    asset_key: str,
    zones: Mapping[str, Sequence[CompositeFile]],
    grid: Grid,
    extents: Mapping[str, tuple[float, float, float, float]],
) -> VRT:
    """Build one layer's chain: a mosaic per UTM zone, warped onto the grid, then mosaicked."""
    # QA-NUM is a uint16 count on the tiles, with 0 as nodata. The mosaic
    # holds the float32 average of the source pixels, with NaN as nodata.
    promote = asset_key.endswith("_qa_num")
    warped = [
        vrt.mosaic(sorted(f.path for f in zone_files)).reproject(
            crs=grid.crs,
            resolution=grid.resolution,
            bounds=extents[zone],
            resampling=Resampling.average,
            dtype="float32" if promote else None,
            nodata=float("nan") if promote else None,
        )
        for zone, zone_files in sorted(zones.items())
    ]
    # The scope's fixed extent, so every year shares one grid.
    return vrt.mosaic(warped, bounds=grid.bounds)


def build_year_vrts(
    files: Sequence[CompositeFile],
    scope: Scope,
    *,
    grid: Grid | None = None,
) -> YearVrts:
    """Build the whole VRT chain for one year's composites.

    Parameters
    ----------
    files
        Every composite of the year, already verified against the manifest.
        Their paths are absolute, typically ``s3://``.
    scope
        Scope being published.
    grid
        Target grid. Defaults to the scope's analysis grid; the web-map build
        passes ``scope.display_grid``. Both are warped directly from the UTM
        tiles.

    Returns
    -------
    YearVrts
        The chain's outputs, keyed by asset key.
    """
    by_band: dict[str, dict[str, list[CompositeFile]]] = defaultdict(lambda: defaultdict(list))
    grid = grid or scope.analysis_grid
    tiles_per_zone: dict[str, set[str]] = defaultdict(set)
    for file in files:
        zone = zone_token(file.tile)
        by_band[file.asset_key][zone].append(file)
        tiles_per_zone[zone].add(file.tile)

    # One extent per (year, zone), shared by every band.
    extents = {zone: zone_bounds(sorted(tiles), grid) for zone, tiles in tiles_per_zone.items()}

    keys = sorted(by_band)
    with spawn_pool(min(VRT_WORKERS, len(keys)) or 1) as pool:
        built = pool.map(
            _build_band_vrt,
            keys,
            [dict(by_band[key]) for key in keys],
            [grid] * len(keys),
            [extents] * len(keys),
        )
        linear = dict(zip(keys, built, strict=True))
    logger.info("Built the chains of %s", ", ".join(keys))
    return YearVrts(linear=linear)


def band_statistics(src: DatasetReader, *, asset_key: str) -> BandStatistics:
    """Exact statistics of a single-band source, computed block by block.

    The band is read one block at a time; the Alps mosaic is 24 100 x 14 475
    float32 pixels (1.4 GiB). Sums are accumulated in float64, and the standard
    deviation is the population one, as GDAL computes it, so the values match
    ``gdalinfo -stats``.

    Valid pixels are the finite ones: every mosaic band is float32 with NaN as
    nodata.

    Raises
    ------
    ValueError
        If the band has no finite pixel, which means the run is broken.
    """
    total = 0
    count = 0
    value_sum = 0.0
    square_sum = 0.0
    minimum = math.inf
    maximum = -math.inf
    for _, window in src.block_windows(1):
        block = src.read(1, window=window)
        values = block[np.isfinite(block)].astype("float64")
        total += int(block.size)
        count += int(values.size)
        if values.size:
            value_sum += float(values.sum())
            square_sum += float(np.square(values).sum())
            minimum = min(minimum, float(values.min()))
            maximum = max(maximum, float(values.max()))
    if not count:
        msg = f"{asset_key}: the mosaic has no valid pixel, so it has no statistics to publish."
        raise ValueError(msg)
    mean = value_sum / count
    # Population variance, clamped at zero: on a constant band the one-pass
    # formula can come out slightly negative through rounding.
    variance = max(square_sum / count - mean * mean, 0.0)
    return BandStatistics(
        minimum=minimum,
        maximum=maximum,
        mean=mean,
        stddev=math.sqrt(variance),
        valid_percent=100.0 * count / total,
    )


def overview_factors(width: int, height: int, block: int) -> list[int]:
    """Overview factors of a COG of this size, down to the level that fits in one tile.

    GDAL's COG driver halves the raster until both sides, rounded down, are at
    most one tile. Portolan rule ``PTL-DAT-011`` requires overviews, and a
    zoomed-out reader then reads a single tile.
    """
    count = 0
    while width >> count > block or height >> count > block:
        count += 1
    return [2**level for level in range(1, count + 1)]


def require_overviews(path: Path) -> None:
    """Check that a written COG has every overview level :func:`overview_factors` lists.

    Raises
    ------
    ValueError
        If the file has fewer or other levels, so that a zoomed-out read
        would fetch more than one tile.
    """
    with rasterio_open(path) as src:
        block_height, block_width = src.block_shapes[0]
        width, height = src.width, src.height
        found = src.overviews(1)
    expected = overview_factors(width, height, max(block_height, block_width))
    if found != expected:
        msg = (
            f"{path.name} has overview factors {found}, but a {width} x {height} "
            f"raster in {block_width} px tiles needs {expected}"
        )
        raise ValueError(msg)


@dataclass(frozen=True)
class MosaicWrite:
    """One written mosaic COG, with what the item has to say about it.

    Parameters
    ----------
    path
        The file written.
    checksum
        Multihash-encoded sha256 of its bytes, published as ``file:checksum``.
    statistics
        The band statistics embedded in the file, which the item also
        publishes under ``bands[].statistics``.
    """

    path: Path
    checksum: str
    statistics: BandStatistics


def build_mosaic_cog(
    src: VRT,
    out_path: Path,
    *,
    asset_key: str,
    tags: Mapping[str, str] | None = None,
) -> MosaicWrite:
    """Write one fixed-grid VRT as a LERC_ZSTD mosaic COG.

    The VRT is streamed into the COG block by block, so memory use stays
    small. The statistics are computed in a separate
    streaming pass beforehand and written as band tags, so they end up in the
    COG's own ``GDAL_METADATA`` header, as Portolan rule ``PTL-DAT-009``
    requires.

    They are statistics of the source values. The written pixels differ from
    them by at most the LERC bound :func:`max_z_error`, far below each
    quantity's noise.

    Parameters
    ----------
    src
        Step (3) of the chain: the mosaic on the fixed grid.
    out_path
        Destination COG.
    asset_key
        Band being written; selects the quantisation bound. It is also the band
        description: each file has one band, so the asset key identifies it
        even in a file separated from the catalog. The ``LAYER_DESCRIPTION``
        tag explains how QA-NUM differs between tiles (counts) and mosaics
        (means).
    tags
        Dataset tags, typically the tiles' provenance plus the composite date
        window.

    Returns
    -------
    MosaicWrite
        The path written, its digest and its band statistics.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    profile = {**COG_CREATION, "max_z_error": max_z_error(asset_key)}
    logger.info("Writing %s (max_z_error %g)", out_path.name, profile["max_z_error"])
    with src.open() as dataset:
        statistics = band_statistics(dataset, asset_key=asset_key)
    with rasterio.Env(GDAL_NUM_THREADS="ALL_CPUS"):
        checksum = src.to_cog(
            out_path,
            profile,
            band_names=[asset_key],
            tags=tags,
            band_tags=[
                {
                    tag: repr(float(getattr(statistics, field)))
                    for field, tag in STATISTICS_TAGS.items()
                }
            ],
            checksum=True,
        )
    require_overviews(out_path)
    return MosaicWrite(path=out_path, checksum=checksum, statistics=statistics)


def build_mosaic_cogs(
    vrts: YearVrts,
    out_dir: Path,
    *,
    tags: Mapping[str, Mapping[str, str]] | None = None,
    layers: Sequence[str] | None = None,
    workers: int = MOSAIC_WORKERS,
) -> dict[str, MosaicWrite]:
    """Write one mosaic COG per band from the chain's linear step.

    Each layer's warp runs on one core, so the layers are written in parallel,
    one process each. Each still compresses with every core: a layer whose
    warp finishes early leaves its cores to the others.

    Parameters
    ----------
    vrts
        Chain built by :func:`build_year_vrts`.
    out_dir
        Local directory the COGs are written into, staged for one upload.
    tags
        Dataset tags per asset key.
    layers
        Restrict to these asset keys. ``None``, the default, writes all twelve,
        including the QA layers.
    workers
        Layers written at the same time, one process each.

    Returns
    -------
    dict[str, MosaicWrite]
        The COG written per asset key, with its digest and band statistics.
    """
    tags = tags or {}
    selected = sorted(vrts.linear if layers is None else set(layers) & set(vrts.linear))
    if not selected:
        return {}
    workers = min(workers, len(selected))
    logger.info("Writing %d mosaic(s), %d at a time", len(selected), workers)
    with spawn_pool(workers, cache_mb=MOSAIC_GDAL_CACHE_MB) as pool:
        futures = {
            pool.submit(
                build_mosaic_cog,
                vrts.linear[asset_key],
                out_dir / published_name(asset_key),
                asset_key=asset_key,
                tags=tags.get(asset_key),
            ): asset_key
            for asset_key in selected
        }
        written: dict[str, MosaicWrite] = {}
        try:
            for future in as_completed(futures):
                written[futures[future]] = future.result()
                logger.info("Wrote %s", written[futures[future]].path.name)
        except BaseException:
            # Stop at the first failure rather than writing the queued layers.
            pool.shutdown(cancel_futures=True)
            raise
    return {key: written[key] for key in selected}


def data_footprint(files: Sequence[CompositeFile]) -> tuple[dict[str, Any], list[float]]:
    """WGS84 footprint and bbox of the tiles present in a year.

    The mosaic covers the scope's fixed extent, a bounding box around an
    irregular perimeter. The item geometry follows the tiles that hold data.

    The union of the tile extents has tens of thousands of vertices, most of
    them along the reprojected tile edges. It is grown by
    :data:`~glace_catalog.stac.items.GEOMETRY_SIMPLIFY_DEG` and then simplified
    by the same tolerance, so the footprint keeps a few hundred vertices and
    still covers every tile. The bbox is that footprint's.
    """
    from shapely.geometry import mapping  # noqa: PLC0415
    from shapely.ops import unary_union  # noqa: PLC0415

    tiles = sorted({file.tile for file in files})
    extents = [MGRSGeoBox.from_mgrs(tile).geographic_extent.geom for tile in tiles]
    union = unary_union(extents)
    footprint = union.buffer(GEOMETRY_SIMPLIFY_DEG, join_style="mitre").simplify(
        GEOMETRY_SIMPLIFY_DEG
    )
    return mapping(footprint), list(footprint.bounds)


__all__ = [
    "COG_CREATION",
    "MAX_Z_ERROR",
    "STATISTICS_TAGS",
    "MosaicWrite",
    "YearVrts",
    "band_statistics",
    "build_mosaic_cog",
    "build_mosaic_cogs",
    "build_year_vrts",
    "data_footprint",
    "max_z_error",
    "zone_bounds",
    "zone_token",
]
