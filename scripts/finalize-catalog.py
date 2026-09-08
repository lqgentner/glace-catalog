r"""Write the store's catalogue: its root, its two collections and its indexes.

Everything that spans years lives here, and nothing else does. ``publish-year``
writes one year and never touches another; this command reads what those years
left behind and assembles the documents over them:

- the root ``catalog.json`` — the only absolute ``self`` href in the store, so
  relocating the bucket is this one run;
- ``tiles/collection.json`` and ``mosaics/collection.json``, each declaring its
  own ``item_assets`` and its collection assets;
- ``tiles/items.parquet``, the per-year indexes concatenated with their hrefs
  rebased onto ``--base-url``, and ``mosaics/items.parquet``, built straight from
  the mosaic items because one row per year leaves nothing to concatenate.

None of it describes a measurement, which is why it sits beside the items rather
than inside them.

Usage:
    uv run --env-file .env python scripts/finalize-catalog.py \\
        --scope ch --base-url https://<host>/glace-ch
"""

from __future__ import annotations

import argparse
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

from glace_catalog import stac
from glace_catalog.config import settings
from glace_catalog.scopes import get_scope
from glace_catalog.stac.items import PMTILES_LINK_REL, extent_from_items
from glace_catalog.stac.save import MOSAICS_DIR, TILES_DIR, Document, read_item
from glace_catalog.stac.store import (
    ASSETS_DIR,
    YEAR_INDEX_NAME,
    multihash_sha256,
    upload_tree,
)
from glace_catalog.thumbnails import write_collection_thumbnails

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(Path(__file__).stem)


def published_years(layout: stac.StoreLayout) -> list[int]:
    """Years the store actually holds, by the presence of their item index.

    The years sit under the tiles collection, which owns its subtree, and each
    one is published with the slice of the item mirror ``publish-year`` wrote
    beside its items.
    """
    if not layout.tiles.exists():
        msg = f"No {TILES_DIR}/ under {layout.root}. Run publish-year.py first."
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
        msg = f"No published years under {layout.tiles}. Run publish-year.py first."
        raise SystemExit(msg)
    logger.info("Found %d published year(s): %s", len(years), ", ".join(map(str, years)))
    return years


def read_year_index(layout: stac.StoreLayout, year: int) -> pa.Table:
    """Read one year's tile-item index."""
    with layout.year_index(year).open("rb") as handle:
        return pq.read_table(handle)


def tiles_summary(tables: Sequence[pa.Table]) -> tuple[int, set[str]]:
    """Count the tile items and collect their editions, without resolving them.

    A store over eleven Alpine years holds roughly 24 000 tile items. All the
    collection documents need from them is a count for the log and the assurance
    that they agree on one edition, so this reads the indexes rather than the
    item documents.
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
    """Links other than the ones this run regenerates."""
    return [link for link in links if link.get("rel") not in drop]


def patch_collections(
    source_dir: Path,
    years: Sequence[int],
    mosaic_items: Sequence[pystac.Item],
) -> dict[str, Document]:
    """Patch the tracked collection documents with what has actually been published.

    **These documents are source, not output.** Their prose, providers, license,
    ``item_assets``, ``renders`` and style registrations are written by hand and
    reviewed in git; nothing here touches them. What this adds is only what
    depends on the run: the links to the years that exist and the temporal
    extent. The run's manifest and configuration stay beside the source archive
    and in the pipeline repository; every value a reader needs from them is on
    the items as tags, ``file:size`` and ``file:checksum``.

    That split is the whole reason the catalog is kept in git. A generator that
    rewrote the descriptions would make every publication a diff nobody can
    review, and would put the dataset's prose in Python string literals.

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
    # Per-year run records used to be registered here as `manifest-{year}` and
    # `config-{year}` assets. Drop any a previous finalize left in the tracked
    # document, so a re-run converges.
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


def repoint_styles(source_dir: Path, staged: Path, latest: int) -> int:
    """Point every MapLibre style at the most recent published year.

    The styles are tracked source — their colours, opacity and legend blocks are
    reviewed, and under M-27 they are what the *build* reads to bake an archive —
    but the year in each ``pmtiles://`` source is the one part that follows from
    the run rather than from a design decision, so it is the one part rewritten
    here. The stretch is per product and does not vary by year, which is what
    lets one style describe every published year.

    Returns
    -------
    int
        Number of style files written.
    """
    out = staged / MOSAICS_DIR / "styles"
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for path in sorted((source_dir / MOSAICS_DIR / "styles").glob("*.json")):
        style = json.loads(path.read_text())
        # Every source, not just one: `default.json` declares one per published
        # layer so that a client switches by visibility rather than by loading
        # another style.
        for source in style.get("sources", {}).values():
            source["url"] = re.sub(r"/\d{4}/", f"/{latest}/", source["url"])
        style["name"] = re.sub(r"\(\d{4}\)", f"({latest})", style["name"])
        for layer in style.get("layers", []):
            layer["id"] = re.sub(r"-\d{4}$", f"-{latest}", layer["id"])
        (out / path.name).write_text(json.dumps(style, indent=2, ensure_ascii=False) + "\n")
        written += 1
    logger.info("Repointed %d style(s) at %d", written, latest)
    return written


def pmtiles_links(
    layout: stac.StoreLayout,
    years: Sequence[int],
    base_url: str,
) -> list[Document]:
    """Register every published web-map archive on the mosaics collection.

    Portolan puts PMTiles registration at collection level (``PORTO-FMT-011``),
    so a per-year archive is a link here rather than an asset of the year item.
    The ``pmtiles:layers`` array names the MapLibre style layer a client switches
    on, which is what the field is for; it is the same id the styles use.
    """
    base = base_url.rstrip("/")
    links: list[Document] = []
    for year in years:
        directory = layout.pmtiles(year)
        if not directory.exists():
            continue
        for archive in sorted(directory.iterdir()):
            if archive.suffix != ".pmtiles":
                continue
            layer = archive.stem
            links.append(
                {
                    "rel": PMTILES_LINK_REL,
                    "href": f"{base}/{MOSAICS_DIR}/pmtiles/{year}/{archive.name}",
                    "type": "application/vnd.pmtiles",
                    "title": f"{layer} web-map tiles, {year}",
                    "pmtiles:layers": [f"glace-{layer}-{year}"],
                },
            )
    logger.info("Registered %d web-map archive(s)", len(links))
    return links


def write_documents(
    source_dir: Path,
    layout: stac.StoreLayout,
    documents: Mapping[str, Document],
    *,
    base_url: str,
    latest: int,
) -> None:
    """Stage the patched source documents and upload them.

    The root catalog gains its absolute ``self`` link here and only here. A
    tracked file whose correct content depends on the deployment target is diff
    noise and a merge conflict waiting to happen, so ``public_base`` is applied
    at publication rather than committed.
    """
    base = base_url.rstrip("/")
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp)
        catalog = _load(source_dir / "catalog.json")
        catalog["links"] = [
            {"rel": "self", "href": f"{base}/catalog.json", "type": "application/json"},
            *_keep(catalog["links"], {"self"}),
        ]
        (staged / "catalog.json").write_text(
            json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
        )

        for directory, document in documents.items():
            path = staged / directory / "collection.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")

        # The prose travels with the documents that link it: PTL-FIL-001 wants
        # both files in every catalog and collection directory.
        for name in ("README.md", "AGENTS.md"):
            (staged / name).write_bytes((source_dir / name).read_bytes())
            for directory in documents:
                (staged / directory / name).write_bytes(
                    (source_dir / directory / name).read_bytes(),
                )

        # `_assets/` holds what the documents link but do not describe -- the
        # wordmark the root catalog carries as its `rel: "icon"`. It travels
        # with them because a registry that renders the icon reads the document
        # and the file from the same tree.
        assets = source_dir / ASSETS_DIR
        if assets.is_dir():
            (staged / ASSETS_DIR).mkdir(parents=True, exist_ok=True)
            for path in sorted(assets.iterdir()):
                if path.is_file():
                    (staged / ASSETS_DIR / path.name).write_bytes(path.read_bytes())

        repoint_styles(source_dir, staged, latest)
        upload_tree(staged, layout.root)


def stamped_asset_paths(
    layout: stac.StoreLayout,
) -> dict[str, dict[str, Path | UPath]]:
    """Where each collection-level asset this run measures keeps its bytes.

    Only the assets whose bytes this run writes are listed. The style
    registration is not among them: the styles are staged and uploaded with the
    documents, so at the moment a document is written they do not yet exist.
    """
    return {
        TILES_DIR: {
            "items": layout.tiles_index,
            "glacier-fractions": layout.glacier_fractions,
            "thumbnail": layout.tiles_thumbnail,
        },
        MOSAICS_DIR: {
            "items": layout.mosaics_index,
            "thumbnail": layout.mosaics_thumbnail,
        },
    }


def stamp_collection_assets(
    documents: Mapping[str, Document],
    layout: stac.StoreLayout,
) -> None:
    """Fill ``file:size`` and ``file:checksum`` on the collection-level assets.

    The assets a collection declares over itself -- its item mirror, its
    thumbnail and, for the tiles, the glacier fractions -- are written by this
    run rather than by the pipeline, so no manifest carries their digests. They
    are measured here, from the bytes as they now sit in the store, which is
    what closes ``PTL-AST-003``: a reader can check a downloaded mirror against
    the release that cites it, the same way the item assets already allow
    (R-16).

    Runs after the indexes and the thumbnails are written and before the
    documents are staged; an asset whose bytes are absent is left unstamped with
    a warning rather than stamped with a stale or guessed value, which Portolan
    treats as worse than an absent field.
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


def validate(layout: stac.StoreLayout) -> None:
    """Read the written catalogue back and validate it against the STAC schemas.

    Read through :class:`~glace_catalog.stac.save.UPathStacIO`, so a store on
    object storage is validated as it actually sits there rather than from the
    local copy that was uploaded.
    """
    stac_io = stac.UPathStacIO(UPath(layout.root).storage_options)
    catalog = pystac.Catalog.from_file(str(layout.catalog), stac_io=stac_io)
    catalog.validate()
    for child in catalog.get_children():
        child.validate()
        logger.info("%s: valid, %d item link(s)", child.id, len(child.get_links("item")))


def parse_args() -> argparse.Namespace:
    """Command-line arguments of the catalogue finalizer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True, choices=["ch", "alps"])
    parser.add_argument("--root", default=None, help="store root; defaults to the scope's")
    parser.add_argument(
        "--base-url",
        required=True,
        help="absolute URL the published root answers at; the only absolute href in the store",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "catalog",
        help="tracked catalog/ directory holding the hand-maintained documents",
    )
    parser.add_argument("--skip-thumbnail", action="store_true")
    parser.add_argument("--skip-validate", action="store_true")
    return parser.parse_args()


def main() -> None:
    """Assemble the collection documents, indexes and thumbnails over every published year."""
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    scope = get_scope(args.scope)
    layout = stac.StoreLayout(settings.resolve_path(args.root or scope.root))
    years = published_years(layout)

    tables = [read_year_index(layout, year) for year in years]
    tile_count, versions = tiles_summary(tables)
    if len(versions) > 1:
        msg = (
            f"the store's tile items carry editions {sorted(versions)}. A collection "
            "spanning two editions could not describe its own pixels; republish the "
            "whole dataset for one."
        )
        raise SystemExit(msg)
    logger.info("%d tile items across %d year(s)", tile_count, len(years))

    mosaic_items = read_mosaic_items(layout, years)
    documents = patch_collections(
        args.source,
        years,
        mosaic_items,
    )
    documents["mosaics"]["links"] += pmtiles_links(layout, years, args.base_url)

    # The indexes and the thumbnails are written before the documents are staged
    # because the documents declare their size and digest, and those can only be
    # measured on bytes that exist.
    stac.concat_indexes(
        [layout.year_index(year) for year in years],
        layout.tiles_index,
        base_url=args.base_url,
    )
    if mosaic_items:
        stac.index_from_items(mosaic_items, layout.mosaics_index, base_url=args.base_url)
    if not args.skip_thumbnail:
        write_collection_thumbnails(layout, years[-1], scope=scope)

    stamp_collection_assets(documents, layout)
    write_documents(
        args.source,
        layout,
        documents,
        base_url=args.base_url,
        latest=years[-1],
    )

    if not args.skip_validate:
        validate(layout)

    logger.info("Store finalized at %s (published as %s)", layout.root, args.base_url)


if __name__ == "__main__":
    main()
