r"""Publish one year of GLACE composites into a store: the whole per-year pipeline.

Five steps, each taking its inputs as explicit paths from the one before, in
process — no globbing, no filename parsing, no ordering hazard:

``copy``
    Verify the run manifest against a listing of the source, refuse anything the
    catalogue cannot describe, and copy the composites in **server-side**, so no
    byte is downloaded and the compression the run chose survives exactly.
``vrt``
    Build the VRT chain: per-zone 10 m mosaic, warp onto the canonical grid,
    mosaic the zones onto the scope's fixed extent, then the decibel and
    false-colour readings.
``mosaics``
    Write twelve LERC_ZSTD mosaic COGs from the chain's linear step.
``pmtiles``
    Render fourteen web-map archives from the chain.
``items``
    Write the year's tile items, its mosaic item and its geoparquet index.

Each step records an **input fingerprint** rather than a "done" flag, so a rerun
after the grid, the bounds or the source moved rebuilds instead of leaving a
stale product in place. ``--force-from`` overrides.

The collections and the root catalog are not written here: they are the one thing
that spans years, so ``finalize-catalog.py`` owns them. That is what makes adding
a year later leave every existing year untouched.

Usage:
    uv run --env-file .env python scripts/stac/publish-year.py 2024 \\
        --src  s3://s1-bursts/data/alps-total/2024/s1-comp-ch \\
        --scope ch \\
        --steps copy,vrt,mosaics,pmtiles,items
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime
import logging
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any

import pandas as pd

from glace_catalog import mosaics, stac, webmap
from glace_catalog.config import settings
from glace_catalog.scopes import get_scope
from glace_catalog.stac.save import write_year_items
from glace_catalog.stac.store import CONFIG_NAME, MANIFEST_NAME, STEPS

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pystac

    from glace_catalog.scopes import Scope

logger = logging.getLogger(Path(__file__).stem)

CATALOGUE_SCHEMA_VERSION = 1
"""Bumped when the documents change shape, so ``items`` reruns against a new one."""


def load_and_verify_manifest(src: Any, year: int) -> tuple[pd.DataFrame, list[stac.CompositeFile]]:
    """Read the run manifest, check it against the archive, and refuse a bad one.

    This is where the preconditions surface, before a byte moves: the manifest
    must match a listing of what is actually there, no two files may resolve to
    one published name, and every raster must be linear and fully tagged.

    Raises
    ------
    SystemExit
        If the manifest is absent, disagrees with the archive, or describes
        composites the catalogue cannot publish.
    """
    manifest_path = src / MANIFEST_NAME
    if not manifest_path.exists():
        msg = (
            f"{manifest_path} is absent. The composite run emits it; for an archive "
            "that predates that, run scripts/stac/build-manifest.py first."
        )
        raise SystemExit(msg)

    frame = stac.read_manifest(manifest_path)
    listing = stac.list_objects(src)
    mismatches = stac.verify_manifest(frame, listing)
    if mismatches:
        for mismatch in mismatches[:50]:
            logger.error("%s", mismatch)
        msg = f"manifest and archive disagree about {len(mismatches)} file(s) in {src}"
        raise SystemExit(msg)

    files = stac.manifest_composites(frame, src)
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

    years = {file.start.year for file in files}
    if years != {year}:
        msg = f"{src} holds composites from {sorted(years)}, not {year}"
        raise SystemExit(msg)

    logger.info("Manifest verified: %d files across %d tiles", len(files), len(by_tile))
    return frame, files


def step_copy(
    args: argparse.Namespace,
    layout: stac.StoreLayout,
    files: Sequence[stac.CompositeFile],
    src: Any,
) -> None:
    """Copy the manifest, the run configuration and every composite into the store.

    The manifest and the configuration are the reproducibility record, and the
    staging tree that holds them is meant to be deleted once the copy is
    verified — so copying them out is not housekeeping, it is the step that stops
    the cleanup destroying the record.
    """
    year_dir = layout.year(args.year)
    year_dir.mkdir(parents=True, exist_ok=True)
    (year_dir / MANIFEST_NAME).write_bytes((src / MANIFEST_NAME).read_bytes())
    config = src / CONFIG_NAME
    if config.exists():
        (year_dir / CONFIG_NAME).write_bytes(config.read_bytes())
    else:
        logger.warning(
            "No %s beside the composites; the run's configuration is not archived",
            CONFIG_NAME,
        )

    listing = stac.list_objects(src)
    result = stac.copy_composites(files, layout, args.year, listing, max_workers=args.max_workers)
    logger.info("copy: %s", result)


def published_files(
    files: Sequence[stac.CompositeFile],
    layout: stac.StoreLayout,
    year: int,
) -> list[stac.CompositeFile]:
    """Readdress the composites at their published paths in the store.

    Everything downstream of ``copy`` reads the store rather than the staging
    tree, because the staging year is meant to be deleted once the copy is
    verified — and a mosaic that could only be rebuilt from a deleted directory
    would not be reproducible at all.
    """
    return [
        replace(file, path=layout.tile(year, file.tile) / file.published_name) for file in files
    ]


def step_vrt(
    args: argparse.Namespace,
    scope: Scope,
    layout: stac.StoreLayout,
    files: Sequence[stac.CompositeFile],
) -> mosaics.YearVrts:
    """Build the year's VRT chain in the local cache.

    Always rebuilt: the VRTs are local and ephemeral, so a remote "done" can be
    true while the cache is empty, and they take seconds.
    """
    vrt_dir = args.cache / "glace-store" / str(args.year) / "vrt"
    chain = mosaics.build_year_vrts(published_files(files, layout, args.year), scope, vrt_dir)
    logger.info(
        "vrt: %d linear, %d decibel, %d false-colour",
        len(chain.linear),
        len(chain.decibel),
        len(chain.rgb),
    )
    return chain


def composite_window(frame: pd.DataFrame) -> tuple[datetime, datetime]:
    """Composite window a year's tiles span, as an inclusive-exclusive range."""
    start = min(datetime.fromisoformat(value) for value in frame["composite_start"])
    end = max(datetime.fromisoformat(value) for value in frame["composite_end"])
    return start.replace(tzinfo=UTC), end.replace(tzinfo=UTC)


def mosaic_tags(frame: pd.DataFrame, asset_key: str) -> dict[str, str]:
    """Provenance tags a mosaic carries over from the tiles it was warped from.

    Only the tags every contributing tile agrees on: a mosaic should say what it
    is made of, and a value that differed between tiles is not something the
    mosaic can assert.
    """
    rows = frame.loc[frame["asset_key"] == asset_key]
    tags: dict[str, str] = {}
    for column in stac.PROVENANCE_TAGS:
        values = {str(value) for value in rows[column].dropna().unique()}
        if len(values) == 1:
            tags[column] = values.pop()
    tags["COMPOSITE_START_DATE"] = str(rows["composite_start"].min())
    tags["COMPOSITE_END_DATE"] = str(rows["composite_end"].max())
    tags["MGRS_TILE"] = "N/A -- mosaic of many tiles"
    if asset_key.endswith("_qa_num"):
        # The mosaic is a mean of counts, not a count: say so, because it is no
        # longer the same quantity as its source.
        tags["LAYER_DESCRIPTION"] = (
            "Mean number of contributing observations per output pixel, averaged "
            "from the 10 m per-tile counts."
        )
    return tags


def step_mosaics(
    args: argparse.Namespace,
    layout: stac.StoreLayout,
    chain: mosaics.YearVrts,
    frame: pd.DataFrame,
) -> dict[str, Path]:
    """Write and upload the year's twelve mosaic COGs."""
    band_names = {
        key: mosaics.mosaic_band_name(
            key,
            str(frame.loc[frame["asset_key"] == key, "band_name"].iloc[0]),
        )
        for key in chain.linear
    }
    tags = {key: mosaic_tags(frame, key) for key in chain.linear}
    staged = args.cache / "glace-store" / str(args.year) / "mosaics"
    staged.mkdir(parents=True, exist_ok=True)
    written = mosaics.build_mosaic_cogs(
        chain,
        staged,
        band_names=band_names,
        tags=tags,
        layers=args.layers,
    )
    stac.store.upload_tree(staged, layout.mosaics(args.year))
    return written


def tiling_chain(
    args: argparse.Namespace,
    layout: stac.StoreLayout,
    chain: mosaics.YearVrts,
) -> mosaics.YearVrts:
    """Choose the chain the web-map tiler reads: the written mosaics where they exist.

    A mosaic COG is the canonical-grid step already materialised, with its own
    overviews, so tiling from it opens one file per worker instead of the whole
    tile chain — which is where the tiler's memory goes, and the reason a
    false-colour layer (two chains at once) is the first thing to fall over.

    Falls back to the tile chain when the mosaics are absent, so ``--steps
    pmtiles`` still works on its own.
    """
    published = {key: layout.mosaics(args.year) / f"{key}.tif" for key in chain.linear}
    if not all(path.exists() for path in published.values()):
        logger.warning(
            "Not every mosaic is published for %d; tiling from the tile chain instead, "
            "which needs far more memory. Run the mosaics step first.",
            args.year,
        )
        return chain
    logger.info("Tiling from the %d published mosaics", len(published))
    return mosaics.build_cog_chain(
        published,
        args.cache / "glace-store" / str(args.year) / "vrt-cog",
    )


def step_pmtiles(
    args: argparse.Namespace,
    layout: stac.StoreLayout,
    chain: mosaics.YearVrts,
    window: tuple[datetime, datetime],
) -> list[webmap.LayerInfo]:
    """Render and upload the year's web-map archives."""
    staged = args.cache / "glace-store" / str(args.year) / "pmtiles"
    staged.mkdir(parents=True, exist_ok=True)
    layers = webmap.build_layers(
        tiling_chain(args, layout, chain),
        staged,
        year=args.year,
        start_date=window[0].date().isoformat(),
        end_date=window[1].date().isoformat(),
        layers=args.layers,
        workers=args.tiler_workers,
    )
    stac.store.upload_tree(staged, layout.pmtiles(args.year))
    return layers


def load_glacier_fractions(layout: stac.StoreLayout) -> dict[str, tuple[float, float]]:
    """Read the scope's precomputed glacier fractions.

    Raises
    ------
    SystemExit
        If the table is absent. A silently absent fraction is worse than a
        stopped build: it would publish a tile the sampler cannot weight.
    """
    path = layout.glacier_fractions
    if not path.exists():
        msg = (
            f"{path} is absent. Run scripts/stac/derive-glacier-fractions.py for this "
            "scope first; the fractions are pinned per scope so they cannot drift "
            "between years."
        )
        raise SystemExit(msg)
    with path.open("rb") as handle:
        frame = pd.read_parquet(handle)
    return {
        str(row["mgrs_code"]): (
            float(row["glacier_fraction"]),
            float(row["glacier_fraction_buffered"]),
        )
        for row in frame.to_dict("records")
    }


def step_items(
    args: argparse.Namespace,
    scope: Scope,
    layout: stac.StoreLayout,
    frame: pd.DataFrame,
    files: Sequence[stac.CompositeFile],
    state: stac.BuildState,
    build_fingerprint: str,
) -> None:
    """Write the year's tile items, its mosaic item and its geoparquet index.

    The mosaic item is written **last and from the build state**, which is how it
    comes to list exactly the products that were actually written. Reading it back
    is how consumers find the layers; never how the publisher does.
    """
    raster_info = stac.manifest_raster_info(frame)
    percents = stac.manifest_valid_percents(frame)
    fractions = load_glacier_fractions(layout)

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
            sorted(tile_files, key=lambda f: f.asset_key),
            raster_info[tile],
            glacier_fraction=fractions[tile][0],
            glacier_fraction_buffered=fractions[tile][1],
            valid_percents=percents.get(tile),
        )
        for tile, tile_files in sorted(by_tile.items())
    ]

    window = composite_window(frame)
    geometry, bbox = mosaics.data_footprint(files)
    mosaic_item = stac.build_mosaic_item(
        scope,
        args.year,
        geometry=geometry,
        bbox=bbox,
        start=window[0],
        end=window[1],
        assets=mosaic_assets(state, frame, has_config=layout.config(args.year).exists()),
        pmtiles=pmtiles_links(state),
        build_fingerprint=build_fingerprint,
    )
    mosaic_item.properties["description"] = (
        f"Annual mosaics of the GLACE {scope.title} composites for {args.year}, plus the "
        "run manifest and configuration. Those two documents cover the year's tiles as "
        "much as its mosaics; they hang here because this is the year's only per-year item."
    )

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp)
        write_year_items(
            tile_items,
            mosaic_item,
            staged,
            tiles_collection_id=scope.tiles_collection_id,
            mosaics_collection_id=scope.mosaics_collection_id,
        )
        # Root-relative hrefs, so the per-year index survives a bucket move
        # untouched; finalize-catalog rebases them onto the published URL.
        stac.export_geoparquet(
            [_rooted(item, args.year) for item in tile_items],
            staged / "items.parquet",
        )
        stac.store.upload_tree(staged, layout.year(args.year))
    logger.info("items: %d tile items and one mosaic item", len(tile_items))


def _rooted(item: pystac.Item, year: int) -> pystac.Item:
    """Copy of a tile item whose asset hrefs are relative to the store root."""
    rooted = item.clone()
    tile = item.properties["glace:mgrs_tile"]
    for asset in rooted.assets.values():
        asset.href = f"{year}/{tile}/{asset.href}"
    return rooted


def mosaic_assets(
    state: stac.BuildState,
    frame: pd.DataFrame,
    *,
    has_config: bool,
) -> dict[str, stac.MosaicAsset]:
    """Assemble the mosaic item's assets from what the ``mosaics`` step recorded writing."""
    written = state.details.get("mosaics", {})
    assets: dict[str, stac.MosaicAsset] = {}
    for asset_key, detail in sorted(written.items()):
        is_num = asset_key.endswith("_qa_num")
        band: dict[str, Any] = {
            "name": asset_key,
            "data_type": "float32",
            "nodata": "nan",
        }
        unit = frame.loc[frame["asset_key"] == asset_key, "band_unit"]
        if not is_num and len(unit) and str(unit.iloc[0]):
            band["unit"] = str(unit.iloc[0])
        assets[asset_key] = stac.MosaicAsset(
            href=f"{asset_key}.tif",
            title=f"{asset_key} annual mosaic",
            roles=["metadata", "quality"] if "_qa_" in asset_key else ["data", "overview"],
            size=detail.get("size"),
            band=band,
            extra={"raster:spatial_resolution": detail.get("resolution")},
        )
    assets["manifest"] = stac.MosaicAsset(
        href=f"../{MANIFEST_NAME}",
        title="Composite run manifest",
        roles=["metadata"],
        media_type="application/vnd.apache.parquet",
    )
    if has_config:
        assets["config"] = stac.MosaicAsset(
            href=f"../{CONFIG_NAME}",
            title="Composite run configuration",
            roles=["metadata"],
            media_type="application/yaml",
        )
    return assets


def pmtiles_links(state: stac.BuildState) -> dict[str, str]:
    """Assemble the mosaic item's PMTiles links from what the ``pmtiles`` step recorded."""
    return {
        layer_id: f"../pmtiles/{detail['url']}"
        for layer_id, detail in sorted(state.details.get("pmtiles", {}).items())
    }


def parse_args() -> argparse.Namespace:
    """Command-line arguments of the per-year publisher."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("year", type=int)
    parser.add_argument("--src", required=True, help="local path or s3:// URI of one year")
    parser.add_argument("--scope", required=True, choices=["ch", "alps"])
    parser.add_argument("--root", default=None, help="store root; defaults to the scope's")
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
        help="threads for the S3 copy; I/O-bound, so more than cores is fine",
    )
    parser.add_argument(
        "--tiler-workers",
        type=int,
        default=None,
        help=(
            "worker processes for the web-map tiler; defaults to one per core. "
            "Each decodes raster blocks, so this is the lever to pull on a memory-"
            "constrained machine -- unlike --max-workers, it is not a thread count"
        ),
    )
    parser.add_argument("--cache", type=Path, default=None, help="local scratch directory")
    return parser.parse_args()


def main() -> None:
    """Publish one year into the store, resuming from whatever the build state already recorded."""
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    scope = get_scope(args.scope)
    args.cache = args.cache or settings.cache_dir
    args.layers = args.layers.split(",") if args.layers else None
    steps = stac.resolve_steps(args.steps.split(","))

    src = settings.resolve_path(args.src)
    layout = stac.StoreLayout(settings.resolve_path(args.root or scope.root))
    logger.info("Publishing %s %d from %s to %s", scope.key, args.year, src, layout.root)

    frame, files = load_and_verify_manifest(src, args.year)

    state = stac.BuildState.load(layout.build_state(args.year))
    if args.force_from is not None:
        state.invalidate_from(args.force_from)

    # The manifest describes the composites, but `copy` also archives the run's
    # configuration, so a config that appeared or changed has to reach the
    # fingerprint too — otherwise the step stays "done" with the record missing.
    config = src / CONFIG_NAME
    copy_print = stac.fingerprint(
        stac.manifest_digest(frame),
        config.stat().st_size if config.exists() else None,
    )
    prints = {
        "copy": copy_print,
        "vrt": copy_print,
        "mosaics": stac.fingerprint(
            copy_print,
            scope.grid,
            mosaics.MAX_Z_ERROR,
            mosaics.COG_CREATION,
            args.layers,
        ),
        "pmtiles": stac.fingerprint(
            copy_print,
            scope.grid,
            webmap.DEFAULT_STYLES,
            webmap.RGB_STYLES,
            args.layers,
        ),
    }
    prints["items"] = stac.fingerprint(prints, CATALOGUE_SCHEMA_VERSION)

    chain: mosaics.YearVrts | None = None
    for step in steps:
        if state.is_current(step, prints[step]):
            logger.info("%s: up to date, skipping", step)
            continue
        logger.info("--- %s ---", step)
        if step == "copy":
            step_copy(args, layout, files, src)
            state.record(step, prints[step])
        elif step == "vrt":
            chain = step_vrt(args, scope, layout, files)
        elif step == "mosaics":
            chain = chain or step_vrt(args, scope, layout, files)
            written = step_mosaics(args, layout, chain, frame)
            state.record(
                step,
                prints[step],
                {
                    key: {"size": path.stat().st_size, "resolution": scope.grid.resolution}
                    for key, path in written.items()
                },
            )
        elif step == "pmtiles":
            chain = chain or step_vrt(args, scope, layout, files)
            built = step_pmtiles(args, layout, chain, composite_window(frame))
            state.record(
                step,
                prints[step],
                {
                    layer.url.removesuffix(".pmtiles"): {
                        "url": layer.url,
                        "size_bytes": layer.size_bytes,
                        "bounds": layer.bounds,
                        "min_zoom": layer.min_zoom,
                        "max_zoom": layer.max_zoom,
                        "cmap": layer.cmap,
                        "vmin": layer.vmin,
                        "vmax": layer.vmax,
                        "units": layer.units,
                        "product": layer.product,
                        "polarization": layer.polarization,
                        "year": layer.year,
                        "id": layer.id,
                        "start_date": layer.start_date,
                        "end_date": layer.end_date,
                    }
                    for layer in built
                },
            )
        elif step == "items":
            step_items(args, scope, layout, frame, files, state, prints["items"])
            state.record(step, prints[step])

    logger.info("Done: %s", ", ".join(steps))


if __name__ == "__main__":
    main()
