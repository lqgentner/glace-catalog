"""Writing the generated documents, and the item mirrors beside them.

Two kinds of document live in the published tree and they are produced very
differently.

**Source documents** — the root ``catalog.json``, both ``collection.json``, and
every ``README.md`` and ``AGENTS.md`` — are written by hand and tracked in git.
Nothing here generates them. ``finalize-catalog`` *patches* them: it adds the
links and per-year assets that follow from what has actually been published, and
leaves the prose alone. That is the whole point of keeping the catalog in git,
and it is why there is no ``build_tiles_collection`` in this package.

**Generated documents** — every ``item.json``, every year sub-catalog, and both
``items.parquet`` — are written from the run and are not tracked. This module
writes those.

Structural links are relative, so one tree of bytes is valid in the repository,
in a preview and in production. Item *asset* hrefs are relative too
(``./coh12_vv.tif``): they sit beside the item that names them, and a consumer
who needs absolute URLs reads the item mirror, which carries them. Only the
collection-level assets are absolute, because those are what a bucket listing
renders as links.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any, override
from urllib.parse import urljoin

import pyarrow as pa
import pyarrow.parquet as pq
import pystac
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

The versioned schema URI is the single signal of specification version — there
is no separate version property — so every generated catalog must agree with the
tracked ``catalog.json``. rashid reports a mismatch against the root as a warning
(``PTL-CNF-002``), which makes this constant the one place a spec bump is edited.
"""

Document = dict[str, Any]
"""One STAC document as it is read and written: JSON object keys to JSON values.

The values are genuinely heterogeneous — a link list, a nested extent, a number —
and every consumer here reads a named key rather than the mapping as a whole.
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
    subtree holding its items, which is what Portolan requires of a collection
    directory and what lets a year sub-catalog exist below one.
    """
    year = item_year(item)
    if is_tile_item(item):
        return f"{TILES_DIR}/{year}/{item.properties['glace:mgrs_tile']}"
    return f"{MOSAICS_DIR}/{year}"


def item_dict(item: pystac.Item) -> Document:
    """Serialize an item without resolving its links.

    A published item's links are all relative, so pystac's default href
    transform would try to walk to a root that is not loaded. The store writes
    those links itself, so there is nothing to transform.
    """
    return item.to_dict(include_self_link=False, transform_hrefs=False)


def write_stac_geoparquet(payloads: Sequence[Document], out_path: Path | UPath) -> None:
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


def _write_table(table: pa.Table, out_path: Path | UPath) -> None:
    """Write an arrow table to parquet, locally or on the object store."""
    out_path = UPath(out_path)
    with out_path.open("wb") as handle:
        pq.write_table(table, handle)


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
    without the collections existing yet — which is what makes adding a year
    later leave every existing year untouched.

    The two collections sit at different depths and have different parents, and
    that is not incidental. A tile item's parent is its **year sub-catalog**
    (``tiles/{year}/catalog.json``), because a collection with 2 209 item links
    per year is what ``PTL-CAT-001`` exists to discourage. A mosaic item's parent
    is the collection itself: eleven items need no sub-catalog.
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
    if collection_id.endswith("-mosaics"):
        return "GLACE mosaics"
    if collection_id.startswith("labels-"):
        return "GLACE labels"
    return "GLACE tiles"


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

    A catalog, not a collection: Portolan forbids a collection inside a
    collection but explicitly allows "a catalog below a collection to organize
    its items (for example, a raster collection grouping items by year)". It
    carries no assets, because a catalog may not have any — the year's manifest
    and configuration are registered on the collection instead.
    """
    return {
        "type": "Catalog",
        "stac_version": "1.1.0",
        "stac_extensions": [portolan_schema],
        "id": f"{collection_id}-{year}",
        "title": f"GLACE tiles {year}",
        "description": (
            f"The {len(item_hrefs_and_titles)} MGRS tiles composited in {year}, one item "
            "each, every item carrying that tile's twelve single-band composites. "
            "For anything beyond browsing, query the collection's items.parquet "
            "instead: it holds every year in one file, so a filter costs one range "
            "request rather than one HTTP fetch per tile."
        ),
        "links": [
            _link("root", f"../../{CATALOG_NAME}", scope_title),
            _link("parent", "../collection.json", _collection_title(collection_id)),
            _link("describedby", "./README.md", f"GLACE tiles {year} documentation")
            | {"type": "text/markdown"},
            _link("agents", "./AGENTS.md", f"GLACE tiles {year} agent guide")
            | {"type": "text/markdown"},
            *[_link("item", href, title, item=True) for href, title in item_hrefs_and_titles],
        ],
    }


def year_catalog_prose(year: int, n_items: int) -> tuple[str, str]:
    """Render the ``README.md`` and ``AGENTS.md`` a year sub-catalog owes.

    Portolan requires both in every catalog directory (``PTL-FIL-001``) and
    requires them linked (``PTL-FIL-002``, ``PTL-FIL-003``). They are generated
    rather than written because there are one per year per scope and the only
    thing that distinguishes them is the year — but they are kept short and
    specific rather than padded, so that a reader who opens one learns something.

    Returns
    -------
    tuple of str
        ``(README.md, AGENTS.md)`` contents.
    """
    readme = f"""# GLACE tiles {year}

The {n_items} MGRS tiles composited in {year}. Each item carries twelve
single-band Cloud-Optimized GeoTIFFs at 10 m, in that tile's own UTM zone.

This sub-catalog exists to group the year's items for browsing. It holds no
assets of its own — a STAC catalog may not have any — and adds no metadata:
everything about the pixels is on the items, and everything about the run that
wrote them is in each GeoTIFF's tags.

## Reading it

Do not walk this catalog: one HTTP request per tile, to learn what one range
request already holds. `../items.parquet` covers every year in one file, so
selecting this year is a predicate rather than a different URL. The
[collection README](../README.md) has the worked query.

## License

CC-BY-4.0. Contains modified Copernicus Sentinel data.

## Provenance

Produced at the University of Zurich, Department of Geography, from Copernicus
Sentinel-1 SLC data. Per-file provenance is embedded in each GeoTIFF's tags and
mirrored into the item properties, including the software versions, the DEM and
the compositing window and parameters.
"""
    agents = f"""# GLACE tiles {year} — agent guide

A browse-only grouping of {n_items} tile items. It carries no assets and adds no
metadata; everything below it is described by the
[tiles collection](../collection.json), whose `AGENTS.md` is the one to read.

**Query `../items.parquet`, not this catalog.** It covers every year in one
file, so filtering by year is a predicate rather than a different URL. Walking
these item links costs one HTTP request each and tells you nothing the mirror
does not.

The year in this path is the **composite window**, not an acquisition date. Each
item carries `start_datetime` and `end_datetime` giving the window its
composites were drawn from.
"""
    return readme, agents


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
) -> list[Path]:
    """Write one year's generated documents into a locally staged tree.

    Writes the tile items, the year sub-catalog and its two markdown files, and
    the mosaic item. Written locally and uploaded once: 2 209 small JSON files
    put straight to the object store would be 2 209 round trips.

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
        readme, agents = year_catalog_prose(year, len(listed))
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
    return _link("child", f"./{year}/{CATALOG_NAME}", f"GLACE tiles {year}")


def mosaic_item_link(year: int) -> dict[str, str]:
    """Build the ``item`` link a mosaics collection carries for one published year.

    Eleven items is under the fan-out threshold, so the mosaics collection links
    them directly rather than through a sub-catalog per year.
    """
    return _link(
        "item",
        f"./{year}/{ITEM_NAME}",
        f"GLACE annual mosaic, {year}",
        item=True,
    )
