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
from datetime import UTC, datetime, timedelta
import math
from typing import TYPE_CHECKING, Any

from freezebase.mgrs import MGRSGeoBox
import pystac
from pystac.utils import datetime_to_str

from glace_catalog.stac.naming import (
    ASSET_KEYS,
    IGNORE_VALUE,
    LABEL_SOURCES,
    POLARIZATIONS,
    PRODUCTS,
)
from glace_catalog.stac.tags import (
    METADATA_VOCABULARY_NOTE,
    SPECIFICATION_URL,
    require_valid,
    standard_fields,
    tag_fields,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from glace_catalog.scopes import Scope
    from glace_catalog.stac.naming import CompositeFile, LabelFile, LabelSource
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

MANIFEST_HREF = "../manifest.parquet"
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
    if statistics is not None:
        band["statistics"] = statistics
    asset.extra_fields["bands"] = [band]
    asset.extra_fields["raster:spatial_resolution"] = info.spatial_resolution
    asset.extra_fields["raster:bits_per_sample"] = info.bits_per_sample
    asset.extra_fields["file:byte_order"] = info.byte_order
    if info.size is not None:
        asset.extra_fields["file:size"] = info.size
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
        roles=["data", "coherence" if is_coh else "backscatter"],
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
        roles=["metadata", "quality"],
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
        Raster metadata per asset key, from :func:`collect_raster_info`. Every
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
        If a discovered composite has no entry in ``raster_info``.
    """
    geobox = MGRSGeoBox.from_mgrs(tile)
    geometry, bbox = _tile_geometry(geobox)
    starts = {f.product: f.start for f in files if f.qa is None}
    ends = {f.product: f.end for f in files if f.qa is None}
    start = min(f.start for f in files)
    end = max(f.end for f in files) + timedelta(days=1)
    year = start.year

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
    for product in PRODUCTS:
        if product in starts:
            prefix = "coh" if product.startswith("COH") else "rtc"
            item.properties[f"glace:{prefix}_start"] = starts[product].date().isoformat()
            item.properties[f"glace:{prefix}_end"] = ends[product].date().isoformat()
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
    # The year's manifest is the run's own record of these files. Linking it
    # from every tile item means a consumer who lands here can reach the
    # provenance without knowing the mosaics collection exists.
    item.add_link(
        pystac.Link(
            rel="describedby",
            target=MANIFEST_HREF,
            media_type="application/vnd.apache.parquet",
            title="Composite run manifest",
        ),
    )

    return item


def build_label_item(label: LabelFile) -> pystac.Item:
    """Build a label item (glacier mask) for one tile.

    The item datetime range is the acquisition year for year-split sources, and
    the inventory's overall source imagery window otherwise.
    """
    source = LABEL_SOURCES[label.source]
    geobox = MGRSGeoBox.from_mgrs(label.tile)
    geometry, bbox = _tile_geometry(geobox)
    if label.year is None:
        start, end = source.start, source.end
    else:
        start = datetime(label.year, 1, 1, tzinfo=UTC)
        end = datetime(label.year, 12, 31, tzinfo=UTC)

    item = pystac.Item(
        id=label.item_id,
        geometry=geometry,
        bbox=bbox,
        datetime=start + (end - start) / 2,
        properties={
            "start_datetime": datetime_to_str(start),
            "end_datetime": datetime_to_str(end),
            "glace:label_source": source.key,
        },
    )
    if label.year is not None:
        item.properties["glace:year_acq"] = label.year
    _apply_common_metadata(item, geobox, label.tile)

    asset = pystac.Asset(
        href=str(label.path),
        title=f"Glacier mask: {source.title}",
        media_type=pystac.MediaType.COG,
        roles=["labels", "data"],
    )
    band: dict[str, Any] = {"name": "glacier_mask", "data_type": "uint8"}
    classes = [
        {"value": 0, "name": "background", "description": "No glacier"},
        {"value": 1, "name": "glacier", "description": f"Glacier per {source.title}"},
    ]
    if source.year_split:
        band["nodata"] = IGNORE_VALUE
        classes.append(
            {
                "value": IGNORE_VALUE,
                "name": "ignore",
                "description": (
                    "Glacier mapped in a different acquisition year, buffered; "
                    "exclude from training and evaluation"
                ),
                "nodata": True,
            },
        )
    asset.extra_fields["bands"] = [band]
    asset.extra_fields["raster:spatial_resolution"] = 10.0
    asset.extra_fields["raster:bits_per_sample"] = 8
    asset.extra_fields["classification:classes"] = classes
    item.add_asset("labels", asset)
    item.stac_extensions.extend([CLASSIFICATION_EXTENSION_URI, RASTER_EXTENSION_URI])
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


def _item_asset_definition(
    asset_key: str,
    *,
    resolution: float,
    title: str,
    roles: list[str],
) -> dict[str, Any]:
    """One ``item_assets`` entry: what every item's asset under this key holds."""
    is_num = asset_key.endswith("_qa_num")
    band: dict[str, Any] = {
        "name": asset_key,
        "data_type": "uint16" if is_num else "float32",
        "nodata": 0 if is_num else "nan",
    }
    return {
        "type": pystac.MediaType.COG,
        "title": title,
        "roles": roles,
        "raster:spatial_resolution": resolution,
        "bands": [band],
    }


def tile_item_assets() -> dict[str, dict[str, Any]]:
    """``item_assets`` of the tiles collection: twelve keys on the 10 m MGRS grid."""
    return {
        key: _item_asset_definition(
            key,
            resolution=10.0,
            title=f"{_band_title(key)}, LRW composite",
            roles=["metadata", "quality"] if "_qa_" in key else ["data"],
        )
        for key in ASSET_KEYS
    }


def mosaic_item_assets(resolution: float) -> dict[str, dict[str, Any]]:
    """``item_assets`` of the mosaics collection: the same keys, different truths.

    The keys are identical to the tiles collection's, which is exactly why the
    two are separate collections: within one, a key could not name both a 10 m
    measurement and its annual summary.

    A QA-NUM mosaic is a *mean* of contributing observations rather than a count,
    so it is float32 with a NaN nodata where the tile is uint16 with a zero one.
    """
    assets = {
        key: _item_asset_definition(
            key,
            resolution=resolution,
            title=f"{_band_title(key)}, annual mosaic",
            roles=["metadata", "quality"] if "_qa_" in key else ["data", "overview"],
        )
        for key in ASSET_KEYS
    }
    for key, definition in assets.items():
        if key.endswith("_qa_num"):
            definition["bands"] = [
                {"name": key, "data_type": "float32", "nodata": "nan"},
            ]
    assets["manifest"] = {
        "type": "application/vnd.apache.parquet",
        "title": "Composite run manifest",
        "roles": ["metadata"],
    }
    assets["config"] = {
        "type": "application/yaml",
        "title": "Composite run configuration",
        "roles": ["metadata"],
    }
    return assets


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
        Composite window the year's tiles span.
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
    grid = scope.grid
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


def _check_edition(collection_id: str, items: Sequence[pystac.Item], version: str) -> None:
    """Reject a collection whose items do not all belong to one edition.

    Raises
    ------
    ValueError
        If the items carry more than one ``version``. A collection spanning two
        editions could not describe its own pixels, since an edition is exactly
        what fixes how they are read.
    """
    editions = {item.properties.get("version") for item in items}
    if editions != {version}:
        msg = (
            f"collection {collection_id!r} is version {version!r} but its items carry "
            f"{sorted(str(edition) for edition in editions)}. Rebuild the whole dataset "
            f"for one edition rather than mixing."
        )
        raise ValueError(msg)


def build_tiles_collection(
    scope: Scope,
    items: Sequence[pystac.Item] = (),
    *,
    extent: pystac.Extent | None = None,
    version: str = DATASET_VERSION,
) -> pystac.Collection:
    """Assemble the per-tile items into the tiles collection.

    Parameters
    ----------
    scope
        Scope being published.
    items
        Tile items, from :func:`build_tile_item`. May be empty when ``extent``
        is given: a store with eleven years of Alpine tiles has 24 000 items, and
        the collection document needs their envelope rather than their contents.
    extent
        Extent of the collection. Derived from ``items`` when absent.
    version
        Published edition; see :data:`DATASET_VERSION`.

    Item links are **not** added here: the items are written by a separate
    command, so the collection links them by explicit relative href through
    :func:`~glace_catalog.stac.save.add_item_links` rather than by holding
    resolved objects. Adding both would leave an unresolved link with a null
    href beside every real one.
    """
    if items:
        _check_edition(scope.tiles_collection_id, items, version)
    collection = pystac.Collection(
        id=scope.tiles_collection_id,
        title=f"GLACE tiles: Sentinel-1 LRW composites, {scope.title}",
        description=(
            "Annual summer composites of Sentinel-1 12-day repeat-pass InSAR coherence "
            "(COH12) and radiometrically terrain corrected backscatter (RTC) over "
            f"{scope.title}, locally resolution weighted (LRW), on the 10 km MGRS grid "
            "at 10 m resolution, with per-tile QA layers (observation count, composite "
            f"quality metric). {METADATA_VOCABULARY_NOTE}"
        ),
        extent=extent if extent is not None else extent_from_items(items),
        keywords=[*_KEYWORDS, scope.title],
        providers=_PRODUCERS,
    )
    collection.item_assets = {
        key: pystac.ItemAssetDefinition(definition)
        for key, definition in tile_item_assets().items()
    }
    collection.extra_fields["version"] = version
    collection.stac_extensions.append(VERSION_EXTENSION_URI)
    _add_specification_link(collection)
    return collection


def build_mosaics_collection(
    scope: Scope,
    items: Sequence[pystac.Item] = (),
    *,
    extent: pystac.Extent | None = None,
    version: str = DATASET_VERSION,
) -> pystac.Collection:
    """Assemble the annual mosaic items into the mosaics collection.

    Parameters
    ----------
    scope
        Scope being published; supplies the canonical grid the mosaics sit on.
    items
        Mosaic items, from :func:`build_mosaic_item`. May be empty when
        ``extent`` is given.
    extent
        Extent of the collection. Derived from ``items`` when absent.
    version
        Published edition; see :data:`DATASET_VERSION`.

    Item links are **not** added here: the items are written by a separate
    command, so the collection links them by explicit relative href through
    :func:`~glace_catalog.stac.save.add_item_links` rather than by holding
    resolved objects. Adding both would leave an unresolved link with a null
    href beside every real one.
    """
    if items:
        _check_edition(scope.mosaics_collection_id, items, version)
    grid = scope.grid
    collection = pystac.Collection(
        id=scope.mosaics_collection_id,
        title=f"GLACE mosaics: annual Sentinel-1 composites, {scope.title}",
        description=(
            f"One mosaic per year of the GLACE {scope.title} composites, warped from the "
            f"10 m MGRS tiles onto the WebMercatorQuad grid at zoom 13 "
            f"({grid.resolution:.3f} m). Every year sits on the same fixed grid, so the "
            "series stacks and differences without an alignment step, and the COGs' "
            "internal overview levels land on the tile-matrix zoom levels below 13. "
            "Values are linear throughout; the observation-count layers become a mean "
            f"rather than a count under averaging. {METADATA_VOCABULARY_NOTE}"
        ),
        extent=extent if extent is not None else extent_from_items(items),
        keywords=[*_KEYWORDS, scope.title, "mosaic"],
        providers=_PRODUCERS,
    )
    collection.item_assets = {
        key: pystac.ItemAssetDefinition(definition)
        for key, definition in mosaic_item_assets(grid.resolution).items()
    }
    collection.extra_fields["version"] = version
    collection.stac_extensions.append(VERSION_EXTENSION_URI)
    _add_specification_link(collection)
    return collection


def build_label_collection(
    items: Sequence[pystac.Item],
    source: LabelSource,
) -> pystac.Collection:
    """Assemble label items of one inventory into a project-internal collection.

    ``items`` gives the extent; their links are added by the caller through
    :func:`~glace_catalog.stac.save.add_item_links`.
    """
    split_note = (
        " Masks are split by acquisition year: each holds the glaciers mapped in that "
        f"year, with the other years' outlines buffered and burned as {IGNORE_VALUE} "
        "(ignore)."
        if source.year_split
        else ""
    )
    return pystac.Collection(
        id=source.collection_id,
        title=f"Glacier masks from the {source.title}",
        description=(
            f"Per-tile glacier masks rasterized from the {source.title} onto the 10 km "
            f"MGRS grid at 10 m resolution. {source.description}{split_note} "
            "Project-internal training and validation labels; not part of the "
            "published GLACE dataset."
        ),
        extent=extent_from_items(items),
        keywords=["glacier inventory", "labels", "Alps"],
    )
