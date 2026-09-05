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
)
from glace_catalog.stac.naming import (
    ASSET_KEYS,
    POLARIZATIONS,
    PRODUCTS,
    QA_METRICS,
    CompositeFile,
    check_completeness,
    discover_composites,
    parse_composite_name,
    require_unique_composites,
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
    year_catalog_prose,
    year_child_link,
)
from glace_catalog.stac.store import (
    BuildState,
    StoreLayout,
    build_manifest_frame,
    copy_composites,
    fingerprint,
    list_objects,
    manifest_composites,
    manifest_digest,
    manifest_raster_info,
    manifest_valid_percents,
    read_manifest,
    resolve_steps,
    verify_manifest,
    write_manifest,
)
from glace_catalog.stac.tags import (
    PROVENANCE_EXPECTATION,
    PROVENANCE_TAGS,
    RasterInfo,
    RasterProblems,
    collect_raster_info,
    read_raster_info,
    survey_tags,
    valid_percent_from_qa,
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
    "BuildState",
    "CompositeFile",
    "MosaicAsset",
    "RasterInfo",
    "RasterProblems",
    "StoreLayout",
    "UPathStacIO",
    "build_manifest_frame",
    "build_mosaic_item",
    "build_tile_item",
    "check_completeness",
    "collect_raster_info",
    "concat_indexes",
    "copy_composites",
    "discover_composites",
    "export_geoparquet",
    "fingerprint",
    "index_from_items",
    "is_tile_item",
    "item_dict",
    "item_prefix",
    "item_year",
    "list_objects",
    "manifest_composites",
    "manifest_digest",
    "manifest_raster_info",
    "manifest_valid_percents",
    "mosaic_item_link",
    "parse_composite_name",
    "read_manifest",
    "read_raster_info",
    "require_unique_composites",
    "resolve_steps",
    "survey_tags",
    "valid_percent_from_qa",
    "verify_manifest",
    "write_manifest",
    "write_stac_geoparquet",
    "year_catalog_document",
    "year_catalog_prose",
    "year_child_link",
]
