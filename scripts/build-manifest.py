r"""Write a run manifest for a composite archive that predates manifest emission.

The composite run in ``deep-glacier-mapping`` emits ``manifest.parquet`` beside
its output — one row per written file, carrying the provenance tags, the size,
the multihash sha256 the writer computed, and the QA-derived valid-data
percentages. The publisher then reads one file instead of opening every
composite in the archive.

The completed Swiss 2021-2024 runs predate that, and re-running four correct
years to produce a sidecar would be absurd. This reconstructs the manifest from
the archive itself: filenames and asset keys by discovery, the provenance tags by
reading each raster's header, the valid percentages by reading the QA-NUM
rasters, and sizes from one listing. It cannot recover a digest, so every
``sha256`` it writes is ``None`` and those assets publish no ``file:checksum``.

That costs one header read per file and one full read per QA-NUM raster — seconds
to minutes on a Swiss year, and **not a viable path for the Alps**, which is the
whole reason the manifest moves upstream. Keep this afterwards as the fallback
for any archive whose manifest is lost.

Usage:
    uv run --env-file .env python scripts/build-manifest.py \\
        --src s3://s1-bursts/data/alps-total/2024/s1-comp-ch
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path

from glace_catalog import stac
from glace_catalog.config import settings
from glace_catalog.stac.store import MANIFEST_NAME

logger = logging.getLogger(Path(__file__).stem)


def valid_percents(
    by_tile: dict[str, list[stac.CompositeFile]],
    *,
    max_workers: int,
) -> dict[str, dict[str, float]]:
    """Valid-data percentage per tile and data-asset key, from the QA-NUM rasters.

    The percentage belongs to the measurement, not to its QA layer, so it is
    keyed by the *data* asset — ``coh12_vv``, not ``coh12_vv_qa_num``.
    """
    jobs = [
        (tile, file.asset_key.removesuffix("_qa_num"), file.path)
        for tile, files in sorted(by_tile.items())
        for file in files
        if file.qa == "NUM"
    ]
    percents: dict[str, dict[str, float]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = pool.map(lambda job: stac.valid_percent_from_qa(job[2]), jobs)
        for (tile, key, _), percent in zip(jobs, results, strict=True):
            percents.setdefault(tile, {})[key] = round(percent, 2)
    logger.info("Computed %d valid-data percentages", len(jobs))
    return percents


def parse_args() -> argparse.Namespace:
    """Command-line arguments of the manifest builder."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True, help="local path or s3:// URI of one year")
    parser.add_argument(
        "--out",
        default=None,
        help=f"destination; defaults to {MANIFEST_NAME} beside the composites",
    )
    parser.add_argument("--max-workers", type=int, default=16)
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing manifest (see the warning this refuses with)",
    )
    parser.add_argument(
        "--allow-problems",
        action="store_true",
        help="write the manifest even for rasters the catalogue would refuse",
    )
    return parser.parse_args()


def main() -> None:
    """Write a manifest for one composite archive that predates manifest emission."""
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    src = settings.resolve_path(args.src)
    out = settings.resolve_path(args.out) if args.out else src / MANIFEST_NAME
    if out.exists() and not args.force:
        # A manifest the run itself wrote records the archive as it was written.
        # Rebuilding it from the files describes the archive as it is *now*, so
        # overwriting silently would erase any drift between the two -- which is
        # exactly the disagreement the manifest exists to make visible.
        msg = (
            f"{out} already exists. Rebuilding it would replace the record of how these "
            "composites were written with one derived from their current contents, hiding "
            "any drift between them. Pass --force if that is what you intend, or --out to "
            "write a copy and compare."
        )
        raise SystemExit(msg)

    # Every helper below enters the environment its own path needs — freezebase
    # for the GDAL reads, fsspec for the listing — so there is nothing to wrap.
    by_tile = stac.discover_composites(src)
    stac.require_unique_composites(by_tile)
    files = [file for tile_files in by_tile.values() for file in tile_files]
    if not files:
        msg = f"No composites found in {src}"
        raise SystemExit(msg)

    logger.info("Reading provenance tags from %d rasters", len(files))
    raster_info = stac.collect_raster_info(by_tile, max_workers=args.max_workers)

    problems = stac.survey_tags(by_tile, raster_info)
    if problems:
        for problem in problems:
            logger.error("%s", problem)
        logger.error("%s", stac.PROVENANCE_EXPECTATION)
        if not args.allow_problems:
            msg = (
                f"{len(problems)} of {len(files)} rasters cannot be published; "
                "fix the archive or pass --allow-problems to record it as it is"
            )
            raise SystemExit(msg)

    listing = stac.list_objects(src)
    percents = valid_percents(by_tile, max_workers=args.max_workers)
    frame = stac.build_manifest_frame(files, raster_info, listing, percents)
    stac.write_manifest(frame, out)

    logger.info(
        "Manifest for %d files across %d tiles written to %s",
        len(frame),
        len(by_tile),
        out,
    )


if __name__ == "__main__":
    main()
