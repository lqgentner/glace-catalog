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
import tempfile
from typing import TYPE_CHECKING, Any, cast

from freezebase.raster import rasterio_open
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pystac
from pystac.utils import str_to_datetime
from upath import UPath

from glace_catalog import stac, webmap
from glace_catalog.config import settings
from glace_catalog.scopes import get_scope
from glace_catalog.stac.items import extent_from_items
from glace_catalog.stac.save import read_item
from glace_catalog.stac.store import ITEMS_INDEX_NAME, upload_tree

if TYPE_CHECKING:
    from collections.abc import Sequence

    from glace_catalog.scopes import Scope

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


def build_collections(
    scope: Scope,
    extent: pystac.Extent,
    tile_hrefs: Sequence[str],
    mosaic_items: Sequence[pystac.Item],
) -> tuple[pystac.Collection, pystac.Collection]:
    """Build both collection documents, with their links and collection assets."""
    tiles = stac.build_tiles_collection(scope, extent=extent)
    stac.save.add_item_links(tiles, tile_hrefs)
    tiles.add_asset(
        "items",
        collection_asset(
            "../tiles.parquet",
            title="stac-geoparquet index of every tile item",
            roles=["stac-items"],
            media_type="application/vnd.apache.parquet",
        ),
    )
    tiles.add_asset(
        "glacier-fractions",
        collection_asset(
            "../glacier-fractions.parquet",
            title="Per-tile RGI7 glacier fractions",
            roles=["metadata"],
            media_type="application/vnd.apache.parquet",
        ),
    )

    mosaics = stac.build_mosaics_collection(
        scope,
        extent=extent_from_items(mosaic_items) if mosaic_items else extent,
    )
    stac.save.add_item_links(
        mosaics,
        [f"../{item.properties['start_datetime'][:4]}/mosaics/item.json" for item in mosaic_items],
    )
    mosaics.add_asset(
        "items",
        collection_asset(
            "../mosaics.parquet",
            title="stac-geoparquet index of every annual mosaic item",
            roles=["stac-items"],
            media_type="application/vnd.apache.parquet",
        ),
    )
    mosaics.add_asset(
        "thumbnail",
        collection_asset(
            "../thumbnail.png",
            title="GLACE overview",
            roles=["thumbnail"],
            media_type=pystac.MediaType.PNG,
        ),
    )
    return tiles, mosaics


def write_documents(
    scope: Scope,
    layout: stac.StoreLayout,
    collections: tuple[pystac.Collection, pystac.Collection],
    *,
    base_url: str,
) -> None:
    """Write the root catalog and both collection documents, and upload them.

    Only these three documents; every item was written by ``publish-year`` with
    links that already point here, so nothing per-year is rewritten.
    """
    tiles, mosaics = collections
    catalog = pystac.Catalog(
        id=f"glace-{scope.key}",
        title=f"GLACE: Sentinel-1 glacier composites, {scope.title}",
        description=(
            f"GLACE over {scope.title}: annual Sentinel-1 coherence and backscatter "
            "composites on the 10 km MGRS grid, their annual mosaics on the "
            "WebMercatorQuad grid, and the web-map tiles rendered from them."
        ),
    )
    catalog.add_link(
        pystac.Link(
            rel="preview",
            target="./thumbnail.png",
            media_type=pystac.MediaType.PNG,
            title="GLACE overview",
        ),
    )
    for collection in (tiles, mosaics):
        catalog.add_child(collection)

    base = base_url.rstrip("/")
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp)
        catalog_payload = catalog.to_dict(include_self_link=False)
        catalog_payload["links"] = [
            {"rel": "self", "href": f"{base}/catalog.json", "type": "application/json"},
            {"rel": "root", "href": "./catalog.json", "type": "application/json"},
            {"rel": "child", "href": "./tiles/collection.json", "type": "application/json"},
            {"rel": "child", "href": "./mosaics/collection.json", "type": "application/json"},
            *[
                link
                for link in catalog_payload["links"]
                if link["rel"] not in {"self", "root", "child"}
            ],
        ]
        (staged / "catalog.json").write_text(json.dumps(catalog_payload, indent=2) + "\n")

        for directory, collection in (("tiles", tiles), ("mosaics", mosaics)):
            payload = collection.to_dict(include_self_link=False)
            payload["links"] = [
                {"rel": "root", "href": "../catalog.json", "type": "application/json"},
                {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
                *[
                    link
                    for link in payload["links"]
                    if link["rel"] not in {"self", "root", "parent"}
                ],
            ]
            path = staged / directory / "collection.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2) + "\n")

        upload_tree(staged, layout.root)


def write_layers_manifest(layout: stac.StoreLayout, years: Sequence[int]) -> int:
    """Merge every year's build state into the viewer's ``layers.json``.

    The build state already records everything the viewer needs — a layer id, a
    colour map, a value range, a zoom range, a size — as the ``pmtiles`` step
    produces it, and this is the only command that sees all the years, so it owns
    the merge. The layer id is the archive's filename stem, which the naming rules
    make a contract rather than an assumption.
    """
    entries: list[dict[str, Any]] = []
    for year in years:
        state = stac.BuildState.load(layout.build_state(year))
        entries.extend(
            {**detail, "url": f"{year}/pmtiles/{detail['url']}"}
            for _, detail in sorted(state.details.get("pmtiles", {}).items())
        )
    if not entries:
        logger.info("No web-map archives recorded; layers.json not written")
        return 0
    manifest = webmap.build_viewer_manifest(entries)
    layout.layers.write_text(json.dumps(manifest, indent=2) + "\n")
    logger.info("Wrote %s with %d layers", layout.layers, len(entries))
    return len(entries)


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
    extent, tile_hrefs, versions = tiles_summary(tables)
    if len(versions) > 1:
        msg = (
            f"the store's tile items carry editions {sorted(versions)}. A collection "
            "spanning two editions could not describe its own pixels; republish the "
            "whole dataset for one."
        )
        raise SystemExit(msg)
    logger.info("%d tile items across %d year(s)", len(tile_hrefs), len(years))

    mosaic_items = read_mosaic_items(layout, years)
    collections = build_collections(scope, extent, tile_hrefs, mosaic_items)
    write_documents(scope, layout, collections, base_url=args.base_url)

    stac.concat_indexes(
        [layout.items_index(year) for year in years],
        layout.tiles_index,
        base_url=args.base_url,
    )
    if mosaic_items:
        stac.index_from_items(mosaic_items, layout.mosaics_index, base_url=args.base_url)

    write_layers_manifest(layout, years)
    if not args.skip_thumbnail:
        render_thumbnail(layout, years)
    if not args.skip_validate:
        validate(layout)

    logger.info("Store finalized at %s (published as %s)", layout.root, args.base_url)


if __name__ == "__main__":
    main()
