"""The documents of a GLACE store: its items and its two collections.

A store splits into a per-tile collection and a per-year mosaic collection —
different products, different grids, different consumers. The split is a
collection boundary rather than a property because a boundary is enforced: with
it, one asset key names exactly one thing inside its own collection, which is
what lets both collections declare ``item_assets`` and lets a consumer read the
index of one without filtering the other out of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, date, datetime, time, timedelta
import math
from typing import TYPE_CHECKING, Any

from freezebase.mgrs import MGRSGeoBox
import pystac
from pystac.utils import datetime_to_str

from glace_catalog.stac.naming import (
    POLARIZATIONS,
    PRODUCTS,
)
from glace_catalog.stac.tags import (
    SPECIFICATION_URL,
    require_valid,
    standard_fields,
    tag_fields,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from glace_catalog.scopes import Scope
    from glace_catalog.stac.naming import CompositeFile
    from glace_catalog.stac.tags import RasterInfo

GEOMETRY_SIMPLIFY_DEG = 1e-4  # ~11 m at the equator

DATASET_VERSION = "1.0"
"""Published edition of the GLACE dataset, as the STAC version extension's ``version``.

Bump the major part when the pixels change meaning, so that a value stored under
one edition can never be reinterpreted under a later reading.
"""

MGRS_EXTENSION_URI = "https://stac-extensions.github.io/mgrs/v1.0.0/schema.json"
SAR_EXTENSION_URI = "https://stac-extensions.github.io/sar/v1.3.2/schema.json"
PROJECTION_EXTENSION_URI = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"
CLASSIFICATION_EXTENSION_URI = (
    "https://stac-extensions.github.io/classification/v2.0.0/schema.json"
)
RASTER_EXTENSION_URI = "https://stac-extensions.github.io/raster/v2.0.0/schema.json"
FILE_EXTENSION_URI = "https://stac-extensions.github.io/file/v2.1.0/schema.json"
PROCESSING_EXTENSION_URI = "https://stac-extensions.github.io/processing/v1.2.0/schema.json"
VERSION_EXTENSION_URI = "https://stac-extensions.github.io/version/v1.2.0/schema.json"
WEB_MAP_LINKS_EXTENSION_URI = "https://stac-extensions.github.io/web-map-links/v1.2.0/schema.json"

CEOS_ARD_LINK_REL = "ceos-ard-specification"
"""Relation type of the CEOS-ARD extension, used without declaring the extension.

The extension's README explicitly allows this, and its schema would reject the
declaration anyway: it enumerates no Composite Backscatter product.
"""

"""Where a tile item finds its year's manifest, relative to the item."""

PMTILES_LINK_REL = "pmtiles"
"""Web Map Links relation type of a PMTiles archive.

A pre-styled RGBA pyramid is a rendering, not a measurement — no bands, no
units, no values to read — so it is a link rather than an asset or an item. That
also keeps the asset key ``coh12_vv`` naming one thing inside the mosaics
collection rather than both a COG and an archive.
"""


def _add_specification_link(target: pystac.Item | pystac.Collection) -> None:
    """Link to the CEOS-ARD PFS the metadata borrows its vocabulary from.

    Uses the ``ceos-ard-specification`` relation type of the STAC CEOS-ARD
    extension without declaring the extension, which the extension's README
    explicitly allows and which its schema would reject anyway: it enumerates
    no Composite Backscatter product.
    """
    target.add_link(
        pystac.Link(
            rel=CEOS_ARD_LINK_REL,
            target=SPECIFICATION_URL,
            media_type=pystac.MediaType.PDF,
            title="CEOS-ARD PFS for Synthetic Aperture Radar v1.3.1",
        ),
    )


def _tile_geometry(geobox: MGRSGeoBox) -> tuple[dict, list[float]]:
    """WGS84 polygon (simplified to the corners) and bounding box of a tile."""
    extent = geobox.geographic_extent
    geometry = extent.simplify(GEOMETRY_SIMPLIFY_DEG).geom.__geo_interface__
    return geometry, list(extent.boundingbox.bbox)


def _apply_common_metadata(item: pystac.Item, geobox: MGRSGeoBox, tile: str) -> None:
    """Add MGRS and projection fields shared by data and label items."""
    if geobox.crs is None:
        msg = f"GeoBox for tile {tile} has no CRS."
        raise TypeError(msg)
    item.properties["glace:mgrs_tile"] = tile
    item.properties["mgrs:utm_zone"] = int(tile[:2])
    item.properties["mgrs:latitude_band"] = tile[2]
    item.properties["mgrs:grid_square"] = tile[3:5]
    item.properties["proj:code"] = f"EPSG:{geobox.crs.epsg}"
    item.properties["proj:shape"] = [geobox.shape.y, geobox.shape.x]
    item.properties["proj:transform"] = list(geobox.affine)[:6]
    item.stac_extensions.extend([MGRS_EXTENSION_URI, PROJECTION_EXTENSION_URI])


def _method_label(file: CompositeFile) -> str:
    """Human-readable compositing method of a composite, from its filename."""
    if file.orbit is None:
        return "LRW composite"
    return f"{'ascending' if file.orbit == 'A' else 'descending'} pass composite"


def composite_window(pairs: Iterable[tuple[str, str]]) -> tuple[date, date]:
    """Return the one compositing window a set of composites shares, as inclusive days.

    s1bursts names and tags every composite of a run with the caller's window,
    so every tile and every layer of a year carries the same pair, and the year
    has exactly one time range. A second pair is a file left from a different
    window, which the archive has to resolve before the year can be published;
    it is never averaged or widened into the extent.

    Parameters
    ----------
    pairs
        ``(start, end)`` ISO dates, one per composite, as the manifest's
        ``composite_start`` / ``composite_end`` columns or the rasters'
        ``COMPOSITE_START_DATE`` / ``COMPOSITE_END_DATE`` tags give them.

    Raises
    ------
    ValueError
        If the pairs disagree, or there are none.
    """
    windows = sorted({(str(start), str(end)) for start, end in pairs})
    if len(windows) != 1:
        found = ", ".join(f"{start}..{end}" for start, end in windows) or "none"
        msg = f"composites must share one compositing window; found {found}"
        raise ValueError(msg)
    ((start, end),) = windows
    return date.fromisoformat(start), date.fromisoformat(end)


def published_time_range(window: tuple[date, date]) -> tuple[datetime, datetime]:
    """Return the window as every item publishes it: ``start_datetime`` and ``end_datetime``.

    A window is inclusive calendar days; STAC wants instants with an exclusive
    end. That is the first day at midnight UTC and the midnight after the last
    day, and this is the only place the translation is written, so the tile
    items and the mosaic item cannot end a day apart.
    """
    start, end = window
    return (
        datetime.combine(start, time.min, tzinfo=UTC),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
    )


def _acquisition_spans(
    files: Sequence[CompositeFile],
    raster_info: Mapping[str, RasterInfo],
) -> dict[str, tuple[str, str]]:
    """First and last contributing acquisition per product, over its data layers.

    Read from ``FIRST_ACQUISITION_DATE`` and ``LAST_ACQUISITION_DATE``; the two
    polarizations of a product may differ by an acquisition, so the span is the
    union. ISO dates compare as strings.
    """
    spans: dict[str, tuple[str, str]] = {}
    for file in files:
        if file.qa is not None:
            continue
        tags = raster_info[file.asset_key].tags
        first, last = tags.get("FIRST_ACQUISITION_DATE"), tags.get("LAST_ACQUISITION_DATE")
        if first is None or last is None:
            continue
        seen = spans.get(file.product)
        spans[file.product] = (
            (first, last) if seen is None else (min(seen[0], first), max(seen[1], last))
        )
    return {product: spans[product] for product in PRODUCTS if product in spans}


def _nodata(value: float | None, data_type: str) -> float | int | str | None:
    """Encode a nodata value for STAC, which spells the non-finite ones out."""
    if value is None:
        return None
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return value if data_type.startswith("float") else int(value)


def _asset(
    file: CompositeFile,
    info: RasterInfo,
    *,
    title: str,
    roles: list[str],
    statistics: dict[str, float] | None = None,
) -> pystac.Asset:
    """Build one asset, describing its pixels entirely from the raster's own metadata."""
    require_valid(file, info)
    asset = pystac.Asset(
        # Relative to the item that owns it, which is what keeps the store
        # relocatable: moving the bucket edits one self href, not every asset.
        href=file.published_name,
        title=title,
        description=info.tags.get("LAYER_DESCRIPTION"),
        media_type=pystac.MediaType.COG,
        roles=roles,
    )
    band: dict[str, Any] = {"name": info.band_name} if info.band_name else {}
    band["data_type"] = info.data_type
    nodata = _nodata(info.nodata, info.data_type)
    if nodata is not None:
        band["nodata"] = nodata
    # A dimensionless band carries no unit at all rather than an empty string.
    if info.band_unit:
        band["unit"] = info.band_unit
    stats = dict(info.statistics)
    if statistics is not None:
        stats.update(statistics)
    if stats:
        band["statistics"] = stats
    asset.extra_fields["bands"] = [band]
    asset.extra_fields["raster:spatial_resolution"] = info.spatial_resolution
    asset.extra_fields["raster:bits_per_sample"] = info.bits_per_sample
    asset.extra_fields["file:byte_order"] = info.byte_order
    if info.size is not None:
        asset.extra_fields["file:size"] = info.size
    if info.checksum is not None:
        asset.extra_fields["file:checksum"] = info.checksum
    asset.extra_fields.update(tag_fields(info.tags))
    asset.extra_fields.update(standard_fields(info.tags))
    return asset


def _data_asset(
    file: CompositeFile,
    info: RasterInfo,
    valid_percent: float | None,
) -> pystac.Asset:
    is_coh = file.product.startswith("COH")
    if is_coh:
        baseline = info.tags.get("TEMPORAL_BASELINE_DAYS") or file.product.removeprefix("COH")
        quantity = f"{baseline}-day repeat-pass coherence"
    else:
        quantity = "RTC backscatter"
    asset = _asset(
        file,
        info,
        title=f"{quantity} {file.polarization}, {_method_label(file)}",
        roles=["data", "cloud-optimized"],
        statistics=None if valid_percent is None else {"valid_percent": valid_percent},
    )
    asset.extra_fields["sar:polarizations"] = [file.polarization]
    asset.extra_fields["sar:product_type"] = "COH" if is_coh else "RTC"
    return asset


def _qa_asset(file: CompositeFile, info: RasterInfo) -> pystac.Asset:
    layer = info.tags.get("QA_LAYER", f"QA-{file.qa}")
    return _asset(
        file,
        info,
        title=f"{layer} ({file.product} {file.polarization}, {_method_label(file)})",
        roles=["quality"],
    )


def _unanimous_raster_tag(
    item_id: str,
    raster_info: Mapping[str, RasterInfo],
    tag: str,
) -> str:
    """Return a required asset tag's unanimous value.

    Parameters
    ----------
    item_id
        Item identifier used in diagnostics.
    raster_info
        Metadata for every asset in the item.
    tag
        GeoTIFF tag to compare.

    Raises
    ------
    ValueError
        If no asset declares the tag or their values differ.
    """
    values = {info.tags[tag] for info in raster_info.values() if tag in info.tags}
    if len(values) == 1:
        return values.pop()
    if not values:
        msg = f"{item_id}: assets do not declare required tag {tag}"
        raise ValueError(msg)
    msg = f"{item_id}: assets disagree on {tag} ({sorted(values)})"
    raise ValueError(msg)


def _apply_processing_metadata(item: pystac.Item, raster_info: Mapping[str, RasterInfo]) -> None:
    """Hoist common processing metadata and declare the extensions in use.

    Both values classify the whole item, so they belong in searchable item
    properties and every asset must agree on them.
    """
    item_fields = {
        "PRODUCT_LEVEL": "processing:level",
        "DEM": "glace:dem",
    }
    for tag, property_name in item_fields.items():
        item.properties[property_name] = _unanimous_raster_tag(item.id, raster_info, tag)

    item.stac_extensions.extend([SAR_EXTENSION_URI, RASTER_EXTENSION_URI, FILE_EXTENSION_URI])
    fields = [key for asset in item.assets.values() for key in asset.extra_fields]
    if "processing:level" in item.properties or "processing:software" in fields:
        item.stac_extensions.append(PROCESSING_EXTENSION_URI)


def build_tile_item(
    tile: str,
    files: Sequence[CompositeFile],
    raster_info: Mapping[str, RasterInfo],
    *,
    glacier_fraction: float | None = None,
    glacier_fraction_buffered: float | None = None,
    valid_percents: dict[str, float] | None = None,
    version: str = DATASET_VERSION,
) -> pystac.Item:
    """Build the tile item for one (tile, year) from its parsed composite files.

    Parameters
    ----------
    tile
        MGRS 10-km tile code, e.g. ``"32TMS55"``.
    files
        Parsed composites of this tile (data + QA).
    raster_info
        Raster metadata per asset key, from the run manifest. Every
        statement the item makes about pixels comes from here.
    glacier_fraction, glacier_fraction_buffered
        RGI7 area fractions (plain and buffered), looked up in the scope's
        precomputed table; stored as ``glace:*`` properties. They are never
        recomputed per run, so one tile cannot carry different values in two
        years of the same dataset.
    valid_percents
        Valid-data percentage per data-asset key (e.g. ``{"coh12_vv": 98.2}``).
    version
        Published edition this item belongs to. Carried on the item as well as
        the collection, so that an item extracted on its own — a geoparquet row,
        a single item JSON — still says which edition it is.

    Raises
    ------
    KeyError
        If a discovered composite has no entry in ``raster_info``, or its entry
        lacks the window tags.
    ValueError
        If the tile's files do not all carry the same compositing window.
    """
    geobox = MGRSGeoBox.from_mgrs(tile)
    geometry, bbox = _tile_geometry(geobox)
    # The rasters' own tags carry the compositing window, the same for every
    # tile of a year, so every tile item of a year shares one time range and a
    # reader stacking them gets one time coordinate. Which acquisitions
    # contributed is per product and per tile, and comes from the tags too.
    window = composite_window(
        (
            raster_info[file.asset_key].tags["COMPOSITE_START_DATE"],
            raster_info[file.asset_key].tags["COMPOSITE_END_DATE"],
        )
        for file in files
    )
    start, end = published_time_range(window)
    year = window[0].year
    acquisitions = _acquisition_spans(files, raster_info)

    item = pystac.Item(
        id=f"{tile}-{year}",
        geometry=geometry,
        bbox=bbox,
        datetime=start + (end - start) / 2,
        properties={
            "start_datetime": datetime_to_str(start),
            "end_datetime": datetime_to_str(end),
            "constellation": "sentinel-1",
            "instruments": ["c-sar"],
            "sar:instrument_mode": "IW",
            "sar:frequency_band": "C",
            "sar:polarizations": sorted({f.polarization for f in files}),
            "version": version,
        },
    )
    item.stac_extensions.append(VERSION_EXTENSION_URI)
    _apply_common_metadata(item, geobox, tile)
    for product, (first, last) in acquisitions.items():
        prefix = "coh" if product.startswith("COH") else "rtc"
        item.properties[f"glace:{prefix}_first_acquisition"] = first
        item.properties[f"glace:{prefix}_last_acquisition"] = last
    if glacier_fraction is not None:
        item.properties["glace:glacier_fraction"] = round(glacier_fraction, 5)
    if glacier_fraction_buffered is not None:
        item.properties["glace:glacier_fraction_buffered"] = round(glacier_fraction_buffered, 5)

    valid_percents = valid_percents or {}
    for file in sorted(files, key=lambda f: f.asset_key):
        info = raster_info[file.asset_key]
        if file.qa is None:
            asset = _data_asset(file, info, valid_percents.get(file.asset_key))
        else:
            asset = _qa_asset(file, info)
        item.add_asset(file.asset_key, asset)

    _apply_processing_metadata(item, raster_info)
    _add_specification_link(item)
    return item


@dataclass(frozen=True)
class MosaicAsset:
    """One file published under an annual mosaic item.

    Parameters
    ----------
    href
        Path relative to the mosaic item, e.g. ``coh12_vv.tif``.
    title
        Asset title.
    roles
        STAC asset roles.
    media_type
        Media type of the file.
    size
        File size in bytes, if known.
    band
        Single-band description, for a raster asset.
    extra
        Further asset fields, such as the provenance tags carried over from the
        tiles the mosaic was warped from.
    """

    href: str
    title: str
    roles: list[str]
    media_type: str = pystac.MediaType.COG
    size: int | None = None
    band: dict[str, Any] | None = None
    extra: dict[str, Any] = dc_field(default_factory=dict)

    def to_asset(self) -> pystac.Asset:
        """Render this record as a pystac asset."""
        asset = pystac.Asset(
            href=self.href,
            title=self.title,
            media_type=self.media_type,
            roles=list(self.roles),
        )
        if self.band is not None:
            asset.extra_fields["bands"] = [self.band]
        if self.size is not None:
            asset.extra_fields["file:size"] = self.size
        asset.extra_fields.update(self.extra)
        return asset


def _band_title(asset_key: str) -> str:
    """Human-readable name of one asset key, e.g. ``COH12 VH observation count``."""
    product, pol, *qa = asset_key.upper().split("_")
    quantity = "12-day repeat-pass coherence" if product.startswith("COH") else "RTC backscatter"
    if not qa:
        return f"{quantity} {pol}"
    role = "observation count" if qa[-1] == "NUM" else "composite quality metric"
    return f"{quantity} {pol}, {role}"


def build_mosaic_item(
    scope: Scope,
    year: int,
    *,
    geometry: dict[str, Any],
    bbox: Sequence[float],
    start: datetime,
    end: datetime,
    assets: Mapping[str, MosaicAsset],
    pmtiles: Mapping[str, str],
    build_fingerprint: str | None = None,
    version: str = DATASET_VERSION,
) -> pystac.Item:
    """Build the annual-mosaic item for one year.

    Parameters
    ----------
    scope
        Scope being published; supplies the item id and the canonical grid.
    year
        Composite year.
    geometry, bbox
        WGS84 data footprint of the year.
    start, end
        The year's time range as the tile items publish it: the window's first
        day, and the midnight after its last day.
    assets
        Written mosaic COGs and per-year documents, keyed by asset key.
    pmtiles
        Href per PMTiles archive, keyed by layer id. Attached as
        ``rel="pmtiles"`` links rather than assets: a pre-styled pyramid is a
        rendering, not a measurement.
    build_fingerprint
        Fingerprint of the inputs this year was built from, so a store can be
        checked against them without re-deriving anything.
    version
        Published edition; see :data:`DATASET_VERSION`.
    """
    grid = scope.analysis_grid
    item = pystac.Item(
        id=scope.mosaic_item_id(year),
        geometry=geometry,
        bbox=list(bbox),
        datetime=start + (end - start) / 2,
        properties={
            "start_datetime": datetime_to_str(start),
            "end_datetime": datetime_to_str(end),
            "constellation": "sentinel-1",
            "instruments": ["c-sar"],
            "sar:instrument_mode": "IW",
            "sar:frequency_band": "C",
            "sar:polarizations": list(POLARIZATIONS),
            "proj:code": grid.crs,
            "proj:shape": list(grid.shape),
            "proj:transform": list(grid.transform)[:6],
            "version": version,
        },
    )
    if build_fingerprint is not None:
        item.properties["glace:build_fingerprint"] = build_fingerprint
    item.stac_extensions.extend(
        [
            SAR_EXTENSION_URI,
            PROJECTION_EXTENSION_URI,
            RASTER_EXTENSION_URI,
            FILE_EXTENSION_URI,
            VERSION_EXTENSION_URI,
        ],
    )
    for key, mosaic in sorted(assets.items()):
        item.add_asset(key, mosaic.to_asset())
    if pmtiles:
        item.stac_extensions.append(WEB_MAP_LINKS_EXTENSION_URI)
    for layer_id, href in sorted(pmtiles.items()):
        item.add_link(
            pystac.Link(
                rel=PMTILES_LINK_REL,
                target=href,
                media_type="application/vnd.pmtiles",
                title=f"{layer_id} web-map tiles",
            ),
        )
    _add_specification_link(item)
    return item


def extent_from_items(items: Sequence[pystac.Item]) -> pystac.Extent:
    """Spatial and temporal extent covering every item."""
    bounds = [item.bbox for item in items if item.bbox is not None]
    extent_bbox = [
        min(b[0] for b in bounds),
        min(b[1] for b in bounds),
        max(b[2] for b in bounds),
        max(b[3] for b in bounds),
    ]
    spatial = pystac.SpatialExtent([[float(v) for v in extent_bbox]])
    starts = [datetime.fromisoformat(item.properties["start_datetime"]) for item in items]
    ends = [datetime.fromisoformat(item.properties["end_datetime"]) for item in items]
    temporal = pystac.TemporalExtent([[min(starts), max(ends)]])
    return pystac.Extent(spatial=spatial, temporal=temporal)


_PRODUCERS = [
    pystac.Provider(
        name="University of Zurich, Department of Geography",
        roles=[pystac.ProviderRole.PRODUCER, pystac.ProviderRole.PROCESSOR],
    ),
]
_KEYWORDS = ["Sentinel-1", "SAR", "InSAR coherence", "backscatter", "glaciers"]
