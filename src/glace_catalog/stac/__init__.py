"""Building the GLACE store: its names, its documents and the tree they live in.

The package splits along what each part is responsible for:

- :mod:`~glace_catalog.stac.naming` — the archive's filenames and the
  published names they map onto.
- :mod:`~glace_catalog.stac.tags` — what a composite says about itself, and
  whether that is enough to publish.
- :mod:`~glace_catalog.stac.items` — the items and collections themselves.
- :mod:`~glace_catalog.stac.store` — the layout on object storage, the run
  manifest, the server-side copy and the build state.
- :mod:`~glace_catalog.stac.save` — writing the catalogue out with relative
  hrefs, and the geoparquet indexes beside it.
"""

from glace_catalog.stac.items import (
    DATASET_VERSION,
    MosaicAsset,
    build_mosaic_item,
    build_tile_item,
    composite_window,
    published_time_range,
)
from glace_catalog.stac.naming import (
    ASSET_KEYS,
    POLARIZATIONS,
    PRODUCTS,
    QA_METRICS,
    VIZ_SUFFIX,
    CompositeFile,
    by_asset_key,
    check_completeness,
    data_key,
    discover_composites,
    parse_composite_name,
    require_unique_composites,
    viz_filename,
    viz_key,
)
from glace_catalog.stac.save import (
    PORTOLAN_SCHEMA,
    UPathStacIO,
    concat_indexes,
    export_geoparquet,
    index_from_items,
    is_tile_item,
    item_dict,
    item_prefix,
    item_year,
    mosaic_item_link,
    write_stac_geoparquet,
    year_catalog_document,
    year_child_link,
)
from glace_catalog.stac.store import (
    BuildState,
    StoreLayout,
    copy_composites,
    fingerprint,
    manifest_composites,
    manifest_digest,
    manifest_raster_info,
    manifest_valid_percents,
    multihash_sha256,
    multihash_sha256_file,
    read_manifest,
    resolve_steps,
    verify_manifest,
)
from glace_catalog.stac.tags import (
    PROVENANCE_EXPECTATION,
    PROVENANCE_TAGS,
    BandStatistics,
    RasterInfo,
    RasterProblems,
    read_raster_info,
    survey_tags,
)

__all__ = [
    "ASSET_KEYS",
    "DATASET_VERSION",
    "POLARIZATIONS",
    "PORTOLAN_SCHEMA",
    "PRODUCTS",
    "PROVENANCE_EXPECTATION",
    "PROVENANCE_TAGS",
    "QA_METRICS",
    "VIZ_SUFFIX",
    "BandStatistics",
    "BuildState",
    "CompositeFile",
    "MosaicAsset",
    "RasterInfo",
    "RasterProblems",
    "StoreLayout",
    "UPathStacIO",
    "build_mosaic_item",
    "build_tile_item",
    "by_asset_key",
    "check_completeness",
    "composite_window",
    "concat_indexes",
    "copy_composites",
    "data_key",
    "discover_composites",
    "export_geoparquet",
    "fingerprint",
    "index_from_items",
    "is_tile_item",
    "item_dict",
    "item_prefix",
    "item_year",
    "manifest_composites",
    "manifest_digest",
    "manifest_raster_info",
    "manifest_valid_percents",
    "mosaic_item_link",
    "multihash_sha256",
    "multihash_sha256_file",
    "parse_composite_name",
    "published_time_range",
    "read_manifest",
    "read_raster_info",
    "require_unique_composites",
    "resolve_steps",
    "survey_tags",
    "verify_manifest",
    "viz_filename",
    "viz_key",
    "write_stac_geoparquet",
    "year_catalog_document",
    "year_child_link",
]
