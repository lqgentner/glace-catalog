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
    uv run --env-file .env scripts/finalize-catalog.py \\
        --scope ch --base-url https://<host>/glace-ch
"""

from __future__ import annotations

import argparse
from copy import deepcopy
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
from glace_catalog.styles import YEAR_IN_TEXT
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


def style_asset(stem: str, year: int) -> tuple[str, str]:
    """Asset key and filename of the published style of one template and year.

    The template named ``default`` drops its stem, so the collection's default
    style is ``styles/2024.json`` under ``style-2024`` rather than
    ``styles/default-2024.json``. A second template keeps its stem.
    """
    if stem == "default":
        return f"style-{year}", f"{year}.json"
    return f"style-{stem}-{year}", f"{stem}-{year}.json"


def repoint_styles(source_dir: Path, staged: Path, years: Sequence[int]) -> dict[str, Path]:
    """Write one published MapLibre style per year, from the tracked template.

    The styles are tracked source — their colours, opacity and legend blocks are
    reviewed, and under M-27 they are what the *build* reads to bake an archive —
    but the year is the one part that follows from the run rather than from a
    design decision, so it is the one part rewritten here. The stretch is per
    product and does not vary by year, which is why one reviewed template can
    describe every published year; writing a copy per year is what makes every
    ``pmtiles:layers`` id resolve to a layer some published style actually draws.

    Returns
    -------
    dict[str, pathlib.Path]
        Staged path per style asset key, e.g. ``{"style-2024": …}``. The
        collection declares its digest over these bytes, not over the tracked
        ones, so the caller needs to know where they landed.
    """
    out = staged / MOSAICS_DIR / "styles"
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for path in sorted((source_dir / MOSAICS_DIR / "styles").glob("*.json")):
        template = json.loads(path.read_text())
        for year in years:
            style = deepcopy(template)
            # Every source, not just one: `default.json` declares one per
            # published layer so that a client switches by visibility rather
            # than by loading another style.
            for source in style.get("sources", {}).values():
                source["url"] = re.sub(r"/\d{4}/", f"/{year}/", source["url"])
                if source.get("attribution"):
                    # The Copernicus wording is per year, so a style describing
                    # 2022 must not credit 2024.
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
    """Replace the tracked style registrations with one entry per template and year.

    The tracked document registers the template it keeps beside it; what the
    store holds is the per-year copies :func:`repoint_styles` writes. Title,
    description and media type come from the tracked entry, so the reviewed prose
    is still the published prose, and only the latest year keeps the ``default``
    role: a client that asks for one style gets the newest.
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
            entry = {
                "href": f"./styles/{name}",
                "type": template.get("type", "application/vnd.mapbox.style+json"),
                "title": f"{template['title']} ({year})",
                "roles": ["style", "default"]
                if year == latest and stem == "default"
                else ["style"],
            }
            if template.get("description"):
                entry["description"] = template["description"]
            generated[asset_key] = entry

    first = next(iter(templates))
    rebuilt: dict[str, Document] = {}
    for key, asset in assets.items():
        if key == first:
            rebuilt.update(generated)
        if key not in templates:
            rebuilt[key] = asset
    document["assets"] = rebuilt


def check_style_coverage(document: Document, styles: Mapping[str, Path]) -> None:
    """Refuse a published archive link whose layer no published style draws.

    ``pmtiles:layers`` is a promise that a client switching that layer on finds
    it in the collection's style. Before one style was written per year, 42 of
    the 56 ids named a layer no published style held, and the only symptom was a
    legend that never appeared.

    Raises
    ------
    SystemExit
        On the first linked layer id absent from its year's style.
    """
    drawn: dict[str, set[str]] = {}
    for path in styles.values():
        match = re.search(r"(\d{4})", path.stem)
        if match is None:
            continue
        style = json.loads(path.read_text())
        ids = drawn.setdefault(match[1], set())
        ids.update(str(layer["id"]) for layer in style.get("layers", []))

    for link in document.get("links", []):
        if link.get("rel") != PMTILES_LINK_REL:
            continue
        href = str(link.get("href", ""))
        year = re.search(r"/(\d{4})/", href)
        if year is None:
            msg = f"{href} is registered as a web-map archive but names no year"
            raise SystemExit(msg)
        ids = drawn.get(year[1], set())
        for layer_id in link.get("pmtiles:layers", []):
            if layer_id not in ids:
                msg = f"{href} names {layer_id}, which styles/{year[1]}.json does not draw"
                raise SystemExit(msg)


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
        directory = layout.mosaic_year(year)
        if not directory.exists():
            continue
        for archive in sorted(directory.iterdir()):
            if archive.suffix != ".pmtiles" or not archive.stem.endswith(stac.VIZ_SUFFIX):
                continue
            layer = stac.data_key(archive.stem)
            links.append(
                {
                    "rel": PMTILES_LINK_REL,
                    "href": f"{base}/{MOSAICS_DIR}/{year}/{archive.name}",
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
    years: Sequence[int],
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

        # The prose travels with the documents that link it: PTL-FIL-001 wants
        # both files in every catalog and collection directory.
        for name in ("README.md", "AGENTS.md"):
            (staged / name).write_bytes((source_dir / name).read_bytes())
            for directory in documents:
                (staged / directory).mkdir(parents=True, exist_ok=True)
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

        # Before the documents are written: the mosaics collection declares a
        # digest over the styles it links, and the bytes a reader downloads are
        # the per-year copies -- the tracked template names one year only.
        styles = repoint_styles(source_dir, staged, years)
        register_style_assets(documents[MOSAICS_DIR], years, max(years))
        stamp_style_assets(documents, styles)
        # The links are already on the document, so this is the last chance to
        # catch one naming a layer no published style draws.
        check_style_coverage(documents[MOSAICS_DIR], styles)

        for directory, document in documents.items():
            path = staged / directory / "collection.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")

        upload_tree(staged, layout.root)


def stamped_asset_paths(
    layout: stac.StoreLayout,
) -> dict[str, dict[str, Path | UPath]]:
    """Where each collection-level asset this run measures keeps its bytes.

    Only the assets whose bytes already sit in the store are listed. The styles
    are not among them: they are staged and uploaded with the documents, so at
    this point they do not yet exist -- :func:`stamp_style_assets` measures them
    where they are written.
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


def stamp_style_assets(
    documents: Mapping[str, Document],
    styles: Mapping[str, Path],
) -> None:
    """Fill ``file:size`` and ``file:checksum`` on the staged MapLibre styles.

    The styles are the collection assets whose published bytes are written during
    this very run and differ from the tracked file: :func:`repoint_styles` writes
    one copy per year, so each digest has to be of the copy a reader downloads. It
    is measured here rather than in :func:`stamp_collection_assets`, which runs
    before the styles are staged.
    """
    for key, path in sorted(styles.items()):
        asset = documents.get(MOSAICS_DIR, {}).get("assets", {}).get(key)
        if asset is None:
            logger.warning("%s declares no %s asset for %s", MOSAICS_DIR, key, path.name)
            continue
        payload = path.read_bytes()
        asset["file:size"] = len(payload)
        asset["file:checksum"] = multihash_sha256(payload)
        logger.info("Stamped %s/%s: %d bytes", MOSAICS_DIR, key, len(payload))


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
        years=years,
    )

    if not args.skip_validate:
        validate(layout)

    logger.info("Store finalized at %s (published as %s)", layout.root, args.base_url)


if __name__ == "__main__":
    main()
