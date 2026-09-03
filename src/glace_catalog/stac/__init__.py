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
    build_label_collection,
    build_label_item,
    build_mosaic_item,
    build_mosaics_collection,
    build_tile_item,
    build_tiles_collection,
    mosaic_item_assets,
    tile_item_assets,
)
from glace_catalog.stac.naming import (
    ASSET_KEYS,
    IGNORE_VALUE,
    LABEL_SOURCES,
    POLARIZATIONS,
    PRODUCTS,
    QA_METRICS,
    CompositeFile,
    LabelFile,
    LabelSource,
    check_completeness,
    discover_composites,
    discover_labels,
    parse_composite_name,
    require_unique_composites,
)
from glace_catalog.stac.save import (
    UPathStacIO,
    build_root_catalog,
    concat_indexes,
    export_geoparquet,
    index_from_items,
    item_dict,
    save_store_catalog,
    write_stac_geoparquet,
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
    METADATA_VOCABULARY_NOTE,
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
    "IGNORE_VALUE",
    "LABEL_SOURCES",
    "METADATA_VOCABULARY_NOTE",
    "POLARIZATIONS",
    "PRODUCTS",
    "PROVENANCE_EXPECTATION",
    "PROVENANCE_TAGS",
    "QA_METRICS",
    "BuildState",
    "CompositeFile",
    "LabelFile",
    "LabelSource",
    "MosaicAsset",
    "RasterInfo",
    "RasterProblems",
    "StoreLayout",
    "UPathStacIO",
    "build_label_collection",
    "build_label_item",
    "build_manifest_frame",
    "build_mosaic_item",
    "build_mosaics_collection",
    "build_root_catalog",
    "build_tile_item",
    "build_tiles_collection",
    "check_completeness",
    "collect_raster_info",
    "concat_indexes",
    "copy_composites",
    "discover_composites",
    "discover_labels",
    "export_geoparquet",
    "fingerprint",
    "index_from_items",
    "item_dict",
    "list_objects",
    "manifest_composites",
    "manifest_digest",
    "manifest_raster_info",
    "manifest_valid_percents",
    "mosaic_item_assets",
    "parse_composite_name",
    "read_manifest",
    "read_raster_info",
    "require_unique_composites",
    "resolve_steps",
    "save_store_catalog",
    "survey_tags",
    "tile_item_assets",
    "valid_percent_from_qa",
    "verify_manifest",
    "write_manifest",
    "write_stac_geoparquet",
]
