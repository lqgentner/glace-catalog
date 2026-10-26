"""Write the generated item documents and the item mirrors beside them.

The published tree holds two kinds of document:

- **Rendered documents**: the root ``catalog.json``, both ``collection.json``
  files, and the root and collection ``README.md`` and ``AGENTS.md``. They are
  rendered from ``metadata/`` and tracked in git. ``finalize-catalog`` adds the
  links and per-year assets of the published years and keeps the prose.
- **Generated documents**: every ``item.json``, every year sub-catalog and both
  ``items.parquet`` files. This module writes them from a run's outputs, and
  they live only in the built store.

Both ``items.parquet`` files follow the GeoParquet rules on top of
stac-geoparquet: spatially ordered rows, a ``bbox`` covering column with
per-row-group statistics, and bounded row groups. :func:`_write_mirror` writes
all of them.

Structural links are relative, so the same tree is valid in the repository, in
a preview and in production. Item asset hrefs are relative too
(``./coh12_vv.tif``), and the item mirror carries absolute asset and link
hrefs.
"""

from __future__ import annotations

import json
import logging
import posixpath
from typing import TYPE_CHECKING, Any, override
from urllib.parse import urljoin

import geopandas as gpd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pystac
import shapely
import stac_geoparquet.arrow

# The private metadata builder behind `stac_geoparquet.arrow.to_parquet`.
# Reusing it keeps the WGS84 PROJJSON and the covering declaration identical to
# the library's. The mirrors are sorted and grouped before writing, so
# `_write_mirror` writes the file itself.
from stac_geoparquet.arrow._to_parquet import create_parquet_metadata
from upath import UPath

# Import the module, not the name: metadata imports the naming module through
# this package, so with some import orders this package is still initializing
# when metadata loads. Looking up `metadata.vocabulary` at call time works for
# every order.
from glace_catalog import metadata
from glace_catalog.stac.store import MOSAICS_DIR, TILES_DIR

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)


class UPathStacIO(pystac.stac_io.DefaultStacIO):
    """pystac IO that reads through ``UPath``.

    Local and ``s3://`` stores are read the same way during validation, and
    relative child links resolve in both.

    pystac passes hrefs as plain strings, which carry no S3 profile or
    endpoint, so this class holds the storage options and attaches them to
    remote paths.

    Parameters
    ----------
    storage_options
        Options to attach to remote paths, typically
        ``layout.root.storage_options``.
    """

    def __init__(self, storage_options: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        self._storage_options = dict(storage_options or {})

    def _path(self, href: str) -> UPath:
        """Rebuild a href as a configured path."""
        if self._storage_options and "://" in href:
            return UPath(href, **self._storage_options)
        return UPath(href)

    @override
    def read_text_from_href(self, href: str) -> str:
        """Read one document, from a local path or an object store."""
        return self._path(href).read_text()


PORTOLAN_SCHEMA = "https://schemas.portolan-sdi.org/portolan/v0.2.0/schema.json"
"""Portolan profile version every generated catalog declares.

The versioned schema URI is the only marker of the specification version, and
every generated catalog must match the tracked ``catalog.json``; rashid warns
about a mismatch with the root (``PTL-CNF-002``). Change the version here.
"""

Document = dict[str, Any]
"""One STAC document as read and written: a JSON object.

Values vary in type (link lists, nested extents, numbers), and the code here
reads them by key.
"""


def dump_document(document: Document) -> str:
    """Serialize one document as the store publishes it: indented UTF-8, one final newline."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


ITEM_NAME = "item.json"
CATALOG_NAME = "catalog.json"


def item_year(item: pystac.Item) -> str:
    """Year an item belongs to, as it appears in the path."""
    return str(item.properties["start_datetime"][:4])


def is_tile_item(item: pystac.Item) -> bool:
    """Whether an item is a per-tile composite rather than an annual mosaic."""
    return item.properties.get("glace:mgrs_tile") is not None


def item_prefix(item: pystac.Item) -> str:
    """Path from the store root to an item's own directory.

    ``tiles/{year}/{tile}`` or ``mosaics/{year}``. Each collection owns the
    subtree holding its items, as Portolan requires, and year sub-catalogs sit
    inside it.
    """
    year = item_year(item)
    if is_tile_item(item):
        return f"{TILES_DIR}/{year}/{item.properties['glace:mgrs_tile']}"
    return f"{MOSAICS_DIR}/{year}"


def item_dict(item: pystac.Item) -> Document:
    """Serialize an item without resolving its links.

    Published items have only relative links, which the store writes itself,
    so pystac's href transform (which needs the root loaded) is skipped.
    """
    return item.to_dict(include_self_link=False, transform_hrefs=False)


def write_stac_geoparquet(payloads: Sequence[Document], out_path: Path | UPath) -> None:
    """Write item dicts as a stac-geoparquet index.

    ``stac_geoparquet`` builds the rows: it converts the geometries to WKB,
    derives the ``bbox`` covering column, and creates the GeoParquet schema
    metadata that declares both, so readers see a geometry column.
    :func:`_write_mirror` then writes the file, spatially ordered and grouped.
    """
    reader = stac_geoparquet.arrow.parse_stac_items_to_arrow(list(payloads))
    table = reader.read_all()
    table = table.replace_schema_metadata(
        create_parquet_metadata(
            table.schema,
            schema_version=stac_geoparquet.arrow.DEFAULT_PARQUET_SCHEMA_VERSION,
        ),
    )
    _write_mirror(table, out_path)
    logger.info("Geoparquet index with %d items saved to %s", len(payloads), out_path)


def year_index_document(item: pystac.Item) -> Document:
    """One row of a year's tile index: the published item, with hrefs from the store root.

    The row carries the item document as published, collection and structural
    links included. Its relative asset and link hrefs are rewritten from the
    item's directory to the store root, so :func:`concat_indexes` can make them
    absolute without knowing where each item lives.
    """
    payload = item_document(item)
    directory = item_prefix(item)
    for entry in [*payload.get("assets", {}).values(), *payload.get("links", [])]:
        href = entry.get("href")
        if href is not None and "://" not in href:
            entry["href"] = posixpath.normpath(posixpath.join(directory, href))
    return payload


def _rebase_hrefs(table: pa.Table, base_url: str) -> pa.Table:
    """Rewrite an index's root-relative asset and link hrefs onto an absolute base URL."""
    base = base_url.rstrip("/")
    assets = table.column("assets").to_pylist()
    links = table.column("links").to_pylist()
    entries = [
        # A struct column holds every key any item has; a tile without one of
        # them (a single-polarization tile) carries a null there.
        *[asset for row in assets if row is not None for asset in row.values()],
        *[link for row in links if row is not None for link in row],
    ]
    for entry in entries:
        if entry is None:
            continue
        href = entry.get("href")
        if href is not None and "://" not in href:
            entry["href"] = f"{base}/{href.lstrip('/')}"
    for name, rows in (("assets", assets), ("links", links)):
        index = table.schema.get_field_index(name)
        field = table.schema.field(index)
        table = table.set_column(index, field, pa.array(rows, type=field.type))
    return table


def concat_indexes(
    parts: Iterable[Path | UPath],
    out_path: Path | UPath,
    *,
    base_url: str,
) -> int:
    """Concatenate per-year indexes into one, rebasing their hrefs onto ``base_url``.

    The per-year files hold root-relative hrefs, so they stay valid when the
    store moves. The store-level index holds absolute hrefs, because its
    readers have no root to resolve against. Only ``finalize-catalog`` writes
    them, from ``public_base``, so moving the store takes one finalize run.

    Returns
    -------
    int
        Number of rows written.
    """
    tables = []
    for part in parts:
        with UPath(part).open("rb") as handle:
            tables.append(pq.read_table(handle))
    if not tables:
        msg = "No per-year indexes to concatenate."
        raise ValueError(msg)
    concatenated = pa.concat_tables(tables, promote_options="permissive")
    combined = _rebase_hrefs(concatenated, base_url)
    # Keep the first table's schema metadata, which holds the GeoParquet
    # geometry declaration.
    combined = combined.replace_schema_metadata(tables[0].schema.metadata)
    # The mirror writer orders the rows spatially across all years.
    _write_mirror(combined, out_path)
    logger.info("Wrote %s with %d rows", out_path, combined.num_rows)
    return combined.num_rows


def index_from_items(
    items: Sequence[pystac.Item],
    out_path: Path | UPath,
    *,
    base_url: str,
    hrefs_relative_to: str = "",
) -> int:
    """Write a stac-geoparquet index straight from items, with absolute asset and link hrefs.

    Used for the mosaics index, which has one row per year and is built
    directly from the mosaic items.

    Parameters
    ----------
    items
        Items to index.
    out_path
        Destination parquet file.
    base_url
        Absolute base the relative hrefs are resolved against.
    hrefs_relative_to
        Path from the store root to the item, prepended to each asset href
        before rebasing. Item hrefs are relative to their own directory.

    Returns
    -------
    int
        Number of rows written.
    """
    base = base_url.rstrip("/")
    dicts = []
    for item in items:
        payload = item_dict(item)
        # urljoin resolves `../` segments, so every href is a clean URL.
        item_base = f"{base}/{hrefs_relative_to or item_prefix(item)}/"
        for entry in [*payload.get("assets", {}).values(), *payload.get("links", [])]:
            href = entry.get("href")
            if href is not None and "://" not in href:
                entry["href"] = urljoin(item_base, href)
        dicts.append(payload)
    write_stac_geoparquet(dicts, out_path)
    return len(dicts)


def _mgrs_tile(item: pystac.Item) -> str:
    """MGRS tile an item covers, as a sort key over tile items."""
    return str(item.properties["glace:mgrs_tile"])


HILBERT_LEVEL = 16
"""Bits per axis of the Hilbert curve the mirror rows are sorted along.

An order-16 curve divides each axis into 65 536 steps, a few metres over the
Alps' extent. That is far finer than the 10 km tiles, so every tile has its own
key.
"""

MIRROR_ROW_GROUP_SIZE = 2048
"""Rows per row group in every mirror this module writes.

Readers skip row groups by their min/max statistics, which helps only when a
file has several groups; rashid's ordering metrics need at least five. At 2 048 rows,
the store-level mirror (about 2 200 tiles over twelve years, about 26 500
rows) has thirteen groups.
"""

MIRROR_COMPRESSION_LEVEL = 15
"""zstd level of every mirror, written without dictionary encoding.

Measured on the Alpine tile mirror: Snappy, pyarrow's default, gave 36.1 MB;
zstd 15 gave 21.4 MB, and 18.4 MB without dictionaries, which pyarrow
otherwise tries on every column, including the mostly unique hrefs and
checksums. Reads were as fast; the write takes about 8 s.
"""

_BBOX_CORNERS = ("xmin", "ymin", "xmax", "ymax")

_BBOX_STRUCT = pa.struct([(corner, pa.float64()) for corner in _BBOX_CORNERS])


def _with_covering_bbox(table: pa.Table) -> pa.Table:
    """Return ``table`` with a float64 ``bbox`` struct declared as the covering.

    ``stac_geoparquet`` writes the column and the
    ``geo.columns.geometry.covering.bbox`` declaration that names it; this
    keeps both and casts the column to float64. A batch whose bboxes are all
    whole numbers arrives as ``int64``, and all years need the same type to be
    concatenated into the store-level mirror.

    Raises
    ------
    ValueError
        If the table has no declared covering column, which the row groups'
        spatial statistics (``PTL-DAT-007``) require.
    """
    index = table.schema.get_field_index("bbox")
    geo = json.loads((table.schema.metadata or {}).get(b"geo", b"{}"))
    columns = geo.get("columns", {})
    covering = columns.get(geo.get("primary_column"), {}).get("covering", {}).get("bbox")
    if index < 0 or not isinstance(covering, dict):
        msg = (
            "The mirror carries no bbox covering column declared in its GeoParquet "
            "metadata, so its row groups would hold no spatial statistics."
        )
        raise ValueError(msg)
    if table.schema.field(index).type == _BBOX_STRUCT:
        return table
    field = pa.field("bbox", _BBOX_STRUCT)
    return table.set_column(index, field, table.column("bbox").cast(_BBOX_STRUCT))


def _with_geo_summary(table: pa.Table) -> pa.Table:
    """Record the geometry types and the file's bbox in the GeoParquet metadata.

    Both are optional in GeoParquet 1.1; ``stac_geoparquet`` leaves the types
    empty and the bbox out. With them, a reader learns the extent and the
    geometry kind from the footer alone.
    """
    geo = json.loads(table.schema.metadata[b"geo"])
    column = geo["columns"][geo["primary_column"]]
    geometries = gpd.GeoSeries.from_wkb(table.column(geo["primary_column"]).to_pylist())
    column["geometry_types"] = sorted(geometries.geom_type.dropna().unique())
    column["bbox"] = [float(value) for value in geometries.total_bounds]
    return table.replace_schema_metadata(
        {**table.schema.metadata, b"geo": json.dumps(geo).encode()}
    )


def _hilbert_sorted(table: pa.Table) -> pa.Table:
    """Sort rows along a Hilbert curve over the bbox centres.

    The curve spans the table's own total bounds, so each mirror is ordered
    across its own extent. Rows close along the curve are close on the
    ground, so each row group gets a small bounding box and readers can skip
    groups.
    """
    bbox = table.column("bbox").combine_chunks()
    boxes = shapely.box(
        *(bbox.field(corner).to_numpy(zero_copy_only=False) for corner in _BBOX_CORNERS)
    )
    distance = gpd.GeoSeries(boxes).hilbert_distance(level=HILBERT_LEVEL).to_numpy()
    return table.take(pa.array(np.argsort(distance, kind="stable")))


def _write_mirror(
    table: pa.Table,
    out_path: Path | UPath,
    *,
    row_group_size: int = MIRROR_ROW_GROUP_SIZE,
) -> None:
    """Write one item mirror, locally or on the object store.

    Every parquet file this module produces is written here, so all of them
    follow the GeoParquet rules Portolan enforces (``PTL-DAT-006`` to
    ``PTL-DAT-008``): spatially ordered rows, a bbox covering column, bounded
    row groups, and per-row-group statistics over the column's four fields.
    ``write_statistics`` is passed explicitly because those statistics are
    essential. Every mirror is compressed the same way
    (:data:`MIRROR_COMPRESSION_LEVEL`).

    Parameters
    ----------
    table
        Rows to write, carrying the GeoParquet schema metadata.
    out_path
        Destination parquet file.
    row_group_size
        Rows per row group. A smaller value gives several groups over a
        handful of rows.
    """
    table = _hilbert_sorted(_with_geo_summary(_with_covering_bbox(table)))
    out_path = UPath(out_path)
    with out_path.open("wb") as handle:
        pq.write_table(
            table,
            handle,
            row_group_size=row_group_size,
            write_statistics=True,
            compression="zstd",
            compression_level=MIRROR_COMPRESSION_LEVEL,
            use_dictionary=False,
        )


def mirror_column_type(data_type: pa.DataType) -> str:
    """Spell one mirror column's Arrow type for ``table:columns``.

    A struct of nested fields, such as ``assets``, is named ``struct`` alone:
    spelled out, its type runs to thousands of characters.
    """
    if pa.types.is_struct(data_type) and any(
        pa.types.is_nested(data_type.field(index).type) for index in range(data_type.num_fields)
    ):
        return "struct"
    return str(data_type)


def link(rel: str, href: str, title: str, media_type: str = "application/json") -> Document:
    """One relative link, as it appears in a written document.

    Portolan requires a ``type`` on every structural link, and
    ``application/geo+json`` specifically on a link to an item.
    """
    return {"rel": rel, "href": href, "type": media_type, "title": title}


def item_document(item: pystac.Item) -> Document:
    """Render one item as the JSON the store publishes.

    The structural links follow from the layout alone, so a year can be written
    before the collections exist, and adding a year leaves existing years
    untouched.

    A tile item's parent is its year sub-catalog (``tiles/{year}/catalog.json``),
    which keeps thousands of item links per year out of the collection
    (``PTL-CAT-001``). A mosaic item's parent is the collection itself, which
    links one item per year.
    """
    catalog = metadata.vocabulary().catalog
    payload = item_dict(item)
    keep = [
        link
        for link in payload.get("links", [])
        if link["rel"] not in {"root", "parent", "collection", "self"}
    ]
    if is_tile_item(item):
        # tiles/{year}/{tile}/item.json
        directory, payload["collection"] = TILES_DIR, catalog.scope.tiles_collection_id
        root, collection, parent = "../../../", "../../", "../"
        parent_href, parent_title = f"{parent}{CATALOG_NAME}", f"Tiles, {item_year(item)}"
    else:
        # mosaics/{year}/item.json
        directory, payload["collection"] = MOSAICS_DIR, catalog.scope.mosaics_collection_id
        root, collection, parent = "../../", "../", "../"
        parent_href, parent_title = f"{parent}collection.json", "Annual mosaics"
    payload["links"] = [
        link("root", f"{root}{CATALOG_NAME}", catalog.short_title),
        link("collection", f"{collection}collection.json", _collection_title(directory)),
        link("parent", parent_href, parent_title),
        *keep,
    ]
    return payload


def _collection_title(directory: str) -> str:
    """Short human title for a link to the collection in ``directory``."""
    return metadata.vocabulary().collection_short_title(directory)


def year_catalog_document(
    year: int,
    item_hrefs_and_titles: Sequence[tuple[str, str]],
) -> Document:
    """Build the sub-catalog listing one year's tile items.

    It is a catalog, which Portolan allows "below a collection to organize its
    items (for example, a raster collection grouping items by year)". Like
    every catalog, it carries no assets.
    """
    catalog = metadata.vocabulary().catalog
    title = f"{_collection_title(TILES_DIR)} {year}"
    return {
        "type": "Catalog",
        "stac_version": "1.1.0",
        "stac_extensions": [PORTOLAN_SCHEMA],
        "id": f"{catalog.scope.tiles_collection_id}-{year}",
        "title": title,
        "description": metadata.vocabulary().year_description(year),
        "links": [
            link("root", f"../../{CATALOG_NAME}", catalog.short_title),
            link("parent", "../collection.json", _collection_title(TILES_DIR)),
            link("describedby", "./README.md", f"{title} documentation", "text/markdown"),
            link("agents", "./AGENTS.md", f"{title} agent guide", "text/markdown"),
            *[
                link("item", href, item_title, "application/geo+json")
                for href, item_title in item_hrefs_and_titles
            ],
        ],
    }


def write_year_items(
    tile_items: Sequence[pystac.Item],
    mosaic_item: pystac.Item | None,
    tiles_dir: Path,
    mosaics_dir: Path,
    *,
    year: int,
    year_prose: tuple[str, str],
) -> list[Path]:
    """Write one year's generated documents into a locally staged tree.

    Writes the tile items, the year sub-catalog and its two markdown files, and
    the mosaic item. The staged tree is uploaded afterwards
    (:func:`~glace_catalog.stac.store.upload_tree`).

    Parameters
    ----------
    tile_items
        The year's tile items.
    mosaic_item
        The year's mosaic item, or None when only tiles were built.
    tiles_dir
        Local directory standing in for ``tiles/{year}/``.
    mosaics_dir
        Local directory standing in for ``mosaics/{year}/``.
    year
        Year being written.
    year_prose
        The year sub-catalog's ``README.md`` and ``AGENTS.md``, rendered by
        :func:`glace_catalog.documents.render_year_prose`.

    Returns
    -------
    list of pathlib.Path
        The documents written.
    """
    written: list[Path] = []
    listed: list[tuple[str, str]] = []
    for item in sorted(tile_items, key=_mgrs_tile):
        tile = str(item.properties["glace:mgrs_tile"])
        path = tiles_dir / tile / ITEM_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            dump_document(item_document(item)),
        )
        written.append(path)
        listed.append((f"./{tile}/{ITEM_NAME}", f"GLACE tile {tile}, {year}"))

    if listed:
        tiles_dir.mkdir(parents=True, exist_ok=True)
        catalog = year_catalog_document(year, listed)
        (tiles_dir / CATALOG_NAME).write_text(dump_document(catalog))
        readme, agents = year_prose
        (tiles_dir / "README.md").write_text(readme)
        (tiles_dir / "AGENTS.md").write_text(agents)
        written += [tiles_dir / CATALOG_NAME, tiles_dir / "README.md", tiles_dir / "AGENTS.md"]

    if mosaic_item is not None:
        mosaics_dir.mkdir(parents=True, exist_ok=True)
        path = mosaics_dir / ITEM_NAME
        path.write_text(
            dump_document(item_document(mosaic_item)),
        )
        written.append(path)

    logger.info("Wrote %d generated documents for %d", len(written), year)
    return written


def read_item(path: Path | UPath) -> pystac.Item:
    """Read one published item document back."""
    return pystac.Item.from_dict(json.loads(UPath(path).read_text()))


def year_child_link(year: int) -> Document:
    """Build the ``child`` link a tiles collection carries for one published year."""
    return link("child", f"./{year}/{CATALOG_NAME}", f"{_collection_title(TILES_DIR)} {year}")


def mosaic_item_link(year: int) -> Document:
    """Build the ``item`` link a mosaics collection carries for one published year.

    The mosaics collection has one item per year, few enough to link directly.
    """
    return link(
        "item",
        f"./{year}/{ITEM_NAME}",
        f"{_collection_title(MOSAICS_DIR)}, {year}",
        "application/geo+json",
    )
