r"""Write the store's catalogue: its root, its two collections and its indexes.

Everything that spans years lives here, and nothing else does. ``publish-year``
writes one year and never touches another; this command reads what those years
left behind and assembles the documents over them:

- the root ``catalog.json`` — the only absolute ``self`` href in the store, so
  relocating the bucket is this one run;
- ``tiles/collection.json`` and ``mosaics/collection.json``, each declaring its
  own ``item_assets`` and its collection assets;
- ``tiles.parquet``, the per-year indexes concatenated with their hrefs rebased
  onto ``--base-url``, and ``mosaics.parquet``, built straight from the mosaic
  items because one row per year leaves nothing to concatenate;
- ``layers.json``, the viewer's manifest, merged from every year's build state.
  None of it describes a measurement, which is why it sits beside the catalogue
  rather than inside it.

Usage:
    uv run --env-file .env python scripts/stac/finalize-catalog.py \\
        --scope ch --base-url https://<host>/glace-ch
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import tempfile
from typing import TYPE_CHECKING, cast

from freezebase.raster import rasterio_open
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pystac
from pystac.utils import str_to_datetime
from upath import UPath

from glace_catalog import stac
from glace_catalog.config import settings
from glace_catalog.scopes import get_scope
from glace_catalog.stac.items import extent_from_items
from glace_catalog.stac.save import MOSAICS_DIR, TILES_DIR, read_item
from glace_catalog.stac.store import (
    CONFIG_NAME,
    ITEMS_INDEX_NAME,
    MANIFEST_NAME,
    upload_tree,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(Path(__file__).stem)

THUMBNAIL_WIDTH = 1200

#: Fixed value range of the thumbnail's three bands, in decibels.
#:
#: This is the one stretch that survives whichever way the COG-versus-PMTiles
#: decision goes: the thumbnail is rendered here, so it needs a range here even
#: if every other stretch moves into the viewer's configuration.
THUMBNAIL_RANGES = ((-18.5, -5.0), (-26.0, -11.0), (4.0, 14.0))


def published_years(layout: stac.StoreLayout) -> list[int]:
    """Years the store actually holds, by the presence of their item index."""
    years: list[int] = []
    entries = cast("list[Path | UPath]", list(layout.root.iterdir()))
    for entry in sorted(entries, key=lambda path: path.name):
        if not entry.name.isdigit():
            continue
        if (entry / ITEMS_INDEX_NAME).exists():
            years.append(int(entry.name))
        else:
            logger.warning("%s has no %s; skipping", entry.name, ITEMS_INDEX_NAME)
    if not years:
        msg = f"No published years under {layout.root}. Run publish-year.py first."
        raise SystemExit(msg)
    logger.info("Found %d published year(s): %s", len(years), ", ".join(map(str, years)))
    return years


def read_year_index(layout: stac.StoreLayout, year: int) -> pa.Table:
    """Read one year's tile-item index."""
    with layout.items_index(year).open("rb") as handle:
        return pq.read_table(handle)


def tiles_summary(tables: Sequence[pa.Table]) -> tuple[pystac.Extent, list[str], set[str]]:
    """Extent, item hrefs and editions of the tile items, without resolving them.

    A store over eleven Alpine years holds roughly 24 000 tile items. The
    collection document needs their envelope and their links, not their contents,
    so this reads the indexes rather than the item documents.
    """
    bboxes: list[list[float]] = []
    starts: list[datetime] = []
    ends: list[datetime] = []
    hrefs: list[str] = []
    versions: set[str] = set()
    for table in tables:
        payload = table.select(
            ["bbox", "start_datetime", "end_datetime", "glace:mgrs_tile", "version"],
        ).to_pylist()
        for row in payload:
            bbox = row["bbox"]
            bboxes.append([bbox["xmin"], bbox["ymin"], bbox["xmax"], bbox["ymax"]])
            # The index stores these as timestamps, not as the ISO strings the
            # item documents carry.
            start, end = _as_datetime(row["start_datetime"]), _as_datetime(row["end_datetime"])
            starts.append(start)
            ends.append(end)
            hrefs.append(f"../{start.year}/{row['glace:mgrs_tile']}/item.json")
            versions.add(row["version"])
    array = np.array(bboxes)
    spatial = pystac.SpatialExtent(
        [[*array[:, :2].min(axis=0).tolist(), *array[:, 2:].max(axis=0).tolist()]],
    )
    temporal = pystac.TemporalExtent([[min(starts), max(ends)]])
    return pystac.Extent(spatial=spatial, temporal=temporal), sorted(hrefs), versions


def _as_datetime(value: datetime | str) -> datetime:
    """Read a datetime from an index column, whether stored as a timestamp or a string."""
    return value if isinstance(value, datetime) else str_to_datetime(value)


def read_mosaic_items(layout: stac.StoreLayout, years: Sequence[int]) -> list[pystac.Item]:
    """Read the per-year mosaic items; one per year, so reading them all is cheap."""
    items = []
    for year in years:
        path = layout.mosaics(year) / "item.json"
        if not path.exists():
            logger.warning("%s has no mosaic item; skipping", year)
            continue
        items.append(read_item(path))
    return items


def collection_asset(
    href: str,
    *,
    title: str,
    roles: list[str],
    media_type: str,
) -> pystac.Asset:
    """One collection-level asset, describing the collection as a whole."""
    return pystac.Asset(href=href, title=title, media_type=media_type, roles=roles)


def _load(path: Path) -> dict:
    """Read one tracked source document."""
    return json.loads(path.read_text())


def _keep(links: Sequence[dict], drop: set[str]) -> list[dict]:
    """Links other than the ones this run regenerates."""
    return [link for link in links if link.get("rel") not in drop]


def patch_collections(
    source_dir: Path,
    years: Sequence[int],
    mosaic_items: Sequence[pystac.Item],
    *,
    base_url: str,
    has_config: Mapping[int, bool],
) -> dict[str, dict]:
    """Patch the tracked collection documents with what has actually been published.

    **These documents are source, not output.** Their prose, providers, license,
    ``item_assets``, ``renders`` and style registrations are written by hand and
    reviewed in git; nothing here touches them. What this adds is only what
    depends on the run: the links to the years that exist, the per-year manifest
    and configuration assets, and the temporal extent.

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
    base_url
        Public base URL, for the per-year asset hrefs.
    has_config
        Whether each year archived its run configuration.

    Returns
    -------
    dict
        ``{"tiles": ..., "mosaics": ...}``, the patched documents.
    """
    base = base_url.rstrip("/")
    patched: dict[str, dict] = {}

    tiles = _load(source_dir / TILES_DIR / "collection.json")
    tiles["links"] = [
        *_keep(tiles["links"], {"child", "item", "self"}),
        *[stac.year_child_link(year) for year in years],
    ]
    for year in years:
        tiles.setdefault("assets", {})[f"manifest-{year}"] = {
            "href": f"{base}/{TILES_DIR}/{year}/{MANIFEST_NAME}",
            "type": "application/vnd.apache.parquet",
            "title": f"Composite run manifest, {year}",
            "description": (
                "Every file the composite run wrote that year, with its size, digest, "
                "band statistics and full provenance tag set. The run's own record; "
                "the rasters remain the source of truth and carry the same tags."
            ),
            "roles": ["metadata"],
        }
        if has_config.get(year):
            tiles["assets"][f"config-{year}"] = {
                "href": f"{base}/{TILES_DIR}/{year}/{CONFIG_NAME}",
                "type": "application/yaml",
                "title": f"Composite run configuration, {year}",
                "roles": ["metadata"],
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

    The styles are tracked source — their colours, opacity and legend stops are
    reviewed — but the year in each ``pmtiles://`` source is the one part that
    follows from the run rather than from a design decision, so it is the one
    part rewritten here.

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
        source = style["sources"]["data"]
        source["url"] = re.sub(r"/\d{4}/", f"/{latest}/", source["url"])
        style["name"] = re.sub(r"\(\d{4}\)", f"({latest})", style["name"])
        for layer in style.get("layers", []):
            layer["id"] = re.sub(r"-\d{4}$", f"-{latest}", layer["id"])
        (out / path.name).write_text(json.dumps(style, indent=2, ensure_ascii=False) + "\n")
        written += 1
    logger.info("Repointed %d style(s) at %d", written, latest)
    return written


def pmtiles_links(layout: stac.StoreLayout, years: Sequence[int], base_url: str) -> list[dict]:
    """Register every published web-map archive on the mosaics collection.

    Portolan puts PMTiles registration at collection level (``PORTO-FMT-011``),
    so a per-year archive is a link here rather than an asset of the year item.
    The ``pmtiles:layers`` array names the MapLibre style layer a client switches
    on, which is what the field is for; it is the same id the styles use.
    """
    base = base_url.rstrip("/")
    links: list[dict] = []
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
                    "rel": "pmtiles",
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
    documents: Mapping[str, dict],
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

        repoint_styles(source_dir, staged, latest)
        upload_tree(staged, layout.root)


def render_thumbnail(layout: stac.StoreLayout, years: Sequence[int]) -> bool:
    """Render the store thumbnail from the most recent year's false-colour mosaic.

    Under the PMTiles branch this could borrow ``rtc_rgb``'s stretch; under the
    COG one there is no such layer, so the range is fixed here in
    :data:`THUMBNAIL_RANGES` and stays wherever the rest of the stretches go.
    """
    from PIL import Image  # noqa: PLC0415

    year = max(years)
    sources = [layout.mosaics(year) / f"rtc_{pol}.tif" for pol in ("vv", "vh")]
    if not all(path.exists() for path in sources):
        logger.warning("No RTC mosaics for %d; thumbnail not rendered", year)
        return False

    bands = []
    for path in sources:
        with rasterio_open(path) as src:
            scale = max(1, src.width // THUMBNAIL_WIDTH)
            bands.append(
                src.read(
                    1,
                    out_shape=(src.height // scale, src.width // scale),
                    masked=True,
                ),
            )
    with np.errstate(divide="ignore", invalid="ignore"):
        vv, vh = (10.0 * np.log10(np.where(band > 0, band, np.nan)) for band in bands)
    channels = (vv, vh, vv - vh)
    rgba = np.zeros((*vv.shape, 4), dtype="uint8")
    for index, (channel, (vmin, vmax)) in enumerate(zip(channels, THUMBNAIL_RANGES, strict=True)):
        # Nodata is transparent, so its colour is irrelevant; zero it before the
        # cast rather than casting a NaN, which is undefined.
        scaled = np.nan_to_num((channel - vmin) / (vmax - vmin) * 255, nan=0.0)
        rgba[..., index] = np.clip(scaled, 0, 255).astype("uint8")
    rgba[..., 3] = np.where(np.isfinite(vv), 255, 0).astype("uint8")

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "thumbnail.png"
        Image.fromarray(rgba, mode="RGBA").save(staged)
        layout.thumbnail.write_bytes(staged.read_bytes())
    logger.info("Wrote %s from %d (%dx%d)", layout.thumbnail, year, rgba.shape[1], rgba.shape[0])
    return True


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
    _extent, tile_hrefs, versions = tiles_summary(tables)
    if len(versions) > 1:
        msg = (
            f"the store's tile items carry editions {sorted(versions)}. A collection "
            "spanning two editions could not describe its own pixels; republish the "
            "whole dataset for one."
        )
        raise SystemExit(msg)
    logger.info("%d tile items across %d year(s)", len(tile_hrefs), len(years))

    mosaic_items = read_mosaic_items(layout, years)
    documents = patch_collections(
        args.source,
        years,
        mosaic_items,
        base_url=args.base_url,
        has_config={year: layout.config(year).exists() for year in years},
    )
    documents["mosaics"]["links"] += pmtiles_links(layout, years, args.base_url)
    write_documents(
        args.source,
        layout,
        documents,
        base_url=args.base_url,
        latest=years[-1],
    )

    stac.concat_indexes(
        [layout.year_index(year) for year in years],
        layout.tiles_index,
        base_url=args.base_url,
    )
    if mosaic_items:
        stac.index_from_items(mosaic_items, layout.mosaics_index, base_url=args.base_url)

    if not args.skip_thumbnail:
        render_thumbnail(layout, years)
    if not args.skip_validate:
        validate(layout)

    logger.info("Store finalized at %s (published as %s)", layout.root, args.base_url)


if __name__ == "__main__":
    main()
