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
(``./coh12_vv.tif``), and the item mirror carries absolute ones.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, override
from urllib.parse import urljoin

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pystac
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

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)


class UPathStacIO(pystac.stac_io.DefaultStacIO):
    """pystac IO that reads and writes through ``UPath``.

    Local and ``s3://`` stores are read and written the same way, including
    during validation, and relative child links resolve in both.

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

    @override
    def write_text_to_href(self, href: str, txt: str) -> None:
        """Write one document, to a local path or an object store."""
        target = self._path(href)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(txt)


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

ITEM_NAME = "item.json"
CATALOG_NAME = "catalog.json"

TILES_DIR = "tiles"
MOSAICS_DIR = "mosaics"


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


def export_geoparquet(items: Sequence[pystac.Item], out_path: Path | UPath) -> None:
    """Export items as a stac-geoparquet index."""
    write_stac_geoparquet([item_dict(item) for item in items], out_path)


def _rebase_hrefs(table: pa.Table, base_url: str) -> pa.Table:
    """Rewrite an index's root-relative asset hrefs onto an absolute base URL."""
    base = base_url.rstrip("/")
    assets = table.column("assets").to_pylist()
    for row in assets:
        if row is None:
            continue
        for asset in row.values():
            # A struct column holds every key any item has; a tile without one
            # of them (a single-polarization tile) carries a null there.
            if asset is None:
                continue
            href = asset.get("href")
            if href is not None and "://" not in href:
                asset["href"] = f"{base}/{href.lstrip('/')}"
    index = table.schema.get_field_index("assets")
    rebased = pa.array(assets, type=table.schema.field(index).type)
    return table.set_column(index, table.schema.field(index), rebased)


def concat_indexes(
    parts: Iterable[Path | UPath],
    out_path: Path | UPath,
    *,
    base_url: str,
) -> int:
    """Concatenate per-year indexes into one, rebasing their hrefs onto ``base_url``.

    The per-year files hold root-relative hrefs, so they stay valid when the
    store moves. The store-level index holds absolute hrefs, because its
    readers have no root to resolve against. Only ``finalize-catalog`` takes
    ``--base-url``, so moving the store takes one finalize run.

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
    """Write a stac-geoparquet index straight from items, with absolute hrefs.

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
        for asset in payload.get("assets", {}).values():
            href = asset.get("href")
            if href is not None and "://" not in href:
                asset["href"] = urljoin(item_base, href)
        dicts.append(payload)
    write_stac_geoparquet(dicts, out_path)
    return len(dicts)


def _mgrs_tile(item: pystac.Item) -> str:
    """MGRS tile an item covers, as a sort key over tile items."""
    return str(item.properties["glace:mgrs_tile"])


HILBERT_ORDER = 16
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

_BBOX_CORNERS = ("xmin", "ymin", "xmax", "ymax")

_BBOX_STRUCT = pa.struct([(corner, pa.float64()) for corner in _BBOX_CORNERS])


def hilbert_index(x: int, y: int, *, order: int = HILBERT_ORDER) -> int:
    """Position of a grid cell along the 2-D Hilbert curve.

    The Hilbert curve visits every cell of a ``2**order`` square grid once, and
    cells close along the curve are close on the grid. Sorting rows by this
    index gives each row group a small bounding box, so readers can skip
    groups.

    Parameters
    ----------
    x, y
        Cell coordinates, each in ``[0, 2**order)``.
    order
        Bits per axis.

    Returns
    -------
    int
        Distance along the curve, in ``[0, 4**order)``.
    """
    distance = 0
    side = 1 << (order - 1)
    while side > 0:
        rx = 1 if x & side else 0
        ry = 1 if y & side else 0
        distance += side * side * ((3 * rx) ^ ry)
        # Rotate the quadrant so the curve enters the next one where it left
        # this one, joining the four sub-curves.
        if ry == 0:
            if rx == 1:
                x = side - 1 - x
                y = side - 1 - y
            x, y = y, x
        side >>= 1
    return distance


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


def _hilbert_sorted(table: pa.Table) -> pa.Table:
    """Sort rows along a Hilbert curve over the bbox centres.

    The curve spans the table's own total bounds, so a Swiss year and an
    Alpine one are each ordered across their own extent. Rows without a bbox
    go last.
    """
    bbox = table.column("bbox").combine_chunks()
    corners = {
        corner: bbox.field(corner).to_numpy(zero_copy_only=False) for corner in _BBOX_CORNERS
    }
    centre_x = (corners["xmin"] + corners["xmax"]) / 2
    centre_y = (corners["ymin"] + corners["ymax"]) / 2
    placed = np.isfinite(centre_x) & np.isfinite(centre_y)
    if not placed.any():
        return table
    cells = 2**HILBERT_ORDER - 1
    grid_x = _grid_coordinates(centre_x, placed, cells)
    grid_y = _grid_coordinates(centre_y, placed, cells)
    # 4**16 - 1 is the last cell on the curve, so unplaced rows get the next
    # key and sort last. Both fit easily in an int64.
    keys = np.full(table.num_rows, 4**HILBERT_ORDER, dtype=np.int64)
    keys[placed] = [
        hilbert_index(int(x), int(y)) for x, y in zip(grid_x[placed], grid_y[placed], strict=True)
    ]
    order = np.argsort(keys, kind="stable")
    return table.take(pa.array(order))


def _grid_coordinates(values: np.ndarray, placed: np.ndarray, cells: int) -> np.ndarray:
    """Map coordinates onto ``[0, cells]`` over the extent of the placed ones."""
    low, high = values[placed].min(), values[placed].max()
    span = high - low
    if span <= 0:
        return np.zeros(values.shape, dtype=np.int64)
    scaled = np.rint((values - low) / span * cells)
    return np.clip(np.nan_to_num(scaled), 0, cells).astype(np.int64)


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
    essential.

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
    table = _with_covering_bbox(table)
    table = _hilbert_sorted(table)
    out_path = UPath(out_path)
    with out_path.open("wb") as handle:
        pq.write_table(table, handle, row_group_size=row_group_size, write_statistics=True)


def _link(rel: str, href: str, title: str | None = None, *, item: bool = False) -> dict[str, str]:
    """One relative link, as it appears in a written document.

    Portolan requires a ``type`` on every structural link, and
    ``application/geo+json`` specifically on a link to an item.
    """
    link = {
        "rel": rel,
        "href": href,
        "type": "application/geo+json" if item else "application/json",
    }
    if title is not None:
        link["title"] = title
    return link


def item_document(item: pystac.Item, collection_id: str, *, scope_title: str) -> Document:
    """Render one item as the JSON the store publishes.

    The structural links follow from the layout alone, so a year can be written
    before the collections exist, and adding a year leaves existing years
    untouched.

    A tile item's parent is its year sub-catalog (``tiles/{year}/catalog.json``),
    which keeps thousands of item links per year out of the collection
    (``PTL-CAT-001``). A mosaic item's parent is the collection itself, which
    links one item per year.
    """
    payload = item_dict(item)
    payload["collection"] = collection_id
    keep = [
        link
        for link in payload.get("links", [])
        if link["rel"] not in {"root", "parent", "collection", "self"}
    ]
    if is_tile_item(item):
        # tiles/{year}/{tile}/item.json
        root, collection, parent = "../../../", "../../", "../"
        parent_href, parent_title = f"{parent}{CATALOG_NAME}", f"Tiles, {item_year(item)}"
    else:
        # mosaics/{year}/item.json
        root, collection, parent = "../../", "../", "../"
        parent_href, parent_title = f"{parent}collection.json", "Annual mosaics"
    payload["links"] = [
        _link("root", f"{root}{CATALOG_NAME}", scope_title),
        _link("collection", f"{collection}collection.json", _collection_title(collection_id)),
        _link("parent", parent_href, parent_title),
        *keep,
    ]
    return payload


def _collection_title(collection_id: str) -> str:
    """Short human title for a link to one of the store's collections."""
    if collection_id.startswith("labels-"):
        return "GLACE labels"
    return metadata.vocabulary().collection_short_title(_collection_dir(collection_id))


def _collection_dir(collection_id: str) -> str:
    """Directory a collection document lives in, from its id.

    A store's two collections take fixed directories; a label collection, which
    lives in its own root beside its masks, takes its own id.
    """
    if collection_id.startswith("labels-"):
        return collection_id
    return MOSAICS_DIR if collection_id.endswith("-mosaics") else TILES_DIR


def year_catalog_document(
    year: int,
    item_hrefs_and_titles: Sequence[tuple[str, str]],
    *,
    scope_title: str,
    portolan_schema: str,
    collection_id: str,
) -> Document:
    """Build the sub-catalog listing one year's tile items.

    It is a catalog, which Portolan allows "below a collection to organize its
    items (for example, a raster collection grouping items by year)". Like
    every catalog, it carries no assets.
    """
    return {
        "type": "Catalog",
        "stac_version": "1.1.0",
        "stac_extensions": [portolan_schema],
        "id": f"{collection_id}-{year}",
        "title": _year_title(collection_id, year),
        "description": metadata.vocabulary().year_description(year),
        "links": [
            _link("root", f"../../{CATALOG_NAME}", scope_title),
            _link("parent", "../collection.json", _collection_title(collection_id)),
            _link(
                "describedby",
                "./README.md",
                f"{_year_title(collection_id, year)} documentation",
            )
            | {"type": "text/markdown"},
            _link("agents", "./AGENTS.md", f"{_year_title(collection_id, year)} agent guide")
            | {"type": "text/markdown"},
            *[_link("item", href, title, item=True) for href, title in item_hrefs_and_titles],
        ],
    }


def _year_title(collection_id: str, year: int) -> str:
    """Title of one year sub-catalog, e.g. ``GLACE tiles 2024``."""
    return f"{_collection_title(collection_id)} {year}"


def write_year_items(
    tile_items: Sequence[pystac.Item],
    mosaic_item: pystac.Item | None,
    tiles_dir: Path,
    mosaics_dir: Path,
    *,
    tiles_collection_id: str,
    mosaics_collection_id: str,
    scope_title: str,
    portolan_schema: str,
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
    tiles_collection_id, mosaics_collection_id
        Collection ids the items belong to.
    scope_title
        Title of the root catalog, used on ``root`` links.
    portolan_schema
        Portolan profile schema URI the year sub-catalog declares.
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
            json.dumps(item_document(item, tiles_collection_id, scope_title=scope_title), indent=2)
            + "\n",
        )
        written.append(path)
        listed.append((f"./{tile}/{ITEM_NAME}", f"GLACE tile {tile}, {year}"))

    if listed:
        tiles_dir.mkdir(parents=True, exist_ok=True)
        catalog = year_catalog_document(
            year,
            listed,
            scope_title=scope_title,
            portolan_schema=portolan_schema,
            collection_id=tiles_collection_id,
        )
        (tiles_dir / CATALOG_NAME).write_text(json.dumps(catalog, indent=2) + "\n")
        readme, agents = year_prose
        (tiles_dir / "README.md").write_text(readme)
        (tiles_dir / "AGENTS.md").write_text(agents)
        written += [tiles_dir / CATALOG_NAME, tiles_dir / "README.md", tiles_dir / "AGENTS.md"]

    if mosaic_item is not None:
        mosaics_dir.mkdir(parents=True, exist_ok=True)
        path = mosaics_dir / ITEM_NAME
        path.write_text(
            json.dumps(
                item_document(mosaic_item, mosaics_collection_id, scope_title=scope_title),
                indent=2,
            )
            + "\n",
        )
        written.append(path)

    logger.info("Wrote %d generated documents for %d", len(written), year)
    return written


def read_item(path: Path | UPath) -> pystac.Item:
    """Read one published item document back."""
    return pystac.Item.from_dict(json.loads(UPath(path).read_text()))


def year_child_link(year: int) -> dict[str, str]:
    """Build the ``child`` link a tiles collection carries for one published year."""
    return _link("child", f"./{year}/{CATALOG_NAME}", f"{_collection_title('-tiles')} {year}")


def mosaic_item_link(year: int) -> dict[str, str]:
    """Build the ``item`` link a mosaics collection carries for one published year.

    The mosaics collection has one item per year, few enough to link directly.
    """
    return _link(
        "item",
        f"./{year}/{ITEM_NAME}",
        f"{_collection_title('-mosaics')}, {year}",
        item=True,
    )
