r"""Write the store's root catalog, collections and item indexes over every built year.

``build-year.py`` writes one year at a time. This script reads the built
years and writes what spans them:

- the root ``catalog.json``, with an absolute ``self`` link under the public
  address, ``public_base`` in ``catalog.publish.yaml``;
- ``tiles/collection.json`` and ``mosaics/collection.json``, with links to each
  year and the temporal extent; the mosaics collection also lists one MapLibre
  style per year;
- the run's time as ``updated`` on the root catalog and both collections;
- a thumbnail per collection, drawn from the latest year's COGs;
- ``tiles/grid.pmtiles``, the tile grid with each tile's glacier fractions,
  and its style ``tiles/styles/grid.json``;
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

logger = logging.getLogger(Path(__file__).stem)

SOURCE_DIR = Path(__file__).resolve().parent.parent / "catalog"
"""The tracked documents, rendered from ``metadata/``."""


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


def patch_collections(
    source_dir: Path,
    years: Sequence[int],
    mosaic_items: Sequence[pystac.Item],
) -> dict[str, Document]:
    """Add the built years to the tracked collection documents.

    ``scripts/render-catalog.py`` renders these documents from ``metadata/``,
    and they are reviewed in git. This function adds only what depends on the
    published data: a link to each year and the temporal extent. Prose,
    providers, license and ``item_assets`` stay as rendered.

    Parameters
    ----------
    source_dir
        The tracked ``catalog/`` directory.
    years
        Published years, ascending.
    mosaic_items
        The mosaic items, read back from the store, for the temporal extent.

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


def style_asset(stem: str, year: int) -> tuple[str, str]:
    """Asset key and filename of the published style of one template and year.

    Styles from the ``default`` template are named by year alone:
    ``styles/2024.json`` under the key ``style-2024``. Other templates keep
    their name: ``styles/{stem}-2024.json`` under ``style-{stem}-2024``.
    """
    if stem == "default":
        return f"style-{year}", f"{year}.json"
    return f"style-{stem}-{year}", f"{stem}-{year}.json"


def repoint_styles(source_dir: Path, staged: Path, years: Sequence[int]) -> dict[str, Path]:
    """Write one MapLibre style per year from each tracked style template.

    The templates are rendered from ``metadata/assets.yaml`` and reviewed in
    git. Encoding and colour ramp are the same every year, so each copy changes
    only the year: in the source URLs, the attribution, the style name and the
    layer ids. Every published year thus has a style that draws its archives.

    Returns
    -------
    dict[str, pathlib.Path]
        Staged path per style asset key, e.g. ``{"style-2024": …}``. The
        caller records each file's size and checksum.
    """
    out = staged / MOSAICS_DIR / "styles"
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for path in sorted((source_dir / MOSAICS_DIR / "styles").glob("*.json")):
        template = json.loads(path.read_text())
        for year in years:
            style = deepcopy(template)
            # A style has one source per layer; clients switch layers by visibility.
            for source in style.get("sources", {}).values():
                source["url"] = re.sub(r"/\d{4}/", f"/{year}/", source["url"])
                if source.get("attribution"):
                    # The Copernicus credit names the year of the data.
                    source["attribution"] = YEAR_IN_TEXT.sub(str(year), source["attribution"])
            style["name"] = re.sub(r"\(\d{4}\)", f"({year})", style["name"])
            for layer in style.get("layers", []):
                layer["id"] = re.sub(r"-\d{4}$", f"-{year}", layer["id"])
            key, name = style_asset(path.stem, year)
            (out / name).write_text(json.dumps(style, indent=2, ensure_ascii=False) + "\n")
            written[key] = out / name
    logger.info("Wrote %d style(s) over %d year(s)", len(written), len(years))
    return written


def register_style_assets(document: Document, years: Sequence[int], latest: int) -> None:
    """Replace each tracked style asset with one asset per year.

    The new assets point at the per-year copies :func:`repoint_styles` writes.
    Title and media type come from the tracked entry, and the description is
    the per-year wording from ``metadata/``. If the template has the
    ``default`` role, its latest year's copy keeps it, so a client asking for
    one style gets the newest.
    """
    assets: dict[str, Document] = document.get("assets", {})
    templates = {key: asset for key, asset in assets.items() if "style" in asset.get("roles", [])}
    if not templates:
        logger.warning("%s registers no style asset", MOSAICS_DIR)
        return

    generated: dict[str, Document] = {}
    for key, template in templates.items():
        stem = key.removeprefix("style-")
        for year in years:
            asset_key, name = style_asset(stem, year)
            is_default = year == latest and "default" in template.get("roles", [])
            generated[asset_key] = {
                "href": f"./styles/{name}",
                "type": template.get("type", "application/vnd.mapbox.style+json"),
                "title": f"{template['title']} ({year})",
                "description": vocabulary().style_description(year),
                "roles": ["style", "default"] if is_default else ["style"],
            }

    first = next(iter(templates))
    rebuilt: dict[str, Document] = {}
    for key, asset in assets.items():
        if key == first:
            rebuilt.update(generated)
        if key not in templates:
            rebuilt[key] = asset
    document["assets"] = rebuilt


def check_style_coverage(
    layout: stac.StoreLayout,
    years: Sequence[int],
    styles: Mapping[str, Path],
) -> None:
    """Check that every published web-map archive has a layer in its year's style.

    A client finds an archive as a ``visual`` asset of the year's mosaic item
    and draws it through that year's style, as layer ``glace-{key}-{year}``.

    Raises
    ------
    SystemExit
        On the first archive whose layer is absent from its year's style.
    """
    drawn: dict[int, set[str]] = {}
    for path in styles.values():
        match = re.search(r"(\d{4})", path.stem)
        if match is None:
            continue
        style = json.loads(path.read_text())
        ids = drawn.setdefault(int(match[1]), set())
        ids.update(str(layer["id"]) for layer in style.get("layers", []))

    archives = 0
    for year in years:
        directory = layout.mosaic_year(year)
        if not directory.exists():
            continue
        for archive in sorted(directory.iterdir()):
            if archive.suffix != ".pmtiles" or not archive.stem.endswith(stac.VIZ_SUFFIX):
                continue
            layer_id = f"glace-{stac.data_key(archive.stem)}-{year}"
            if layer_id not in drawn.get(year, set()):
                msg = (
                    f"{MOSAICS_DIR}/{year}/{archive.name} is published, but "
                    f"styles/{year}.json draws no layer {layer_id}"
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

        # Write the styles first: the mosaics collection records the size and
        # checksum of each per-year copy.
        styles = repoint_styles(source_dir, staged, years)
        register_style_assets(documents[MOSAICS_DIR], years, max(years))
        stamp_style_assets(documents, styles)
        stamp_style_assets(documents, stage_grid_styles(source_dir, staged), TILES_DIR)
        # Every archive must have a layer in its year's style.
        check_style_coverage(layout, years, styles)

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
    download: the per-year copies :func:`repoint_styles` wrote, or the tile
    grid's style.
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


def stage_grid_styles(source_dir: Path, staged: Path) -> dict[str, Path]:
    """Copy the tiles collection's styles, which do not depend on the year.

    Returns
    -------
    dict[str, pathlib.Path]
        Staged path per style asset key, e.g. ``{"style-grid": …}``.
    """
    out = staged / TILES_DIR / "styles"
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for path in sorted((source_dir / TILES_DIR / "styles").glob("*.json")):
        (out / path.name).write_bytes(path.read_bytes())
        written[f"style-{path.stem}"] = out / path.name
    return written


def require_no_published_fractions(layout: stac.StoreLayout) -> None:
    """Check that the glacier-fraction table is under its leading dot.

    Publishing uploads every file without one, and the table is a build input,
    not an asset: an old store would publish it unregistered.

    Raises
    ------
    SystemExit
        If ``tiles/glacier-fractions.parquet`` is in the store.
    """
    stray = layout.tiles / "glacier-fractions.parquet"
    if stray.exists():
        msg = (
            f"{stray} would be published. Rename it to "
            f"{layout.glacier_fractions.name} (on object storage, copy it there and "
            "delete the original), then finalize again"
        )
        raise SystemExit(msg)


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
    require_no_published_fractions(layout)

    tables = [read_year_index(layout, year) for year in years]
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

    mosaic_items = read_mosaic_items(layout, years)
    documents = patch_collections(
        SOURCE_DIR,
        years,
        mosaic_items,
    )

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
