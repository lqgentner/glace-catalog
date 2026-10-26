r"""Build one year of GLACE composites into the store.

The year is built in five steps, run in this order:

``copy``
    Check the run manifest against the source archive, accept only the
    composites the catalog publishes, and copy them server-side, unchanged.
``vrt``
    Build temporary VRTs that mosaic the tiles onto the scope's grid, in linear
    values and in decibels.
``mosaics``
    Write the twelve annual mosaic COGs.
``pmtiles``
    Write the twelve web-map archives.
``items``
    Write the year's tile items, its mosaic item and its GeoParquet index.

Each step records a fingerprint of its inputs. A rerun skips a step whose
inputs are unchanged and rebuilds one whose inputs changed; ``--force-from``
rebuilds regardless.

The collections and the root catalog span all years, so
``finalize-catalog.py`` writes them. Adding a year therefore leaves the other
years untouched.

The scope comes from ``metadata/catalog.yaml``; the source archive
(``GLACE_SOURCE``), the store (``GLACE_STORE_ROOT``) and the scratch cache
(``FREEZEBASE_CACHE``) come from the environment.

Usage:
    make build YEAR=2023
    uv run --env-file .env scripts/build-year.py 2023
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, replace
import logging
from pathlib import Path
import shutil
import tempfile
from typing import TYPE_CHECKING, Any

from freezebase.s3 import list_object_sizes

from glace_catalog import mosaics, stac, tiler, webmap
from glace_catalog.config import settings
from glace_catalog.documents import render_year_prose
from glace_catalog.grid import read_glacier_fractions
from glace_catalog.metadata import vocabulary
from glace_catalog.stac.naming import ASSET_KEYS, published_name
from glace_catalog.stac.save import write_year_items
from glace_catalog.stac.store import MANIFEST_NAME, STEPS, YEAR_INDEX_NAME

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from datetime import date

    import pandas as pd

    from glace_catalog.scopes import Grid, Scope

logger = logging.getLogger(Path(__file__).stem)

CATALOGUE_SCHEMA_VERSION = 6
"""Version of the item documents' layout. Bump it to make the ``items`` step rerun."""


def load_and_verify_manifest(src: Any, year: int) -> tuple[pd.DataFrame, list[stac.CompositeFile]]:
    """Read the run manifest, check it against the archive, and refuse a bad one.

    All checks run before anything is copied. The manifest must match a listing
    of the archive, each file must map to its own published name, and every
    raster must hold linear values and carry the required tags.

    Raises
    ------
    SystemExit
        If the manifest is absent, disagrees with the archive, or describes
        composites the catalog cannot publish.
    """
    manifest_path = src / MANIFEST_NAME
    if not manifest_path.exists():
        msg = (
            f"{manifest_path} is absent. The composite run in deep-glacier-mapping "
            "emits it; rerunning the year over a complete archive writes one too."
        )
        raise SystemExit(msg)

    frame = stac.read_manifest(manifest_path)
    listing = list_object_sizes(src)
    mismatches = stac.verify_manifest(frame, listing)
    if mismatches:
        for mismatch in mismatches[:50]:
            logger.error("%s", mismatch)
        msg = f"manifest and archive disagree about {len(mismatches)} file(s) in {src}"
        raise SystemExit(msg)

    files = stac.manifest_composites(frame, src)
    # Only the twelve composites described in metadata/assets.yaml are published.
    # Any other (a per-pass composite, say) would fail at the items step, after
    # the copy, so refuse it now.
    unpublishable = sorted({file.asset_key for file in files if file.asset_key not in ASSET_KEYS})
    if unpublishable:
        msg = (
            f"{src} holds composites the catalog does not publish: {', '.join(unpublishable)}. "
            "Only the LRW composites named in metadata/assets.yaml are published."
        )
        raise SystemExit(msg)
    by_tile: dict[str, list[stac.CompositeFile]] = {}
    for file in files:
        by_tile.setdefault(file.tile, []).append(file)
    stac.require_unique_composites(by_tile)

    problems = stac.survey_tags(by_tile, stac.manifest_raster_info(frame))
    if problems:
        for problem in problems:
            logger.error("%s", problem)
        logger.error("%s", stac.PROVENANCE_EXPECTATION)
        msg = f"{len(problems)} of {len(files)} composites cannot be published"
        raise SystemExit(msg)

    try:
        window = composite_window(frame)
    except ValueError as error:
        msg = f"{src}: {error}"
        raise SystemExit(msg) from None
    if window[0].year != year:
        msg = f"{src} holds composites of the window {window[0]}..{window[1]}, not of {year}"
        raise SystemExit(msg)

    logger.info("Manifest verified: %d files across %d tiles", len(files), len(by_tile))
    return frame, files


def without_empty_composites(
    frame: pd.DataFrame,
    files: list[stac.CompositeFile],
) -> tuple[pd.DataFrame, list[stac.CompositeFile]]:
    """Leave out the measurements with no valid pixel, and their QA layers.

    Raises
    ------
    SystemExit
        If too many are empty, which points at a broken composite run.
    """
    try:
        kept, dropped = stac.drop_empty_composites(frame)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    for label in dropped:
        logger.warning("%s holds no valid pixel; not published, nor its QA layers", label)
    names = set(kept["filename"].astype(str))
    return kept, [file for file in files if file.path.name in names]


def step_copy(
    args: argparse.Namespace,
    layout: stac.StoreLayout,
    files: Sequence[stac.CompositeFile],
    src: Any,
) -> None:
    """Copy every composite into the store, byte for byte.

    The run's manifest and configuration stay with the source archive. What a
    reader needs from them is on the items as tags, ``file:size`` and
    ``file:checksum``.
    """
    layout.tile_year(args.year).mkdir(parents=True, exist_ok=True)
    listing = list_object_sizes(src)
    result = stac.copy_composites(files, layout, args.year, listing, max_workers=args.max_workers)
    logger.info("copy: %s", result)


def published_files(
    files: Sequence[stac.CompositeFile],
    layout: stac.StoreLayout,
    year: int,
) -> list[stac.CompositeFile]:
    """Point the composites at their copies in the store.

    The steps after ``copy`` read from the store, so the products can be
    rebuilt after the source archive is deleted.
    """
    return [
        replace(file, path=layout.tile(year, file.tile) / file.published_name) for file in files
    ]


def step_vrt(
    args: argparse.Namespace,
    scope: Scope,
    layout: stac.StoreLayout,
    files: Sequence[stac.CompositeFile],
    *,
    grid: Grid | None = None,
    name: str = "vrt",
) -> mosaics.YearVrts:
    """Build one of the year's two VRT chains in the local cache.

    The mosaics use the scope's equal-area analysis grid and the web maps its
    Web Mercator display grid. Both chains start from the UTM composites, so
    each product is resampled once.

    The VRTs are always rebuilt: they are local, temporary and take seconds,
    and the cache may have been emptied since the last run.
    """
    vrt_dir = args.cache / "glace-store" / str(args.year) / name
    chain = mosaics.build_year_vrts(
        published_files(files, layout, args.year),
        scope,
        vrt_dir,
        grid=grid,
    )
    logger.info("vrt: %d layer(s)", len(chain.linear))
    return chain


def composite_window(frame: pd.DataFrame) -> tuple[date, date]:
    """Read the year's compositing window, first and last day inclusive, from the manifest.

    Every row must carry the same window; see
    :func:`glace_catalog.stac.composite_window`.
    """
    return stac.composite_window(
        zip(frame["composite_start"], frame["composite_end"], strict=True)
    )


def mosaic_tags(frame: pd.DataFrame, asset_key: str) -> dict[str, str]:
    """Provenance tags a mosaic takes from its source tiles.

    A tag is kept only when every contributing tile has the same value.
    """
    rows = frame.loc[frame["asset_key"] == asset_key]
    tags: dict[str, str] = {}
    # PROVENANCE_TAGS covers every product; use the ones this manifest has.
    for column in stac.PROVENANCE_TAGS:
        if column not in rows:
            continue
        values = {str(value) for value in rows[column].dropna().unique()}
        if len(values) == 1:
            tags[column] = values.pop()
    start, end = composite_window(rows)
    tags["COMPOSITE_START_DATE"] = start.isoformat()
    tags["COMPOSITE_END_DATE"] = end.isoformat()
    # Acquisition dates differ per tile; the mosaic spans the earliest to the latest.
    for tag, pick in (("FIRST_ACQUISITION_DATE", min), ("LAST_ACQUISITION_DATE", max)):
        if tag in rows and rows[tag].notna().any():
            tags[tag] = str(pick(rows[tag].dropna()))
    tags["MGRS_TILE"] = "N/A -- mosaic of many tiles"
    if asset_key.endswith("_qa_num"):
        # Resampling turns the per-tile counts into a mean, so describe it as one.
        tags["LAYER_DESCRIPTION"] = (
            "Mean number of contributing observations per output pixel, averaged "
            "from the 10 m per-tile counts."
        )
    return tags


@contextmanager
def staging(args: argparse.Namespace, step: str) -> Iterator[Path]:
    """Yield an empty local directory for one step's outputs; remove it afterwards.

    The step uploads the whole directory, so a new one per run uploads only
    this run's files. Removing it after the upload keeps the cache from
    growing by a year of mosaics and archives with every build.
    """
    parent = args.cache / "glace-store" / str(args.year)
    parent.mkdir(parents=True, exist_ok=True)
    # A killed run cannot remove its directory; one year has one build at a time.
    for leftover in parent.glob(f"{step}-*"):
        shutil.rmtree(leftover)
    with tempfile.TemporaryDirectory(dir=parent, prefix=f"{step}-") as tmp:
        yield Path(tmp)


def step_mosaics(
    args: argparse.Namespace,
    scope: Scope,
    layout: stac.StoreLayout,
    chain: mosaics.YearVrts,
    frame: pd.DataFrame,
) -> dict[str, dict[str, Any]]:
    """Write and upload the year's twelve mosaic COGs; return their build-state details."""
    tags = {key: mosaic_tags(frame, key) for key in chain.linear}
    with staging(args, "mosaics") as staged:
        written = mosaics.build_mosaic_cogs(
            chain,
            staged,
            tags=tags,
            layers=args.layers,
            workers=args.mosaic_workers,
        )
        stac.store.upload_tree(staged, layout.mosaic_year(args.year))
        return {
            key: {
                "size": write.path.stat().st_size,
                "resolution": scope.analysis_grid.resolution,
                "checksum": write.checksum,
                "statistics": write.statistics.to_dict(),
            }
            for key, write in written.items()
        }


def step_pmtiles(
    args: argparse.Namespace,
    layout: stac.StoreLayout,
    chain: mosaics.YearVrts,
    files: Sequence[stac.CompositeFile],
) -> dict[str, dict[str, Any]]:
    """Render and upload the year's web-map archives; return their build-state details.

    Each archive's header declares the bbox of the mosaic item, the extent of
    the data rather than of the tile-aligned display grid.
    """
    _, bbox = mosaics.data_footprint(files)
    with staging(args, "pmtiles") as staged, staging(args, "rgba") as scratch:
        layers = webmap.build_layers(
            chain,
            staged,
            year=args.year,
            layers=args.layers,
            workers=args.tiler_workers,
            concurrent_layers=args.concurrent_layers,
            # Outside `staged`, which is uploaded whole.
            scratch=scratch,
            bounds=bbox,
        )
        # The archives are assets of the year's mosaic item, so they go beside
        # its COGs. `upload_tree` adds and overwrites files and leaves the rest.
        stac.store.upload_tree(staged, layout.mosaic_year(args.year))
    return {layer.url.removesuffix(".pmtiles"): asdict(layer) for layer in layers}


def scratch_needed(args: argparse.Namespace, scope: Scope, steps: Sequence[str]) -> int:
    """Estimate the local scratch, in bytes, that ``steps`` need at their peak.

    Measured on the Alps in 2026: a year's staged mosaics take about 7 bytes
    per analysis-grid pixel; while tiling, each concurrently warped RGBA raster
    takes about 0.4 bytes and the staged archives together about 0.6 bytes per
    display-grid pixel. The estimate adds half again as a margin.
    """
    layers = len(args.layers) if args.layers else len(ASSET_KEYS)
    share = layers / len(ASSET_KEYS)
    analysis = scope.analysis_grid.width * scope.analysis_grid.height
    display = scope.display_grid.width * scope.display_grid.height
    need = 0.0
    if "mosaics" in steps:
        need = max(need, 7 * share * analysis)
    if "pmtiles" in steps:
        warps = min(max(1, args.concurrent_layers), layers)
        need = max(need, (0.4 * warps + 0.6 * share) * display)
    return int(1.5 * need)


def require_scratch_space(cache: Path, needed: int) -> None:
    """Stop before any work if ``cache`` has less free space than ``needed`` bytes.

    Raises
    ------
    SystemExit
        Naming the cache, its free space and the estimate.
    """
    cache.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(cache).free
    if free < needed:
        msg = (
            f"the cache {cache} has {free / 1e9:.1f} GB free, but this run needs about "
            f"{needed / 1e9:.1f} GB; set FREEZEBASE_CACHE to a volume with more room"
        )
        raise SystemExit(msg)


def step_items(
    args: argparse.Namespace,
    scope: Scope,
    layout: stac.StoreLayout,
    frame: pd.DataFrame,
    files: Sequence[stac.CompositeFile],
    state: stac.BuildState,
    build_fingerprint: str,
) -> None:
    """Write the year's tile items, its mosaic item and its GeoParquet index.

    The mosaic item's assets come from the build state, so it lists exactly the
    mosaics and archives that earlier steps wrote.
    """
    raster_info = stac.manifest_raster_info(frame)
    percents = stac.manifest_valid_percents(frame)
    try:
        fractions = read_glacier_fractions(layout.glacier_fractions, scope)
    except (FileNotFoundError, ValueError) as error:
        raise SystemExit(str(error)) from error

    by_tile: dict[str, list[stac.CompositeFile]] = {}
    for file in files:
        by_tile.setdefault(file.tile, []).append(file)

    missing = sorted(set(by_tile) - set(fractions))
    if missing:
        msg = (
            f"{len(missing)} tile(s) have no precomputed glacier fraction "
            f"({', '.join(missing[:5])}...). Regenerate the table for this scope."
        )
        raise SystemExit(msg)

    tile_items = [
        stac.build_tile_item(
            tile,
            sorted(tile_files, key=stac.by_asset_key),
            raster_info[tile],
            glacier_fraction=fractions[tile][0],
            glacier_fraction_buffered=fractions[tile][1],
            valid_percents=percents.get(tile),
        )
        for tile, tile_files in sorted(by_tile.items())
    ]

    start, end = stac.published_time_range(composite_window(frame))
    geometry, bbox = mosaics.data_footprint(files)
    mosaic_item = stac.build_mosaic_item(
        scope,
        args.year,
        geometry=geometry,
        bbox=bbox,
        start=start,
        end=end,
        assets={**mosaic_assets(state, frame), **visual_assets(state)},
        build_fingerprint=build_fingerprint,
    )
    mosaic_item.properties["description"] = vocabulary().mosaic_item_description(args.year)

    with tempfile.TemporaryDirectory() as tmp:
        staged_tiles = Path(tmp) / "tiles"
        staged_mosaics = Path(tmp) / "mosaics"
        write_year_items(
            tile_items,
            mosaic_item,
            staged_tiles,
            staged_mosaics,
            year=args.year,
            year_prose=render_year_prose(vocabulary(), args.year),
        )
        # Hrefs relative to the store root; finalize-catalog makes them absolute.
        stac.write_stac_geoparquet(
            [stac.year_index_document(item) for item in tile_items],
            staged_tiles / YEAR_INDEX_NAME,
        )
        stac.store.upload_tree(staged_tiles, layout.tile_year(args.year))
        stac.store.upload_tree(staged_mosaics, layout.mosaic_year(args.year))
    logger.info("items: %d tile items and one mosaic item", len(tile_items))


def mosaic_assets(
    state: stac.BuildState,
    frame: pd.DataFrame,
) -> dict[str, stac.MosaicAsset]:
    """Build the mosaic item's COG assets from what the ``mosaics`` step recorded.

    Title, description and roles come from ``metadata/assets.yaml``; size,
    checksum, statistics and resolution come from the build state.
    """
    written = state.details.get("mosaics", {})
    assets: dict[str, stac.MosaicAsset] = {}
    for asset_key, detail in sorted(written.items()):
        entry = vocabulary().item_asset(asset_key, "mosaics")
        band: dict[str, Any] = dict(entry["bands"][0])
        unit = frame.loc[frame["asset_key"] == asset_key, "band_unit"]
        if not asset_key.endswith("_qa_num") and len(unit) and str(unit.iloc[0]):
            band["unit"] = str(unit.iloc[0])
        # The statistics the writer embedded in the COG.
        statistics = detail.get("statistics")
        if statistics:
            band["statistics"] = statistics
        assets[asset_key] = stac.MosaicAsset(
            href=published_name(asset_key),
            title=entry["title"],
            description=entry["description"],
            roles=list(entry["roles"]),
            size=detail.get("size"),
            checksum=detail.get("checksum"),
            band=band,
            extra={"raster:spatial_resolution": detail.get("resolution")},
        )
    return assets


def visual_assets(state: stac.BuildState) -> dict[str, stac.MosaicAsset]:
    """Build the mosaic item's web-map assets from what the ``pmtiles`` step recorded.

    One asset per archive, with the ``visual`` role: the archives hold lossy
    8-bit values for display, and analysis reads the COGs. Title,
    description and band come from ``metadata/assets.yaml``; zoom range, size
    and checksum come from the build state.
    """
    written = state.details.get("pmtiles", {})
    assets: dict[str, stac.MosaicAsset] = {}
    for key, detail in sorted(written.items()):
        if mismatch := webmap.encoding_mismatch(key, detail):
            msg = f"{mismatch}; rebuild the year with --force-from pmtiles"
            raise SystemExit(msg)
        entry = vocabulary().viz_asset(
            key,
            min_zoom=int(detail["min_zoom"]),
            max_zoom=int(detail["max_zoom"]),
        )
        assets[key] = stac.MosaicAsset(
            href=detail["url"],
            title=entry["title"],
            description=entry["description"],
            roles=list(entry["roles"]),
            media_type=entry["type"],
            size=detail.get("size_bytes"),
            checksum=detail.get("checksum"),
            extra={"proj:code": "EPSG:3857", "bands": entry["bands"]},
        )
    return assets


def positive_int(text: str) -> int:
    """Parse a command-line count that must be at least one."""
    value = int(text)
    if value < 1:
        msg = f"must be at least 1, not {value}"
        raise argparse.ArgumentTypeError(msg)
    return value


def parse_args() -> argparse.Namespace:
    """Command-line arguments of the year build."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("year", type=int)
    parser.add_argument(
        "--steps",
        default=",".join(STEPS),
        help=f"comma-separated subset of {','.join(STEPS)}",
    )
    parser.add_argument(
        "--force-from",
        default=None,
        choices=STEPS,
        help="rerun this step and every step after it, ignoring their fingerprints",
    )
    parser.add_argument(
        "--layers",
        default=None,
        help="comma-separated asset keys to restrict the mosaics and tiles to",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=16,
        help="threads for the copy; network-bound, so more than the core count is fine",
    )
    parser.add_argument(
        "--mosaic-workers",
        type=positive_int,
        default=mosaics.MOSAIC_WORKERS,
        help=(
            "mosaic layers written at the same time, one process each; each peaks at "
            "about 2.7 GB of memory for alps"
        ),
    )
    parser.add_argument(
        "--tiler-workers",
        type=int,
        default=None,
        help=(
            "processes for the web-map tiler (default: one per core); each uses "
            "well under 1 GB of memory"
        ),
    )
    parser.add_argument(
        "--concurrent-layers",
        type=int,
        default=webmap.DEFAULT_CONCURRENT_LAYERS,
        help=(
            "web-map layers warped at the same time. Each needs a scratch raster of "
            "the whole scope (about 100 MB for ch, 1.1 GB for alps); lower this "
            "first if the build runs out of memory"
        ),
    )
    return parser.parse_args()


def main() -> None:
    """Build one year into the store, resuming from whatever the build state already recorded."""
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    scope = vocabulary().catalog.scope
    args.cache = settings.cache_dir
    args.layers = args.layers.split(",") if args.layers else None
    steps = stac.resolve_steps(args.steps.split(","))

    src = settings.source(args.year)
    layout = stac.StoreLayout(settings.store_root)
    logger.info("Building %s %d from %s into %s", scope.key, args.year, src, layout.root)

    frame, files = load_and_verify_manifest(src, args.year)
    # Fingerprint the archive as verified. An empty composite adds no pixel to
    # the mosaics or the web maps, so leaving it out reruns only the items.
    copy_print = stac.fingerprint(stac.manifest_digest(frame))
    frame, files = without_empty_composites(frame, files)

    state = stac.BuildState.load(layout.build_state(args.year))
    if args.force_from is not None:
        state.invalidate_from(args.force_from)

    prints = {
        "copy": copy_print,
        "vrt": copy_print,
        "mosaics": stac.fingerprint(
            copy_print,
            scope.analysis_grid,
            mosaics.MAX_Z_ERROR,
            mosaics.COG_CREATION,
            args.layers,
        ),
        # The encoding ranges, tile format, quality and tiler version decide the
        # archive bytes, so changing any of them rebuilds the archives. Colour
        # ramps belong to the styles.
        "pmtiles": stac.fingerprint(
            copy_print,
            scope.display_grid,
            [repr(vocabulary().display(key).encoding) for key in ASSET_KEYS],
            webmap.TILE_FORMAT,
            webmap.WEBP_QUALITY,
            args.layers,
            tiler.WRITER,
        ),
    }
    prints["items"] = stac.fingerprint(prints, CATALOGUE_SCHEMA_VERSION, stac.DATASET_VERSION)

    pending = [step for step in steps if not state.is_current(step, prints[step])]
    logger.info("Scratch: %s", args.cache)
    require_scratch_space(args.cache, scratch_needed(args, scope, pending))

    analysis_chain: mosaics.YearVrts | None = None
    display_chain: mosaics.YearVrts | None = None
    for step in steps:
        if state.is_current(step, prints[step]):
            logger.info("%s: up to date, skipping", step)
            continue
        logger.info("--- %s ---", step)
        if step == "copy":
            step_copy(args, layout, files, src)
            state.record(step, prints[step])
        elif step == "vrt":
            analysis_chain = step_vrt(args, scope, layout, files)
        elif step == "mosaics":
            analysis_chain = analysis_chain or step_vrt(args, scope, layout, files)
            state.record(
                step, prints[step], step_mosaics(args, scope, layout, analysis_chain, frame)
            )
        elif step == "pmtiles":
            display_chain = display_chain or step_vrt(
                args,
                scope,
                layout,
                files,
                grid=scope.display_grid,
                name="vrt-display",
            )
            state.record(step, prints[step], step_pmtiles(args, layout, display_chain, files))
        elif step == "items":
            step_items(args, scope, layout, frame, files, state, prints["items"])
            state.record(step, prints[step])

    logger.info("Done: %s", ", ".join(steps))


if __name__ == "__main__":
    main()
