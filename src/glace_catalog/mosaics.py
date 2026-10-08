"""The VRT chain, and the annual mosaic COGs written from it.

Each mosaicking and reprojection step is a VRT: the 10 m tiles of one UTM zone
form a virtual mosaic, which is warped onto the target grid, and the zones are
then combined on the scope's fixed extent. Only the final step writes data, as
a streaming windowed copy.

The chain is built for two grids, each directly from the UTM composites: the
equal-area ETRS89-LAEA 40 m grid for the annual analysis mosaics, and the
WebMercatorQuad z13 grid for the web-map archives. Each output is therefore
resampled once.

The chain, per year, band ``B`` and UTM zone ``Z``::

    1  {B}_{Z}.vrt            10 m UTM mosaic of that zone's tiles
    2  {B}_{Z}_webm13.vrt     (1) warped onto the target grid, average
    3  {B}_webm13.vrt         (2) for every zone, on the scope's fixed extent
    4  {B}_webm13_db.vrt      (3) in decibels; RTC and QA-CQM only

Both grids use these filenames, ``webm13`` included, and write their chains to
separate directories.

Step (3) feeds the mosaic COGs; (3) or (4) feeds the web-map tiles. The VRTs
are local scratch files with absolute ``/vsis3/`` sources, rebuilt in seconds.
The published record of a run is its manifest, configuration and products.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

import defusedxml.ElementTree as DefusedET
from freezebase.mgrs import MGRSGeoBox, mgrs_to_crs
from freezebase.raster import get_utm_zone_string, rasterio_open
from freezebase.s3 import s3_env
from freezebase.vrt import build_vrt_mosaic, create_decibel_vrt, create_warped_vrt
import numpy as np
from rasterio.enums import Resampling
import rasterio.shutil
from rasterio.warp import transform_bounds
from upath import UPath

from glace_catalog.scopes import MOSAIC_ZOOM, TILE_PX
from glace_catalog.stac.naming import published_name
from glace_catalog.stac.store import multihash_sha256_file
from glace_catalog.stac.tags import BandStatistics

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence

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

DECIBEL_BANDS: tuple[str, ...] = (
    "rtc_vv",
    "rtc_vh",
    "coh12_vv_qa_cqm",
    "coh12_vh_qa_cqm",
    "rtc_vv_qa_cqm",
    "rtc_vh_qa_cqm",
)
"""Bands with a decibel version, used only for the web maps.

All published rasters are linear, so warps and COG overviews average in the
linear domain, where averages are physically meaningful. Viewers convert to
decibels for display.
"""

COG_CREATION = {
    "driver": "COG",
    "BLOCKSIZE": TILE_PX,
    "COMPRESS": "LERC_ZSTD",
    "LEVEL": 9,
    "OVERVIEW_RESAMPLING": "AVERAGE",
    "BIGTIFF": "IF_SAFER",
    "NUM_THREADS": "ALL_CPUS",
}
"""COG creation options of an annual mosaic.

``AVERAGE`` overviews make each reduced level the mean of the level above.
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
    """The VRT chain of one year, as explicit paths from each step to the next.

    Consumers read their inputs from these paths.

    Parameters
    ----------
    linear
        Step (3) per asset key: the fixed-grid mosaic every published product
        is written from.
    decibel
        Step (4) per asset key, for the bands that have a decibel version.
    sources_at
        One of the tile paths the chain reads. The VRTs are local but their
        sources may be remote; :func:`chain_env` uses this path to set up the
        matching credentials.
    """

    linear: dict[str, Path | UPath]
    decibel: dict[str, Path]
    sources_at: Path | UPath | None = None


def build_year_vrts(
    files: Sequence[CompositeFile],
    scope: Scope,
    vrt_dir: Path,
    *,
    grid: Grid | None = None,
    decibel: bool = True,
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
    vrt_dir
        Local scratch directory the chain is written into.
    decibel
        Also build step (4), the decibel VRTs.

    Returns
    -------
    YearVrts
        The chain's outputs, keyed by asset key.
    """
    vrt_dir.mkdir(parents=True, exist_ok=True)

    by_band: dict[str, dict[str, list[CompositeFile]]] = defaultdict(lambda: defaultdict(list))
    grid = grid or scope.analysis_grid
    tiles_per_zone: dict[str, set[str]] = defaultdict(set)
    for file in files:
        zone = zone_token(file.tile)
        by_band[file.asset_key][zone].append(file)
        tiles_per_zone[zone].add(file.tile)

    # One extent per (year, zone), shared by every band.
    extents = {zone: zone_bounds(sorted(tiles), grid) for zone, tiles in tiles_per_zone.items()}

    linear: dict[str, Path | UPath] = {}
    for asset_key, zones in sorted(by_band.items()):
        # QA-NUM is a uint16 count on the tiles, with 0 as nodata. The mosaic
        # holds the float32 average of the source pixels, with NaN as nodata.
        promote = asset_key.endswith("_qa_num")
        warped: list[Path] = []
        for zone, zone_files in sorted(zones.items()):
            zone_vrt = vrt_dir / f"{asset_key}_{zone}.vrt"
            build_vrt_mosaic(sorted(f.path for f in zone_files), zone_vrt)
            web_vrt = vrt_dir / f"{asset_key}_{zone}_webm{MOSAIC_ZOOM}.vrt"
            create_warped_vrt(
                zone_vrt,
                web_vrt,
                crs=grid.crs,
                resolution=grid.resolution,
                bounds=extents[zone],
                resampling=Resampling.average,
                dtype="float32" if promote else None,
                nodata=float("nan") if promote else None,
            )
            warped.append(web_vrt)
        mosaic_vrt = vrt_dir / f"{asset_key}_webm{MOSAIC_ZOOM}.vrt"
        # The scope's fixed extent, so every year shares one grid.
        build_vrt_mosaic(warped, mosaic_vrt, bounds=grid.bounds)
        linear[asset_key] = mosaic_vrt
        logger.info("Built %s from %d zone(s)", mosaic_vrt.name, len(warped))

    decibels: dict[str, Path] = {}
    if decibel:
        for asset_key in sorted(set(DECIBEL_BANDS) & set(linear)):
            db_vrt = vrt_dir / f"{asset_key}_webm{MOSAIC_ZOOM}_db.vrt"
            create_decibel_vrt(linear[asset_key], db_vrt)
            decibels[asset_key] = db_vrt

    return YearVrts(
        linear=linear,
        decibel=decibels,
        sources_at=files[0].path if files else None,
    )


@contextmanager
def chain_env(vrts: YearVrts) -> Generator[None]:
    """Enter the environment the chain's sources need, if they are remote.

    A local VRT path says nothing about where its sources live, so the
    credentials are set around the read.
    """
    if vrts.sources_at is not None and UPath(vrts.sources_at).protocol == "s3":
        with s3_env(UPath(vrts.sources_at)):
            yield
    else:
        yield


def _parse_vrt(text: str) -> ET.Element:
    """Parse VRT XML with ``defusedxml``.

    VRTs are read back from the store, so they are treated as untrusted input;
    ``defusedxml`` rejects entity-expansion and external-entity attacks. XML is
    built and written with ``xml.etree.ElementTree``, because ``defusedxml``
    only parses.
    """
    return DefusedET.fromstring(text)


def tagged_vrt(
    src_vrt: Path | UPath,
    dst_vrt: Path,
    *,
    band_name: str,
    tags: Mapping[str, str] | None = None,
    band_tags: Mapping[str, str] | None = None,
) -> Path:
    """Copy a VRT with dataset tags, band tags and a band description attached.

    The tags go on the source VRT because GDAL copies a VRT's metadata into the
    file written from it, and adding tags to a finished COG would break its
    cloud-optimized layout.

    Parameters
    ----------
    src_vrt, dst_vrt
        VRT read, and the tagged copy written.
    band_name
        Description written onto the first band.
    tags
        Dataset-level metadata.
    band_tags
        Metadata of the first band, where Portolan requires the
        ``STATISTICS_*`` values.

    Returns
    -------
    pathlib.Path
        The tagged VRT.
    """
    root = _parse_vrt(UPath(src_vrt).read_text())
    if tags:
        metadata = ET.Element("Metadata")
        for key, value in sorted(tags.items()):
            ET.SubElement(metadata, "MDI", key=key).text = str(value)
        root.insert(0, metadata)
    band = root.find("VRTRasterBand")
    if band is not None:
        if band_tags:
            metadata = ET.Element("Metadata")
            for key, value in sorted(band_tags.items()):
                ET.SubElement(metadata, "MDI", key=key).text = str(value)
            band.insert(0, metadata)
        description = band.find("Description")
        if description is None:
            description = ET.SubElement(band, "Description")
        description.text = band_name
    ET.indent(root)
    dst_vrt.write_text(ET.tostring(root, encoding="unicode"))
    return dst_vrt


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
    src_vrt: Path | UPath,
    out_path: Path,
    *,
    asset_key: str,
    band_name: str,
    tags: Mapping[str, str] | None = None,
) -> MosaicWrite:
    """Write one fixed-grid VRT as a LERC_ZSTD mosaic COG.

    The VRT is copied block by block, so memory use stays small. Its statistics
    are computed in a separate streaming pass and written onto the VRT band
    before the copy, so they end up in the COG's own ``GDAL_METADATA`` header,
    as Portolan rule ``PTL-DAT-009`` requires.

    They are statistics of the source values. The written pixels differ from
    them by at most the LERC bound :func:`max_z_error`, far below each
    quantity's noise.

    Parameters
    ----------
    src_vrt
        Step (3) of the chain: the mosaic on the fixed grid.
    out_path
        Destination COG.
    asset_key
        Band being written; selects the quantisation bound.
    band_name
        Description written onto the output band.
    tags
        Dataset tags, typically the tiles' provenance plus the composite date
        window. They are attached to the VRT before the copy (see
        :func:`tagged_vrt`).

    Returns
    -------
    MosaicWrite
        The path written, its digest and its band statistics.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    creation = {**COG_CREATION, "MAX_Z_ERROR": max_z_error(asset_key)}
    logger.info("Writing %s (max_z_error %g)", out_path.name, creation["MAX_Z_ERROR"])
    with rasterio_open(src_vrt) as src:
        statistics = band_statistics(src, asset_key=asset_key)
    described = tagged_vrt(
        src_vrt,
        Path(str(src_vrt)).with_name(f"{Path(str(src_vrt)).stem}_tagged.vrt"),
        band_name=band_name,
        tags=tags,
        band_tags={
            tag: repr(float(getattr(statistics, field))) for field, tag in STATISTICS_TAGS.items()
        },
    )
    with rasterio_open(described) as src:
        rasterio.shutil.copy(src, str(out_path), **creation)
    return MosaicWrite(
        path=out_path,
        checksum=multihash_sha256_file(out_path),
        statistics=statistics,
    )


def build_mosaic_cogs(
    vrts: YearVrts,
    out_dir: Path,
    *,
    band_names: Mapping[str, str],
    tags: Mapping[str, Mapping[str, str]] | None = None,
    layers: Sequence[str] | None = None,
) -> dict[str, MosaicWrite]:
    """Write one mosaic COG per band from the chain's linear step.

    Parameters
    ----------
    vrts
        Chain built by :func:`build_year_vrts`.
    out_dir
        Local directory the COGs are written into, staged for one upload.
    band_names
        Band description per asset key.
    tags
        Dataset tags per asset key.
    layers
        Restrict to these asset keys. ``None``, the default, writes all twelve,
        including the QA layers.

    Returns
    -------
    dict[str, MosaicWrite]
        The COG written per asset key, with its digest and band statistics.
    """
    tags = tags or {}
    selected = sorted(vrts.linear if layers is None else set(layers) & set(vrts.linear))
    written: dict[str, MosaicWrite] = {}
    with chain_env(vrts):
        for asset_key in selected:
            written[asset_key] = build_mosaic_cog(
                vrts.linear[asset_key],
                out_dir / published_name(asset_key),
                asset_key=asset_key,
                band_name=band_names.get(asset_key, asset_key),
                tags=tags.get(asset_key),
            )
    return written


def mosaic_band_name(asset_key: str) -> str:
    """Band description of a mosaic: its asset key.

    Each file has one band, so the asset key identifies it, even in a file
    separated from the catalog. The mosaic's ``LAYER_DESCRIPTION`` tag explains
    how QA-NUM differs between tiles (counts) and mosaics (means).
    """
    return asset_key


def data_footprint(files: Sequence[CompositeFile]) -> tuple[dict[str, Any], list[float]]:
    """WGS84 footprint and bbox of the tiles present in a year.

    The mosaic covers the scope's fixed extent, a bounding box around an
    irregular perimeter. The item geometry follows the tiles that hold data.
    """
    from shapely.geometry import mapping  # noqa: PLC0415
    from shapely.ops import unary_union  # noqa: PLC0415

    tiles = sorted({file.tile for file in files})
    extents = [MGRSGeoBox.from_mgrs(tile).geographic_extent.geom for tile in tiles]
    union = unary_union(extents)
    return mapping(union), list(union.bounds)


__all__ = [
    "COG_CREATION",
    "DECIBEL_BANDS",
    "MAX_Z_ERROR",
    "STATISTICS_TAGS",
    "MosaicWrite",
    "YearVrts",
    "band_statistics",
    "build_mosaic_cog",
    "build_mosaic_cogs",
    "build_year_vrts",
    "chain_env",
    "data_footprint",
    "max_z_error",
    "mosaic_band_name",
    "zone_bounds",
    "zone_token",
]
