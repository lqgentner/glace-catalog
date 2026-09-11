"""The VRT chain, and the annual mosaic COGs written from it.

Every mosaicking and reprojection step is a **VRT** rather than an in-memory
merge: the 10 m tiles of one UTM zone become a virtual mosaic, that mosaic is
warped virtually onto the target grid, and the zones are mosaicked virtually
again onto the scope's fixed extent.

**The chain is built twice, against two grids** (M-24). The annual mosaics go on
the scope's equal-area ETRS89-LAEA 40 m grid, because WebMercator is not
equal-area and an area statistic taken on it needs a correction the catalog does
not declare. The web-map archives go on the WebMercatorQuad grid at z13. Both
start from the same UTM composites; neither is warped out of the other, so the
display path never pays a second resampling and the analysis path never carries
a non-equal-area grid. Only the last step
materialises anything, and it does so as a streaming windowed copy rather than a
gigabyte-scale in-RAM merge.

The chain, per year, per band ``B``, per UTM zone ``Z``::

    1  {B}_{Z}.vrt            10 m UTM mosaic of that zone's tiles
    2  {B}_{Z}_webm13.vrt     (1) warped onto the target grid, average
    3  {B}_webm13.vrt         (2) for every zone, on the canonical extent
    4  {B}_webm13_db.vrt      (3) in decibels; RTC and QA-CQM only
    5  {P}_rgb_webm13.vrt     R=VV, G=VH, B=VV-VH (RTC) or VV/VH (COH12)

(The ``webm13`` token is historical: the same filenames are used for whichever
grid the chain was asked for, and the two chains are kept in separate cache
directories rather than distinguished by name.)

(3) feeds the mosaic COGs, (3) or (4) the single-band web-map tiles, and (5) the
false-colour ones. The VRTs are **local and ephemeral**: they hold absolute
``/vsis3/`` sources, they are not published, and they take seconds to rebuild —
so the reproducibility record is the manifest, the config snapshot and the
products, not 1 700 XML files in a public listing.

On the **display** grid the target is WebMercatorQuad at z13, so a COG's
internal overview levels land exactly on z12, z11, …: the file *is* a tile
pyramid. On the **analysis** grid that property is neither available nor wanted
— nothing tiles an equal-area mosaic.
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
from freezebase.vrt import build_vrt_mosaic, create_decibel_vrt, create_rgb_vrt, create_warped_vrt
import numpy as np
from rasterio.enums import Resampling
import rasterio.shutil
from rasterio.warp import transform_bounds
from upath import UPath

from glace_catalog.scopes import MOSAIC_ZOOM, TILE_PX
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
    "qa_cqm": 1e-4,
    "qa_num": 0.0,
}
"""LERC quantisation bound per quantity, in the quantity's own units.

Anchored to the **spread of the data**, not to the display: coherence against the
Touzi spread at L about 96 measured over lake water, RTC against measured ENL,
landing below 1.3 % of each quantity's own per-pixel noise. Keeping both products
in the same band matters, because quantisation error does not average down under
aggregation while the estimator's noise does. Anchoring to a fraction of an
8-bit step across the nominal stretch would be wrong twice over — the stretch is
user-adjustable, which is the point of serving a COG, and 8 bits is a property of
today's rendering path rather than of the measurement.

The bound transfers unchanged from the 10 m tile to the 19 m mosaic. Averaging
about 3.6 source pixels would normally cut the spread by the square root of that
and argue for a tighter bound, but adjacent 10 m pixels are already heavily
correlated by their own estimation windows, so averaging a handful of neighbours
adds almost no independent information.
"""

DECIBEL_BANDS: tuple[str, ...] = (
    "rtc_vv",
    "rtc_vh",
    "coh12_vv_qa_cqm",
    "coh12_vh_qa_cqm",
    "rtc_vv_qa_cqm",
    "rtc_vh_qa_cqm",
)
"""Bands with a decibel reading, which only the web-map branch ever uses.

Everything published is linear. The warp then averages in the linear domain,
where it is physically meaningful, and so does the COG's own internal pyramid —
``AVERAGE`` on a decibel raster would give log-domain means at z12 and below,
silently a different quantity from the level above them. The conversion belongs
in the viewer.
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

256 px blocks so a block is exactly one web-map tile, and ``AVERAGE`` overviews
so the reduced levels are means of the level above rather than samples of it.
"""


STATISTICS_TAGS: dict[str, str] = {
    "minimum": "STATISTICS_MINIMUM",
    "maximum": "STATISTICS_MAXIMUM",
    "mean": "STATISTICS_MEAN",
    "stddev": "STATISTICS_STDDEV",
    "valid_percent": "STATISTICS_VALID_PERCENT",
}
"""GDAL band tag each :class:`~glace_catalog.stac.tags.BandStatistics` field is written as.

``PTL-DAT-009`` requires the first four in the file's own ``GDAL_METADATA``, and
``PTL-DAT-010`` the fifth wherever the band declares a nodata value — which
every mosaic does, as float32 with a NaN sentinel. rashid checks that they are
present, not what they say.
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

    Computed once per (year, zone) and shared by **every band**, not per band.
    Where one product's tile set differs from another's — an RTC-only tile, say —
    a per-band extent would give the two warps different grids, and the
    false-colour VRT that has to read both would be impossible to build.
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

    Every consumer takes its input from here rather than globbing a directory:
    there is no filename to parse and no ordering hazard.

    Parameters
    ----------
    linear
        Step (3) per asset key: the canonical-grid mosaic every published
        product is written from.
    decibel
        Step (4) per asset key, for the bands that have a decibel reading.
    rgb
        Step (5) per product, e.g. ``{"rtc": …, "coh12": …}``.
    sources_at
        One of the tile paths the chain reads. The VRTs are local but their
        sources are not, so nothing can read them without the object store's
        credentials — and a local path carries none. Keeping a source path here
        is what lets a consumer of the chain enter the right environment without
        being told about it separately.
    """

    linear: dict[str, Path | UPath]
    decibel: dict[str, Path]
    rgb: dict[str, Path]
    sources_at: Path | UPath | None = None


def build_year_vrts(
    files: Sequence[CompositeFile],
    scope: Scope,
    vrt_dir: Path,
    *,
    grid: Grid | None = None,
    decibel: bool = True,
    rgb: bool = True,
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
        Target grid to warp onto. Defaults to the scope's **analysis** grid;
        the web-map branch passes ``scope.display_grid`` instead. Both are
        warped straight from the UTM tiles, so neither is derived from the
        other (M-24, M-25).
    vrt_dir
        Local directory the chain is written into. Ephemeral.
    decibel
        Also build step (4), the decibel VRTs.
    rgb
        Also build step (5), the false-colour VRTs.

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
        # QA-NUM is a uint16 count with a zero sentinel on the tile, and a
        # float32 mean with a NaN one on the mosaic: averaging 3.6 source pixels
        # would discard three in four under `nearest`, and `max` is biased high.
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
        # The canonical extent, so every year of the series shares one grid.
        build_vrt_mosaic(warped, mosaic_vrt, bounds=grid.bounds)
        linear[asset_key] = mosaic_vrt
        logger.info("Built %s from %d zone(s)", mosaic_vrt.name, len(warped))

    decibels: dict[str, Path] = {}
    if decibel:
        for asset_key in sorted(set(DECIBEL_BANDS) & set(linear)):
            db_vrt = vrt_dir / f"{asset_key}_webm{MOSAIC_ZOOM}_db.vrt"
            create_decibel_vrt(linear[asset_key], db_vrt)
            decibels[asset_key] = db_vrt

    rgbs: dict[str, Path] = {}
    if rgb:
        for product in ("coh12", "rtc"):
            # RTC's blue band is a difference of decibels, coherence's a ratio
            # of linear values; both reduce to "how much brighter is VV".
            source = decibels if product == "rtc" else linear
            vv, vh = f"{product}_vv", f"{product}_vh"
            if vv not in source or vh not in source:
                continue
            rgb_vrt = vrt_dir / f"{product}_rgb_webm{MOSAIC_ZOOM}.vrt"
            create_rgb_vrt(
                source[vv],
                source[vh],
                rgb_vrt,
                decibel_scale=product == "rtc",
            )
            rgbs[product] = rgb_vrt

    return YearVrts(
        linear=linear,
        decibel=decibels,
        rgb=rgbs,
        sources_at=files[0].path if files else None,
    )


def build_cog_chain(
    cogs: Mapping[str, Path | UPath],
    vrt_dir: Path,
    *,
    decibel: bool = True,
    rgb: bool = True,
) -> YearVrts:
    """Build a chain over the written mosaic COGs rather than over the tiles.

    Once the mosaics exist, they *are* the canonical-grid step of the chain —
    already warped, already on the target grid, and carrying their own overviews.
    Deriving the web-map layers from them instead of from the tile chain means a
    reader opens one file rather than 143 (or 286, for a false-colour layer that
    reads two polarizations), and a low zoom level reads a decimated overview
    instead of warping full-resolution data it will throw away.

    That is what makes the tiling affordable: the tile chain holds a whole
    dataset graph open per worker process, which is where the memory goes.

    Parameters
    ----------
    cogs
        Written mosaic COG per asset key.
    vrt_dir
        Local directory the derived VRTs are written into.
    decibel, rgb
        Also derive the decibel and false-colour readings.

    Returns
    -------
    YearVrts
        The same shape as :func:`build_year_vrts`, over the mosaics.
    """
    vrt_dir.mkdir(parents=True, exist_ok=True)
    # Kept as given: a published mosaic is on the object store, and coercing it
    # to a local path would strip the protocol it needs to be opened by.
    linear: dict[str, Path | UPath] = dict(cogs)

    decibels: dict[str, Path] = {}
    if decibel:
        for asset_key in sorted(set(DECIBEL_BANDS) & set(cogs)):
            db_vrt = vrt_dir / f"{asset_key}_cog_db.vrt"
            create_decibel_vrt(cogs[asset_key], db_vrt)
            decibels[asset_key] = db_vrt

    rgbs: dict[str, Path] = {}
    if rgb:
        for product in ("coh12", "rtc"):
            source: Mapping[str, Path | UPath] = decibels if product == "rtc" else cogs
            vv, vh = f"{product}_vv", f"{product}_vh"
            if vv not in source or vh not in source:
                continue
            rgb_vrt = vrt_dir / f"{product}_rgb_cog.vrt"
            create_rgb_vrt(source[vv], source[vh], rgb_vrt, decibel_scale=product == "rtc")
            rgbs[product] = rgb_vrt

    return YearVrts(
        linear=linear,
        decibel=decibels,
        rgb=rgbs,
        sources_at=next(iter(cogs.values())) if cogs else None,
    )


@contextmanager
def chain_env(vrts: YearVrts) -> Generator[None]:
    """Enter the environment the chain's sources need, if they are remote.

    Reading a local VRT gives GDAL no hint that its sources are on an object
    store, so the credentials have to be established around the read rather than
    derived from the path being opened.
    """
    if vrts.sources_at is not None and UPath(vrts.sources_at).protocol == "s3":
        with s3_env(UPath(vrts.sources_at)):
            yield
    else:
        yield


def _parse_vrt(text: str) -> ET.Element:
    """Parse VRT XML, through the hardened parser.

    Two parsers appear in this module and they do not overlap. Anything *read*
    goes through ``defusedxml``, which refuses the entity-expansion and external
    entity constructs the stdlib parser accepts; a VRT is written by GDAL but
    read back from wherever the store lives, so it is input. Anything *built*
    uses ``xml.etree.ElementTree``, which has no parser in it — defusedxml
    deliberately ships no serializer, and element construction is not an attack
    surface. Parsing is confined here so no caller has to know that.
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

    The tags have to be on the *source*: a finished COG cannot be reopened to add
    them without breaking the layout that makes it cloud-optimized, and GDAL
    carries a VRT's metadata into the file it copies out of it.

    Parameters
    ----------
    src_vrt, dst_vrt
        VRT read, and the tagged copy written.
    band_name
        Description written onto the first band.
    tags
        Dataset-level metadata.
    band_tags
        Band-level metadata of the first band, which is where the
        ``STATISTICS_*`` values Portolan requires have to sit.

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
    """Exact statistics of a single-band source, accumulated window by window.

    One streaming pass, never the whole band in memory: the Alps mosaic is
    24 100 x 14 475 float32, which is 1.4 GiB read at once. Sums are accumulated
    in float64 and the standard deviation is the **population** one, as GDAL
    computes it, so the values are the ones a reader would get from
    ``gdalinfo -stats`` rather than an approximation of them.

    Valid pixels are the finite ones: every mosaic band is float32 with a NaN
    nodata sentinel, so NaN is exactly the nodata mask.

    Raises
    ------
    ValueError
        If the band has no finite pixel at all. An empty mosaic is a broken run,
        not a band that legitimately has nothing to say about itself.
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
    # Population variance, clamped: the one-pass form can go slightly negative
    # on a constant band where the two accumulated sums cancel.
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
        The band statistics embedded in it, so the item can publish the same
        five values under ``bands[].statistics`` without a second header read.
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
    """Write one canonical-grid VRT as a LERC_ZSTD mosaic COG.

    A streaming windowed copy of the VRT, so nothing larger than a block is ever
    in memory — where the earlier in-RAM merge held a whole continental band at
    once. The statistics are taken in one further streaming pass over the same
    VRT and written onto the band *before* the copy, which is the only way they
    reach the COG's own ``GDAL_METADATA`` header: a finished COG cannot be
    reopened to add them without either losing its layout or leaving them in a
    PAM sidecar, and ``PTL-DAT-009`` requires them in the file.

    They are the statistics of the **source** values. The written pixels differ
    from them by at most the LERC bound :func:`max_z_error`, which is orders of
    magnitude below each quantity's own noise; rashid checks that the five tags
    are present rather than recomputing them.

    Parameters
    ----------
    src_vrt
        Step (3) of the chain: the mosaic on the canonical grid.
    out_path
        Destination COG.
    asset_key
        Band being written; selects the quantisation bound.
    band_name
        Description written onto the output band.
    tags
        Dataset tags, typically the provenance the tiles carried plus the
        composite date window. Attached to the VRT before the copy, since a
        finished COG cannot take them without losing its layout.

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
        Restrict to these asset keys. ``None`` writes all twelve, which is the
        default: QA-CQM is the LRW weighting's own record, and a continental map
        of it is a dataset-paper figure.

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
                out_dir / f"{asset_key}.tif",
                asset_key=asset_key,
                band_name=band_names.get(asset_key, asset_key),
                tags=tags.get(asset_key),
            )
    return written


def mosaic_band_name(asset_key: str) -> str:
    """Band description of a mosaic: its asset key, like every band in the catalog.

    One band per file, so the asset key identifies it, and a file separated from
    the catalog still says what it holds. The tiles carry the same rule once
    s1bursts writes it (UPSTREAM.md S-4); the mosaic's ``LAYER_DESCRIPTION`` tag
    is where the count-versus-mean difference is spelled out.
    """
    return asset_key


def data_footprint(files: Sequence[CompositeFile]) -> tuple[dict[str, Any], list[float]]:
    """WGS84 footprint and bbox of the tiles a year actually holds.

    The mosaic sits on the scope's canonical extent, which is a bounding box
    around a ragged perimeter; the item's geometry is the data, not the box.
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
    "build_cog_chain",
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
