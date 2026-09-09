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
    uv run --env-file .env scripts/publish-year.py 2024 \\
        --src  s3://s1-bursts/data/alps-total/2024/s1-comp-ch \\
        --scope ch \\
        --steps copy,vrt,mosaics,pmtiles,items
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import logging
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any

from freezebase.s3 import list_object_sizes
import pandas as pd

from glace_catalog import mosaics, stac, tiler, webmap
from glace_catalog.config import settings
from glace_catalog.scopes import get_scope
from glace_catalog.stac import PORTOLAN_SCHEMA
from glace_catalog.stac.save import write_year_items
from glace_catalog.stac.store import MANIFEST_NAME, STEPS, YEAR_INDEX_NAME

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    import pystac

    from glace_catalog.scopes import Grid, Scope

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


def step_copy(
    args: argparse.Namespace,
    layout: stac.StoreLayout,
    files: Sequence[stac.CompositeFile],
    src: Any,
) -> None:
    """Copy every composite into the store, byte for byte.

    The run's manifest and configuration stay beside the source archive: the
    manifest is the publisher's cache, and every value a reader needs from either
    is on the items — tags, ``file:size``, ``file:checksum`` — or in the pipeline
    repository's committed configuration.
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
    *,
    grid: Grid | None = None,
    name: str = "vrt",
) -> mosaics.YearVrts:
    """Build one of the year's VRT chains in the local cache.

    There are two, and they do not share a step: the mosaics are warped onto the
    scope's equal-area analysis grid, the web-map archives onto its
    WebMercatorQuad display grid, and **both start from the UTM composites**
    (M-24, M-25). Warping one out of the other would resample twice and put a
    non-equal-area grid back in the analysis path.

    Always rebuilt: the VRTs are local and ephemeral, so a remote "done" can be
    true while the cache is empty, and they take seconds.
    """
    vrt_dir = args.cache / "glace-store" / str(args.year) / name
    chain = mosaics.build_year_vrts(
        published_files(files, layout, args.year),
        scope,
        vrt_dir,
        grid=grid,
    )
    logger.info(
        "vrt: %d linear, %d decibel, %d false-colour",
        len(chain.linear),
        len(chain.decibel),
        len(chain.rgb),
    )
    return chain


def composite_window(frame: pd.DataFrame) -> tuple[date, date]:
    """Read the year's compositing window, first and last day inclusive, from the manifest.

    Every row carries the same pair; a second pair is a stale file and refuses
    the year, see :func:`glace_catalog.stac.composite_window`.
    """
    return stac.composite_window(
        zip(frame["composite_start"], frame["composite_end"], strict=True)
    )


def mosaic_tags(frame: pd.DataFrame, asset_key: str) -> dict[str, str]:
    """Provenance tags a mosaic carries over from the tiles it was warped from.

    Only the tags every contributing tile agrees on: a mosaic should say what it
    is made of, and a value that differed between tiles is not something the
    mosaic can assert.
    """
    rows = frame.loc[frame["asset_key"] == asset_key]
    tags: dict[str, str] = {}
    # PROVENANCE_TAGS is the union over every product: a pass composite's
    # PASS_DIRECTION is not a column of an LRW manifest, and a coherence tag is
    # not one of a backscatter-only run. Absent is fine; disagreement is not.
    for column in stac.PROVENANCE_TAGS:
        if column not in rows:
            continue
        values = {str(value) for value in rows[column].dropna().unique()}
        if len(values) == 1:
            tags[column] = values.pop()
    start, end = composite_window(rows)
    tags["COMPOSITE_START_DATE"] = start.isoformat()
    tags["COMPOSITE_END_DATE"] = end.isoformat()
    # The span of contributing acquisitions differs per tile; the mosaic's is
    # their union, as the item builder derives it for a tile's two polarizations.
    for tag, pick in (("FIRST_ACQUISITION_DATE", min), ("LAST_ACQUISITION_DATE", max)):
        if tag in rows and rows[tag].notna().any():
            tags[tag] = str(pick(rows[tag].dropna()))
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
) -> dict[str, mosaics.MosaicWrite]:
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
    stac.store.upload_tree(staged, layout.mosaic_year(args.year))
    return written


def clear_stale_archives(staged: Path) -> int:
    """Delete the archives a previous run left in the staging directory.

    Everything under ``staged`` is uploaded into the year's item directory, so
    an archive from an earlier build -- under a name the tiler no longer writes
    -- would be published beside the new ones as if it were current. The
    2026-09-09 migration did exactly that: the old ``{stem}.pmtiles`` rode
    along with the new ``{stem}_viz.pmtiles`` and had to be deleted by hand.
    """
    stale = sorted(path for path in staged.iterdir() if path.suffix == ".pmtiles")
    for path in stale:
        path.unlink()
    if stale:
        logger.info("Removed %d stale archive(s) from %s", len(stale), staged)
    return len(stale)


def step_pmtiles(
    args: argparse.Namespace,
    layout: stac.StoreLayout,
    chain: mosaics.YearVrts,
    window: tuple[date, date],
) -> list[webmap.LayerInfo]:
    """Render and upload the year's web-map archives."""
    staged = args.cache / "glace-store" / str(args.year) / "pmtiles"
    staged.mkdir(parents=True, exist_ok=True)
    clear_stale_archives(staged)
    layers = webmap.build_layers(
        chain,
        staged,
        style_path=args.style,
        year=args.year,
        start_date=window[0].isoformat(),
        end_date=window[1].isoformat(),
        layers=args.layers,
        workers=args.tiler_workers,
        concurrent_layers=args.concurrent_layers,
        # Beside the staged archives, not inside them: everything under
        # `staged` is uploaded, and a warp interrupted half-way must not be.
        scratch=args.cache / "glace-store" / str(args.year) / "rgba",
    )
    # Into the year's item directory, beside the COGs: the archives are assets of
    # that item, and `upload_tree` merges rather than replaces.
    stac.store.upload_tree(staged, layout.mosaic_year(args.year))
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
            f"{path} is absent. Run deep-glacier-mapping's "
            "scripts/inventories/derive-glacier-fractions.py for this scope first; "
            "the fractions are pinned per scope so they cannot drift between years."
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
    grid = scope.analysis_grid
    mosaic_item.properties["description"] = (
        f"Annual mosaics of the GLACE composites for {args.year}, on the fixed "
        f"equal-area grid ({grid.crs}, {grid.resolution:g} m) every year of this "
        "collection shares."
    )

    with tempfile.TemporaryDirectory() as tmp:
        staged_tiles = Path(tmp) / "tiles"
        staged_mosaics = Path(tmp) / "mosaics"
        write_year_items(
            tile_items,
            mosaic_item,
            staged_tiles,
            staged_mosaics,
            tiles_collection_id=scope.tiles_collection_id,
            mosaics_collection_id=scope.mosaics_collection_id,
            scope_title=scope.catalog_title,
            portolan_schema=PORTOLAN_SCHEMA,
            year=args.year,
        )
        # Root-relative hrefs, so the year's slice survives a bucket move
        # untouched; finalize-catalog rebases them onto the published URL.
        stac.export_geoparquet(
            [_rooted(item, args.year) for item in tile_items],
            staged_tiles / YEAR_INDEX_NAME,
        )
        stac.store.upload_tree(staged_tiles, layout.tile_year(args.year))
        stac.store.upload_tree(staged_mosaics, layout.mosaic_year(args.year))
    logger.info("items: %d tile items and one mosaic item", len(tile_items))


def _rooted(item: pystac.Item, year: int) -> pystac.Item:
    """Copy of a tile item whose asset hrefs are relative to the store root."""
    rooted = item.clone()
    tile = item.properties["glace:mgrs_tile"]
    for asset in rooted.assets.values():
        asset.href = f"tiles/{year}/{tile}/{asset.href}"
    return rooted


def mosaic_assets(
    state: stac.BuildState,
    frame: pd.DataFrame,
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
        # The five statistics the writer embedded in the COG, republished as the
        # raster extension's `bands[].statistics` rather than measured again.
        statistics = detail.get("statistics")
        if statistics:
            band["statistics"] = statistics
        assets[asset_key] = stac.MosaicAsset(
            href=f"{asset_key}.tif",
            title=f"{asset_key} annual mosaic",
            roles=["quality"] if "_qa_" in asset_key else ["data", "cloud-optimized"],
            size=detail.get("size"),
            checksum=detail.get("checksum"),
            band=band,
            extra={"raster:spatial_resolution": detail.get("resolution")},
        )
    return assets


def _read_instead(stem: str) -> str:
    """Which COG to read for the values a rendering only pictures.

    A false-colour archive has no COG of its own: it composes the two
    polarizations, so it names both.
    """
    if stem.endswith("_rgb"):
        base = stem.removesuffix("_rgb")
        return f"read {base}_vv.tif and {base}_vh.tif"
    return f"read {stem}.tif"


def visual_assets(state: stac.BuildState) -> dict[str, stac.MosaicAsset]:
    """Assemble the year item's renderings from what the ``pmtiles`` step recorded.

    One asset per archive, with the ``visual`` role and never ``data``: the pixels
    are an 8-bit stretch of the mosaic, backscatter in decibels, and an analysis
    pipeline that read one would be reading a picture. The archive's own header
    carries bounds and zoom range; the asset carries the grid, the digest, and a
    sentence saying which COG to read instead.
    """
    written = state.details.get("pmtiles", {})
    assets: dict[str, stac.MosaicAsset] = {}
    for key, detail in sorted(written.items()):
        stem = stac.data_key(key)
        stretch = f"{detail['vmin']:g}-{detail['vmax']:g}"
        ramp = f" through {detail['cmap']}" if detail.get("cmap") else ""
        units = f" {detail['units']}" if detail.get("units") else ""
        assets[key] = stac.MosaicAsset(
            href=detail["url"],
            title=f"{stem} web-map tiles",
            description=(
                f"Pre-styled 8-bit WebP rendering of {stem} on WebMercatorQuad "
                f"z{detail['min_zoom']}-z{detail['max_zoom']}, stretched {stretch}{units}{ramp} "
                f"as the collection's style for {detail['year']} declares. "
                f"Not for analysis: {_read_instead(stem)}."
            ),
            roles=["visual"],
            media_type="application/vnd.pmtiles",
            size=detail.get("size_bytes"),
            checksum=detail.get("checksum"),
            extra={"proj:code": "EPSG:3857"},
        )
    return assets


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
    parser.add_argument(
        "--concurrent-layers",
        type=int,
        default=webmap.DEFAULT_CONCURRENT_LAYERS,
        help=(
            "web-map layers warped at once before tiling. Each holds one RGBA raster of "
            "the whole scope in the cache while it runs (about 100 MB for ch, 1.1 GB for "
            "the Alps), so this is the lever to pull on a disk-constrained machine; "
            "raise it towards 14 where there is room"
        ),
    )
    parser.add_argument("--cache", type=Path, default=None, help="local scratch directory")
    parser.add_argument(
        "--style",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "catalog/mosaics/styles/default.json",
        help="MapLibre style holding the published stretches and ramps (M-27)",
    )
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

    copy_print = stac.fingerprint(stac.manifest_digest(frame))
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
        # The committed style is the source of the stretches and ramps (M-27),
        # so its bytes are what a rebuild has to notice: edit a range and every
        # archive that baked the old one is stale.
        # The tiler's own identity is an input too: its output changed when it
        # moved in-tree (R-12), and archives of one year must not silently keep
        # an older tiler's pixels beside another year's.
        "pmtiles": stac.fingerprint(
            copy_print,
            scope.display_grid,
            args.style.read_bytes().decode() if args.style.exists() else None,
            args.layers,
            tiler.WRITER,
        ),
    }
    prints["items"] = stac.fingerprint(prints, CATALOGUE_SCHEMA_VERSION)

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
            written = step_mosaics(args, layout, analysis_chain, frame)
            state.record(
                step,
                prints[step],
                {
                    key: {
                        "size": write.path.stat().st_size,
                        "resolution": scope.analysis_grid.resolution,
                        "checksum": write.checksum,
                        "statistics": write.statistics.to_dict(),
                    }
                    for key, write in written.items()
                },
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
            built = step_pmtiles(args, layout, display_chain, composite_window(frame))
            state.record(
                step,
                prints[step],
                {
                    layer.url.removesuffix(".pmtiles"): {
                        "url": layer.url,
                        "size_bytes": layer.size_bytes,
                        "checksum": layer.checksum,
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
