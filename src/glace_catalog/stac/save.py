"""Writing the catalogue out, and the geoparquet indexes beside it.

Everything is written with ``RELATIVE_PUBLISHED`` hrefs: every link and every
asset relative, and one absolute ``self`` link on the root catalog. Moving the
bucket is then editing that line and rerunning ``finalize-catalog``, rather than
rewriting every document in the store.

The layout the store wants — ``{year}/{tile}/item.json``, ``{year}/mosaics/
item.json`` — is not one pystac produces by default, so it comes from a custom
layout strategy rather than from where the items happen to be added.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import pyarrow as pa
import pyarrow.parquet as pq
import pystac
from pystac.layout import CustomLayoutStrategy
import stac_geoparquet.arrow
from upath import UPath

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)


class UPathStacIO(pystac.stac_io.DefaultStacIO):
    """pystac IO that reads and writes through ``UPath``.

    pystac's default reader is urllib-based and does not know ``s3://``, so a
    catalogue on object storage cannot be read back — including by its own
    validation. Routing through ``UPath`` makes local and remote stores behave
    identically, and relative child links resolve either way.

    pystac hands links back as plain strings, and a bare ``s3://`` string carries
    no profile or endpoint, so the configuration has to be held here rather than
    recovered from the href.

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

    def read_text_from_href(self, href: str) -> str:
        """Read one document, from a local path or an object store."""
        return self._path(href).read_text()

    def write_text_to_href(self, href: str, txt: str) -> None:
        """Write one document, to a local path or an object store."""
        target = self._path(href)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(txt)


CATALOG_ID = "glace"
CATALOG_DESCRIPTION = (
    "GLACE: Sentinel-1 coherence and backscatter composites, their annual mosaics "
    "and the web-map tiles rendered from them."
)


def _item_href(item: pystac.Item, parent_dir: str) -> str:
    """Place an item at ``{year}/{tile}/item.json`` or ``{year}/mosaics/item.json``.

    The year and the tile come from the item's own properties, so the layout
    survives a collection being assembled in any order — and both collections
    write into the same ``{year}/`` tree, which is the point of splitting the
    documents rather than the directories.
    """
    year = item.properties["start_datetime"][:4]
    tile = item.properties.get("glace:mgrs_tile")
    leaf = tile if tile is not None else "mosaics"
    return f"{parent_dir}/{year}/{leaf}/item.json"


def store_layout_strategy() -> CustomLayoutStrategy:
    """Layout strategy placing items under ``{year}/{tile}/`` inside the store root.

    Collections keep their own subdirectory for the document alone; their items
    live in the year tree, which relative hrefs make perfectly ordinary.
    """
    return CustomLayoutStrategy(item_func=_item_href)


def build_root_catalog(
    collections: Sequence[pystac.Collection],
    *,
    catalog_id: str = CATALOG_ID,
    description: str = CATALOG_DESCRIPTION,
    thumbnail_href: str | None = None,
) -> pystac.Catalog:
    """Assemble the collections under one root catalog.

    A catalog has links but no assets, so the thumbnail is attached with
    ``rel="preview"`` rather than as an asset.
    """
    catalog = pystac.Catalog(id=catalog_id, description=description)
    for collection in collections:
        catalog.add_child(collection)
    if thumbnail_href is not None:
        catalog.add_link(
            pystac.Link(
                rel="preview",
                target=thumbnail_href,
                media_type=pystac.MediaType.PNG,
                title="GLACE overview",
            ),
        )
    return catalog


def save_store_catalog(
    catalog: pystac.Catalog,
    out_dir: Path,
    *,
    self_href: str,
) -> None:
    """Write a catalog tree with relative hrefs and one absolute root self href.

    Parameters
    ----------
    catalog
        Root catalog holding the store's collections.
    out_dir
        Local directory the tree is staged in, mirroring the store root.
    self_href
        Absolute URL the published root catalog will answer at. It is the only
        absolute href in the store, so relocating the bucket is editing it.
    """
    catalog.normalize_hrefs(str(out_dir), strategy=store_layout_strategy())
    catalog.set_self_href(f"{self_href.rstrip('/')}/catalog.json")
    catalog.save(catalog_type=pystac.CatalogType.RELATIVE_PUBLISHED)
    n_items = sum(len(list(child.get_items())) for child in catalog.get_children())
    logger.info("Saved catalog with %d items to %s", n_items, out_dir)


def item_dict(item: pystac.Item) -> dict:
    """Serialize an item without resolving its links.

    A published item's links are all relative, so pystac's default href
    transform would try to walk to a root that is not loaded. The store writes
    those links itself, so there is nothing to transform.
    """
    return item.to_dict(include_self_link=False, transform_hrefs=False)


def write_stac_geoparquet(payloads: Sequence[dict], out_path: Path | UPath) -> None:
    """Write item dicts as a stac-geoparquet index.

    Always through ``stac_geoparquet.arrow.to_parquet``, never a plain parquet
    write: the geometry column needs the GeoParquet schema metadata, without
    which a reader sees an opaque blob rather than a geometry.
    """
    batches = stac_geoparquet.arrow.parse_stac_items_to_arrow(list(payloads))
    out_path = UPath(out_path)
    if out_path.protocol == "s3":
        # stac-geoparquet writes through a path, so stage locally and put once.
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / out_path.name
            stac_geoparquet.arrow.to_parquet(batches, str(staged))
            out_path.write_bytes(staged.read_bytes())
    else:
        stac_geoparquet.arrow.to_parquet(batches, str(out_path))
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

    The per-year files hold **root-relative** hrefs, so they survive a bucket
    move untouched; the store-level index holds absolute ones, because a
    consumer reading it has no root to resolve against. That is why ``--base-url``
    belongs to ``finalize-catalog`` alone: a bucket move is one finalize run
    rather than a per-year rewrite.

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
    # concat_tables keeps the first table's schema metadata, which is where the
    # GeoParquet geometry declaration lives; the rebase must not drop it.
    combined = combined.replace_schema_metadata(tables[0].schema.metadata)
    _write_table(combined, out_path)
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

    Used for the mosaics index, which is one row per year: a per-year parquet
    holding a single row would be pointless, so it is built from the mosaic items
    rather than concatenated.

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
        # Resolved rather than concatenated: an asset one level up (`../`) must
        # come out as a clean URL, not one a reader has to normalise itself.
        item_base = f"{base}/{hrefs_relative_to or _item_prefix(item)}/"
        for asset in payload.get("assets", {}).values():
            href = asset.get("href")
            if href is not None and "://" not in href:
                asset["href"] = urljoin(item_base, href)
        dicts.append(payload)
    write_stac_geoparquet(dicts, out_path)
    return len(dicts)


def _item_prefix(item: pystac.Item) -> str:
    """Path from the store root to an item's own directory."""
    year = item.properties["start_datetime"][:4]
    tile = item.properties.get("glace:mgrs_tile")
    return f"{year}/{tile if tile is not None else 'mosaics'}"


def _write_table(table: pa.Table, out_path: Path | UPath) -> None:
    """Write an arrow table to parquet, locally or on the object store."""
    out_path = UPath(out_path)
    with out_path.open("wb") as handle:
        pq.write_table(table, handle)


ITEM_NAME = "item.json"

#: Depth of an item document below the store root: ``{year}/{tile}/item.json``.
_ITEM_DEPTH = "../.."


def _link(rel: str, href: str, title: str | None = None) -> dict[str, str]:
    """One relative link, as it appears in a written item document."""
    link = {"rel": rel, "href": href, "type": "application/json"}
    if title is not None:
        link["title"] = title
    return link


def item_document(item: pystac.Item, collection_id: str) -> dict:
    """Render one item as the JSON the store publishes.

    The root, parent and collection links are all relative and follow from the
    layout alone, so a year can be written without the collections existing yet
    — which is what makes adding a year later leave every existing year
    untouched.
    """
    payload = item_dict(item)
    payload["collection"] = collection_id
    keep = [
        link
        for link in payload.get("links", [])
        if link["rel"] not in {"root", "parent", "collection", "self"}
    ]
    payload["links"] = [
        _link("root", f"{_ITEM_DEPTH}/catalog.json"),
        _link("collection", f"{_ITEM_DEPTH}/{_collection_dir(collection_id)}/collection.json"),
        _link("parent", f"{_ITEM_DEPTH}/{_collection_dir(collection_id)}/collection.json"),
        *keep,
    ]
    return payload


def _collection_dir(collection_id: str) -> str:
    """Directory a collection document lives in, from its id.

    A store's two collections take fixed directories; a label collection, which
    lives in its own root beside its masks, takes its own id.
    """
    if collection_id.startswith("labels-"):
        return collection_id
    return "mosaics" if collection_id.endswith("-mosaics") else "tiles"


def write_year_items(
    tile_items: Sequence[pystac.Item],
    mosaic_item: pystac.Item | None,
    out_dir: Path,
    *,
    tiles_collection_id: str,
    mosaics_collection_id: str,
) -> list[Path]:
    """Write one year's item documents into a locally staged year directory.

    Written locally and uploaded once: 2 209 small JSON files put straight to the
    object store would be 2 209 round trips.

    Returns
    -------
    list[pathlib.Path]
        The documents written.
    """
    written: list[Path] = []
    for item in tile_items:
        tile = item.properties["glace:mgrs_tile"]
        path = out_dir / tile / ITEM_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(item_document(item, tiles_collection_id), indent=2) + "\n")
        written.append(path)
    if mosaic_item is not None:
        path = out_dir / "mosaics" / ITEM_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(item_document(mosaic_item, mosaics_collection_id), indent=2) + "\n",
        )
        written.append(path)
    logger.info("Wrote %d item documents to %s", len(written), out_dir)
    return written


def read_item(path: Path | UPath) -> pystac.Item:
    """Read one published item document back."""
    return pystac.Item.from_dict(json.loads(UPath(path).read_text()))


def add_item_links(collection: pystac.Collection, hrefs: Sequence[str]) -> None:
    """Link a collection to its items by href, without resolving them.

    A tiles collection over eleven Alpine years links roughly 24 000 items;
    materialising each to write one link would be pointless, and nobody should be
    walking that list anyway — that is what the geoparquet index is for.
    """
    for href in hrefs:
        collection.add_link(pystac.Link(rel="item", target=href, media_type="application/json"))
