r"""Write the store's root catalog, collections and item indexes over every built year.

``build-year.py`` writes one year at a time. This script reads the built
years and writes what spans them:

- the root ``catalog.json``, with an absolute ``self`` link under the public
  address, ``public_base`` in ``catalog.publish.yaml``;
- each year's MapLibre styles, ``mosaics/{year}/styles/``, registered on its
  mosaic item;
- ``tiles/collection.json`` and ``mosaics/collection.json``, with links to each
  year and the temporal extent; the tiles collection's spatial extent bounds
  its tiles, and the mosaics collection's style draws the latest year;
- the run's time as ``updated`` on the root catalog and both collections;
- a thumbnail per collection, drawn from the latest year's COGs;
- ``tiles/grid.pmtiles``, the tile grid with each tile's glacier fractions,
  and the tiles collection's styles: ``tiles/styles/grid.json``, and
  ``tiles/styles/default.json``, which draws the latest year's coherence;
- ``tiles/items.parquet``, the per-year indexes combined, and
  ``mosaics/items.parquet``, one row per year; both with absolute hrefs under
  the public address, and described by their ``items`` assets' column types
  and row counts.

It then validates the result against the STAC schemas.

The documents come from the rendered ``catalog/`` and the store from
``GLACE_STORE_ROOT``.

Usage:
    make finalize
    uv run --env-file .env scripts/finalize-catalog.py
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import UTC, datetime
import json
import logging
import math
from pathlib import Path
import re
import tempfile
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq
import pystac
from upath import UPath

from glace_catalog import stac, webmap
from glace_catalog.config import settings
from glace_catalog.documents import load_public_bases, stale_documents
from glace_catalog.grid import grid_frame, write_grid
from glace_catalog.metadata import vocabulary
from glace_catalog.stac.items import extent_from_items
from glace_catalog.stac.save import (
    MOSAICS_DIR,
    TILES_DIR,
    Document,
    mirror_column_type,
    read_item,
)
from glace_catalog.stac.store import (
    ASSETS_DIR,
    YEAR_INDEX_NAME,
    multihash_sha256,
    upload_tree,
)
from glace_catalog.styles import YEAR_IN_TEXT
from glace_catalog.thumbnails import DEFAULT_BASEMAP, write_collection_thumbnails

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import geopandas as gpd

logger = logging.getLogger(Path(__file__).stem)

SOURCE_DIR = Path(__file__).resolve().parent.parent / "catalog"
"""The tracked documents, rendered from ``metadata/``."""

EXTENT_DECIMALS = 4
"""Decimals of the tiles collection's bbox, about ten metres."""


def built_years(layout: stac.StoreLayout) -> list[int]:
    """Years the store holds: the ``tiles/{year}/`` directories with an item index.

    ``build-year.py`` writes that index in its ``items`` step.
    """
    if not layout.tiles.exists():
        msg = f"No {TILES_DIR}/ under {layout.root}. Run build-year.py first."
        raise SystemExit(msg)
    years: list[int] = []
    entries = {entry.name: entry for entry in layout.tiles.iterdir()}
    for name in sorted(entries):
        if not name.isdigit():
            continue
        if (entries[name] / YEAR_INDEX_NAME).exists():
            years.append(int(name))
        else:
            logger.warning("%s has no %s; skipping", name, YEAR_INDEX_NAME)
    if not years:
        msg = f"No built years under {layout.tiles}. Run build-year.py first."
        raise SystemExit(msg)
    logger.info("Found %d built year(s): %s", len(years), ", ".join(map(str, years)))
    return years


def read_year_index(layout: stac.StoreLayout, year: int) -> pa.Table:
    """Read one year's tile-item index."""
    with layout.year_index(year).open("rb") as handle:
        return pq.read_table(handle)


def tiles_summary(tables: Sequence[pa.Table]) -> tuple[int, set[str]]:
    """Count the tile items and collect their editions from the item indexes.

    The indexes are fast to read; the Alpine store holds about 26 500 item
    documents.
    """
    count = 0
    versions: set[str] = set()
    for table in tables:
        count += table.num_rows
        versions.update(table.column("version").to_pylist())
    return count, versions


def require_current_year_indexes(tables: Sequence[pa.Table], years: Sequence[int]) -> None:
    """Stop if a year's tile index lacks a documented column or has an undocumented one.

    The combined mirror takes every column any year has and fills the rest
    with nulls, so a year indexed by an older build would publish rows
    without, say, their collection. Checked before anything is written.

    Raises
    ------
    SystemExit
        Listing each stale year and the command that reindexes it.
    """
    documented = {column["name"] for column in vocabulary().mirror_columns(TILES_DIR)}
    stale = []
    for year, table in zip(years, tables, strict=True):
        written = set(table.column_names)
        if written != documented:
            stale.append(
                f"{year}: missing {sorted(documented - written)}, "
                f"undocumented {sorted(written - documented)}"
            )
    if stale:
        hint = "reindex those years with: make build YEAR=... ARGS='--force-from items'"
        raise SystemExit("\n".join([*stale, hint]))


def read_mosaic_items(layout: stac.StoreLayout, years: Sequence[int]) -> list[pystac.Item]:
    """Read the per-year mosaic items; one per year, so reading them all is cheap."""
    items = []
    for year in years:
        path = layout.mosaic_item(year)
        if not path.exists():
            logger.warning("%s has no mosaic item; skipping", year)
            continue
        items.append(read_item(path))
    return items


def _load(path: Path) -> Document:
    """Read one tracked source document."""
    return json.loads(path.read_text())


def _keep(links: Sequence[Document], drop: set[str]) -> list[Document]:
    """Keep the links whose ``rel`` is outside ``drop``."""
    return [link for link in links if link.get("rel") not in drop]


def tiles_bbox(grid: gpd.GeoDataFrame) -> list[float]:
    """Return the WGS84 bbox of every tile, rounded outward to :data:`EXTENT_DECIMALS`.

    The tile grid archive declares the bounds of the same polygons in its
    header, so the collection and the archive agree.
    """
    scale = 10**EXTENT_DECIMALS
    # Rounding the product first keeps a bound already on the grid, such as
    # 46.3, from floating to the step below it.
    west, south, east, north = (round(float(value) * scale, 6) for value in grid.total_bounds)
    return [
        math.floor(west) / scale,
        math.floor(south) / scale,
        math.ceil(east) / scale,
        math.ceil(north) / scale,
    ]


def patch_collections(
    source_dir: Path,
    years: Sequence[int],
    mosaic_items: Sequence[pystac.Item],
    tiles_extent: Sequence[float],
) -> dict[str, Document]:
    """Add the built years to the tracked collection documents.

    ``scripts/render-catalog.py`` renders these documents from ``metadata/``,
    and they are reviewed in git. This function adds only what depends on the
    published data: a link to each year, the temporal extent and the tiles'
    spatial extent. Prose, providers, license and ``item_assets`` stay as
    rendered.

    Parameters
    ----------
    source_dir
        The tracked ``catalog/`` directory.
    years
        Published years, ascending.
    mosaic_items
        The mosaic items, read back from the store, for the temporal extent.
    tiles_extent
        The bbox of every tile, from :func:`tiles_bbox`. The rendered extent
        is the mosaic grid's, which reaches past the outermost tiles.

    Returns
    -------
    dict
        ``{"tiles": ..., "mosaics": ...}``, the patched documents.
    """
    patched: dict[str, Document] = {}

    tiles = _load(source_dir / TILES_DIR / "collection.json")
    tiles["links"] = [
        *_keep(tiles["links"], {"child", "item", "self"}),
        *[stac.year_child_link(year) for year in years],
    ]
    # Run manifests and configurations stay unpublished: strip any such asset.
    tiles["assets"] = {
        key: asset
        for key, asset in tiles.get("assets", {}).items()
        if not key.startswith(("manifest-", "config-"))
    }
    tiles["extent"]["spatial"]["bbox"] = [list(tiles_extent)]
    patched["tiles"] = tiles

    mosaics = _load(source_dir / MOSAICS_DIR / "collection.json")
    mosaics["links"] = [
        *_keep(mosaics["links"], {"item", "child", "self"}),
        *[stac.mosaic_item_link(year) for year in years],
    ]
    patched["mosaics"] = mosaics

    if mosaic_items:
        interval = extent_from_items(mosaic_items).temporal.to_dict()["interval"]
        for document in patched.values():
            document["extent"]["temporal"]["interval"] = interval
    return patched


def year_style(template: Document, year: int, archives: str) -> Document:
    """Copy a tracked style template for one year.

    The templates are rendered from ``metadata/assets.yaml`` and reviewed in
    git. Encoding and colour ramp are the same every year, so a copy changes
    only the year, in the attribution, the style name and the layer ids, and
    points each archive source at the year's archive under ``archives``, a path
    relative to the style file. Other sources, such as the tile grid, are the
    same every year and stay as they are.
    """
    style = deepcopy(template)
    # A style has one source per layer; clients switch layers by visibility.
    for source in style.get("sources", {}).values():
        if source.get("type") != "raster-dem":
            continue
        source["url"] = f"pmtiles://{archives}{source['url'].rsplit('/', 1)[-1]}"
        if source.get("attribution"):
            # The Copernicus credit names the year of the data.
            source["attribution"] = YEAR_IN_TEXT.sub(str(year), source["attribution"])
    style["name"] = re.sub(r"\(\d{4}\)", f"({year})", style["name"])
    for layer in style.get("layers", []):
        layer["id"] = re.sub(r"-\d{4}$", f"-{year}", layer["id"])
    return style


def _style_templates(source_dir: Path) -> dict[str, Document]:
    """Read the mosaics collection's tracked style templates, by asset key ``style-{stem}``."""
    return {
        f"style-{path.stem}": json.loads(path.read_text())
        for path in sorted((source_dir / MOSAICS_DIR / "styles").glob("*.json"))
    }


def _write_style(path: Path, style: Document) -> Path:
    """Write one staged style document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(style, indent=2, ensure_ascii=False) + "\n")
    return path


def stage_item_styles(
    source_dir: Path, staged: Path, years: Sequence[int]
) -> dict[int, dict[str, Path]]:
    """Write each year's styles beside its archives, as ``{year}/styles/{stem}.json``.

    Every published year thus has styles that draw its own archives, and its
    mosaic item registers them.

    Returns
    -------
    dict[int, dict[str, pathlib.Path]]
        Staged path per year and style asset key, e.g.
        ``{2024: {"style-default": …}}``.
    """
    templates = _style_templates(source_dir)
    written: dict[int, dict[str, Path]] = {}
    for year in years:
        directory = staged / MOSAICS_DIR / str(year) / "styles"
        written[year] = {
            key: _write_style(
                directory / f"{key.removeprefix('style-')}.json", year_style(template, year, "../")
            )
            for key, template in templates.items()
        }
    logger.info("Wrote %d style(s) over %d year(s)", len(templates) * len(years), len(years))
    return written


def stage_collection_styles(source_dir: Path, staged: Path, latest: int) -> dict[str, Path]:
    """Write the mosaics collection's styles, each the latest year's copy.

    They keep the tracked paths, ``styles/{stem}.json``, so the collection's
    assets name them as rendered.

    Returns
    -------
    dict[str, pathlib.Path]
        Staged path per style asset key, e.g. ``{"style-default": …}``.
    """
    directory = staged / MOSAICS_DIR / "styles"
    return {
        key: _write_style(
            directory / f"{key.removeprefix('style-')}.json",
            year_style(template, latest, f"../{latest}/"),
        )
        for key, template in _style_templates(source_dir).items()
    }


def register_item_styles(
    item: Document,
    year: int,
    styles: Mapping[str, Path],
    item_assets: Mapping[str, Document],
) -> None:
    """Add one year's styles to its mosaic item document, with size and checksum.

    Title, media type and roles come from the collection's ``item_assets``;
    the title takes the year after a dash, and the description is the year's wording from
    ``metadata/``.
    """
    assets: dict[str, Document] = item.setdefault("assets", {})
    for key, path in sorted(styles.items()):
        shared = item_assets.get(key, {})
        payload = path.read_bytes()
        assets[key] = {
            "href": f"./styles/{path.name}",
            "type": shared.get("type", "application/vnd.mapbox.style+json"),
            "title": f"{shared.get('title', key)} – {year}",
            "description": vocabulary().style_description(year),
            "roles": list(shared.get("roles", ["style"])),
            "file:size": len(payload),
            "file:checksum": multihash_sha256(payload),
        }


def publish_item_styles(
    source_dir: Path,
    layout: stac.StoreLayout,
    years: Sequence[int],
    item_assets: Mapping[str, Document],
) -> None:
    """Write each year's styles beside its archives and register them on its mosaic item.

    Runs before the mosaics index is written, so the index lists the styles.
    A rerun of a year's ``items`` step drops them from its item until the next
    finalization.
    """
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp)
        styles = stage_item_styles(source_dir, staged, years)
        # Every archive must have a layer in its year's styles.
        check_style_coverage(layout, years, styles)
        for year in years:
            source = layout.mosaic_item(year)
            if not source.exists():
                continue
            item = json.loads(UPath(source).read_text())
            register_item_styles(item, year, styles[year], item_assets)
            # Written as the build writes items.
            path = staged / MOSAICS_DIR / str(year) / UPath(source).name
            path.write_text(json.dumps(item, indent=2) + "\n")
        upload_tree(staged, layout.root)


def check_style_coverage(
    layout: stac.StoreLayout,
    years: Sequence[int],
    styles: Mapping[int, Mapping[str, Path]],
) -> None:
    """Check that every published web-map archive has a layer in its year's styles.

    A client finds an archive as a ``visual`` asset of the year's mosaic item
    and draws it through that item's style, as layer ``glace-{key}-{year}``.

    Raises
    ------
    SystemExit
        On the first archive whose layer is absent from its year's styles.
    """
    archives = 0
    for year in years:
        directory = layout.mosaic_year(year)
        if not directory.exists():
            continue
        drawn = {
            str(layer["id"])
            for path in styles.get(year, {}).values()
            for layer in json.loads(path.read_text()).get("layers", [])
        }
        for archive in sorted(directory.iterdir()):
            if archive.suffix != ".pmtiles" or not archive.stem.endswith(stac.VIZ_SUFFIX):
                continue
            layer_id = f"glace-{stac.data_key(archive.stem)}-{year}"
            if layer_id not in drawn:
                msg = (
                    f"{MOSAICS_DIR}/{year}/{archive.name} is published, but "
                    f"no style in {MOSAICS_DIR}/{year}/styles/ draws layer {layer_id}"
                )
                raise SystemExit(msg)
            archives += 1
    logger.info("Every one of %d web-map archive(s) is drawn by its year's style", archives)


def with_updated(document: Document, updated: str) -> Document:
    """Return ``document`` with ``updated`` set, placed before its links."""
    stamped = {key: value for key, value in document.items() if key not in {"updated", "links"}}
    stamped["updated"] = updated
    if "links" in document:
        stamped["links"] = document["links"]
    return stamped


def write_documents(
    source_dir: Path,
    layout: stac.StoreLayout,
    documents: Mapping[str, Document],
    *,
    base_url: str,
    years: Sequence[int],
    updated: str,
) -> None:
    """Stage the documents, styles and static files, then upload them.

    The root catalog's absolute ``self`` link is added here, so the tracked
    catalog stays independent of where it is deployed. The root and both
    collections take ``updated``, the time of this run.
    """
    base = base_url.rstrip("/")
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp)
        catalog = _load(source_dir / "catalog.json")
        catalog["links"] = [
            {"rel": "self", "href": f"{base}/catalog.json", "type": "application/json"},
            *_keep(catalog["links"], {"self"}),
        ]
        catalog = with_updated(catalog, updated)
        (staged / "catalog.json").write_text(
            json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
        )

        # Portolan requires a README.md and an AGENTS.md beside every catalog
        # and collection (rule PTL-FIL-001).
        for name in ("README.md", "AGENTS.md"):
            (staged / name).write_bytes((source_dir / name).read_bytes())
            for directory in documents:
                (staged / directory).mkdir(parents=True, exist_ok=True)
                (staged / directory / name).write_bytes(
                    (source_dir / directory / name).read_bytes(),
                )

        # `_assets/` holds files the documents link to, such as the root
        # catalog's icon. They are published beside the documents.
        assets = source_dir / ASSETS_DIR
        if assets.is_dir():
            (staged / ASSETS_DIR).mkdir(parents=True, exist_ok=True)
            for path in sorted(assets.iterdir()):
                if path.is_file():
                    (staged / ASSETS_DIR / path.name).write_bytes(path.read_bytes())

        # Write the styles first: the collections record their size and
        # checksum. The mosaics collection's styles draw the latest year.
        stamp_style_assets(documents, stage_collection_styles(source_dir, staged, max(years)))
        stamp_style_assets(
            documents, stage_tiles_styles(source_dir, staged, max(years)), TILES_DIR
        )

        for directory, document in documents.items():
            path = staged / directory / "collection.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(with_updated(document, updated), indent=2, ensure_ascii=False) + "\n",
            )

        upload_tree(staged, layout.root)


def stamped_asset_paths(
    layout: stac.StoreLayout,
) -> dict[str, dict[str, Path | UPath]]:
    """Store paths of the collection assets :func:`stamp_collection_assets` measures.

    The styles are measured separately by :func:`stamp_style_assets`, once
    they are staged.
    """
    return {
        TILES_DIR: {
            "items": layout.tiles_index,
            "grid": layout.tiles_grid,
            "thumbnail": layout.tiles_thumbnail,
        },
        MOSAICS_DIR: {
            "items": layout.mosaics_index,
            "thumbnail": layout.mosaics_thumbnail,
        },
    }


def stamp_style_assets(
    documents: Mapping[str, Document],
    styles: Mapping[str, Path],
    directory: str = MOSAICS_DIR,
) -> None:
    """Fill ``file:size`` and ``file:checksum`` on one collection's MapLibre styles.

    Each value is measured on the staged copy, which is the file readers
    download: the latest year's copies :func:`stage_collection_styles` and
    :func:`stage_tiles_styles` wrote.
    """
    for key, path in sorted(styles.items()):
        asset = documents.get(directory, {}).get("assets", {}).get(key)
        if asset is None:
            logger.warning("%s declares no %s asset for %s", directory, key, path.name)
            continue
        payload = path.read_bytes()
        asset["file:size"] = len(payload)
        asset["file:checksum"] = multihash_sha256(payload)
        logger.info("Stamped %s/%s: %d bytes", directory, key, len(payload))


def stage_tiles_styles(source_dir: Path, staged: Path, latest: int) -> dict[str, Path]:
    """Write the tiles collection's styles, the latest year's copies.

    The tiles have no archives of their own: the default style draws the
    mosaics collection's archive of the latest year, and the grid style
    draws only the tile grid, the same every year.

    Returns
    -------
    dict[str, pathlib.Path]
        Staged path per style asset key, e.g. ``{"style-grid": …}``.
    """
    out = staged / TILES_DIR / "styles"
    return {
        f"style-{path.stem}": _write_style(
            out / path.name,
            year_style(json.loads(path.read_text()), latest, f"../../{MOSAICS_DIR}/{latest}/"),
        )
        for path in sorted((source_dir / TILES_DIR / "styles").glob("*.json"))
    }


def require_existing_thumbnails(layout: stac.StoreLayout) -> None:
    """Check that the store holds both thumbnails before ``--skip-thumbnail`` reuses them.

    The collections link their thumbnails by file name. A missing file (after
    a rename, say) would publish a broken link, and
    :func:`stamp_collection_assets` only logs a warning for it, so this check
    stops the run.

    Raises
    ------
    FileNotFoundError
        If either collection's thumbnail is missing from the store.
    """
    missing = [
        path for path in (layout.tiles_thumbnail, layout.mosaics_thumbnail) if not path.exists()
    ]
    if missing:
        names = ", ".join(str(path) for path in missing)
        msg = (
            f"--skip-thumbnail keeps the thumbnails already in the store, but {names} "
            f"{'are' if len(missing) > 1 else 'is'} not there; "
            "rerun without --skip-thumbnail to render them"
        )
        raise FileNotFoundError(msg)


def stamp_collection_assets(
    documents: Mapping[str, Document],
    layout: stac.StoreLayout,
) -> None:
    """Fill ``file:size`` and ``file:checksum`` on the collection-level assets.

    These are the item indexes, the thumbnails and the glacier fractions. The
    run manifest holds no digest for them, so they are measured here from the
    bytes in the store. Readers can then verify a download, as they can for
    item assets (Portolan rule ``PTL-AST-003``).

    Call this after the indexes and thumbnails are written. An asset whose
    file is missing is logged as a warning and left as it is.
    """
    paths = stamped_asset_paths(layout)
    for directory, document in documents.items():
        for key, asset in document.get("assets", {}).items():
            path = paths[directory].get(key)
            if path is None:
                continue
            if not path.exists():
                logger.warning("%s has no bytes for its %s asset at %s", directory, key, path)
                continue
            payload = path.read_bytes()
            asset["file:size"] = len(payload)
            asset["file:checksum"] = multihash_sha256(payload)
            logger.info("Stamped %s/%s: %d bytes", directory, key, len(payload))


def describe_mirrors(
    documents: Mapping[str, Document],
    layout: stac.StoreLayout,
) -> None:
    """Add each column's type and the row count to the ``items`` assets.

    The rendered ``table:columns`` name and describe the columns from
    ``metadata/assets.yaml``; the types and the count come from the written
    mirror, read from its footer alone.

    Raises
    ------
    SystemExit
        If a mirror's columns differ from the documented ones.
    """
    paths = stamped_asset_paths(layout)
    for directory, document in documents.items():
        asset = document.get("assets", {}).get("items")
        if asset is None:
            continue
        with UPath(paths[directory]["items"]).open("rb") as handle:
            mirror = pq.ParquetFile(handle)
            schema, rows = mirror.schema_arrow, mirror.metadata.num_rows
        columns = asset.get("table:columns", [])
        documented = {column["name"] for column in columns}
        undocumented = sorted(set(schema.names) - documented)
        absent = sorted(documented - set(schema.names))
        if undocumented or absent:
            msg = (
                f"{directory}/items.parquet differs from mirror_columns in "
                f"metadata/assets.yaml: undocumented {undocumented}, absent {absent}. "
                "Describe or remove them, render, then finalize again"
            )
            raise SystemExit(msg)
        asset["table:columns"] = [
            {
                "name": column["name"],
                "type": mirror_column_type(schema.field(column["name"]).type),
                "description": column["description"],
            }
            for column in columns
        ]
        asset["table:row_count"] = rows
        logger.info(
            "Described %s/items.parquet: %d columns, %d rows", directory, len(columns), rows
        )


def validate(layout: stac.StoreLayout) -> None:
    """Read the written catalog back and validate it against the STAC schemas.

    Reads through :class:`~glace_catalog.stac.save.UPathStacIO`, so a store on
    object storage is validated where it is published.
    """
    stac_io = stac.UPathStacIO(UPath(layout.root).storage_options)
    catalog = pystac.Catalog.from_file(str(layout.catalog), stac_io=stac_io)
    catalog.validate()
    for child in catalog.get_children():
        child.validate()
        logger.info("%s: valid, %d item link(s)", child.id, len(child.get_links("item")))


def require_current_render(source_dir: Path) -> None:
    """Stop if ``source_dir`` differs from a fresh render of ``metadata/``.

    The year builds read the transport ranges from ``metadata/assets.yaml``,
    but the year styles are copied from the rendered template. A stale render
    would decode every archive with the old ranges.

    Raises
    ------
    SystemExit
        Listing each stale document and the command that renders them.
    """
    stale = stale_documents(vocabulary(), source_dir)
    if stale:
        raise SystemExit("\n".join([*stale, "render them with: uv run scripts/render-catalog.py"]))


def require_current_encodings(layout: stac.StoreLayout, years: Sequence[int]) -> None:
    """Stop if any year holds an archive encoded over a range other than the metadata's.

    Every year style decodes with the current ranges, so a year tiled before a
    range changed must be tiled again first.

    Raises
    ------
    SystemExit
        Listing each stale archive and the command that re-tiles its year.
    """
    stale = [
        f"{year} {mismatch}"
        for year in years
        for key, detail in sorted(
            stac.BuildState.load(layout.build_state(year)).details.get("pmtiles", {}).items()
        )
        if (mismatch := webmap.encoding_mismatch(key, detail))
    ]
    if stale:
        hint = "re-tile those years with: make build YEAR=... ARGS='--force-from pmtiles'"
        raise SystemExit("\n".join([*stale, hint]))


def parse_args() -> argparse.Namespace:
    """Command-line arguments of the catalog finalizer."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--skip-thumbnail",
        action="store_true",
        help="reuse the thumbnails already in the store",
    )
    parser.add_argument(
        "--thumbnail-basemap",
        default=DEFAULT_BASEMAP,
        help="contextily provider name or local georeferenced basemap raster",
    )
    parser.add_argument(
        "--thumbnail-attribution",
        default=None,
        help="credit override; use an empty string only for attribution-free basemaps",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="skip the final STAC schema validation",
    )
    return parser.parse_args()


def main() -> None:
    """Assemble the collection documents, indexes and thumbnails over every built year."""
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    require_current_render(SOURCE_DIR)
    base_url = load_public_bases().https

    layout = stac.StoreLayout(settings.store_root)
    years = built_years(layout)
    require_current_encodings(layout, years)
    tables = [read_year_index(layout, year) for year in years]
    require_current_year_indexes(tables, years)
    tile_count, versions = tiles_summary(tables)
    if len(versions) > 1:
        msg = (
            f"the store's tile items carry editions {sorted(versions)}. A collection "
            "describes one edition; rebuild every year with the same one."
        )
        raise SystemExit(msg)
    logger.info("%d tile items across %d year(s)", tile_count, len(years))
    # Collect the grid before writing anything: years that disagree on a tile
    # stop the run with the store as it was.
    try:
        grid = grid_frame(tables)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    # Read the mosaic items after their styles are registered: the mosaics
    # index lists every item asset.
    publish_item_styles(
        SOURCE_DIR,
        layout,
        years,
        _load(SOURCE_DIR / MOSAICS_DIR / "collection.json").get("item_assets", {}),
    )
    mosaic_items = read_mosaic_items(layout, years)
    documents = patch_collections(SOURCE_DIR, years, mosaic_items, tiles_bbox(grid))

    # Write the indexes and thumbnails first: the collection documents record
    # their size and checksum.
    stac.concat_indexes(
        [layout.year_index(year) for year in years],
        layout.tiles_index,
        base_url=base_url,
    )
    if mosaic_items:
        stac.index_from_items(mosaic_items, layout.mosaics_index, base_url=base_url)
    write_grid(grid, layout.tiles_grid)
    logger.info("Wrote the tile grid: %d tiles", len(grid))
    if args.skip_thumbnail:
        require_existing_thumbnails(layout)
    else:
        write_collection_thumbnails(
            layout,
            years[-1],
            basemap=args.thumbnail_basemap,
            attribution=args.thumbnail_attribution,
        )

    describe_mirrors(documents, layout)
    stamp_collection_assets(documents, layout)
    write_documents(
        SOURCE_DIR,
        layout,
        documents,
        base_url=base_url,
        years=years,
        updated=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    if not args.skip_validate:
        validate(layout)

    logger.info("Store finalized at %s (published as %s)", layout.root, base_url)


if __name__ == "__main__":
    main()
