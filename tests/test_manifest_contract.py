#!/usr/bin/env python3
"""Checks the run-manifest contract with ``deep-glacier-mapping``.

The composite run in that repository writes ``manifest.parquet``, and the
build here reads it. The file is the only contract between the two
repositories, so a change on either side must show up here. This gate writes a
manifest in exactly the producer's column layout and checks that everything the
build needs survives the round trip, including the checksum and the band
statistics, which the build takes from the manifest alone. It also checks
how the build writes files into a store.

The column list below is copied verbatim from
``glacier_mapping/manifest.py::COLUMNS``; keep the two identical.

Reads no data and needs no network.

Run: uv run tests/test_manifest_contract.py
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from upath import UPath

from glace_catalog.stac import store
from glace_catalog.stac.naming import parse_composite_name
from glace_catalog.stac.store import (
    MANIFEST_COLUMNS,
    StoreLayout,
    copy_composites,
    drop_empty_composites,
    manifest_composites,
    manifest_raster_info,
    manifest_valid_percents,
    read_manifest,
)
from glace_catalog.stac.tags import BandStatistics

if TYPE_CHECKING:
    from types import ModuleType

PRODUCER_COLUMNS = (
    "filename",
    "tile",
    "asset_key",
    "product",
    "qa",
    "polarization",
    "composite_start",
    "composite_end",
    "size",
    "sha256",
    "valid_percent",
    "band_name",
    "band_unit",
    "data_type",
    "nodata",
    "spatial_resolution",
    "bits_per_sample",
    "byte_order",
    "stat_minimum",
    "stat_maximum",
    "stat_mean",
    "stat_stddev",
    "stat_valid_percent",
)

DIGEST = "1220" + "ab" * 32


def _build_year() -> ModuleType:
    """Load ``scripts/build-year.py`` as a module.

    The script resolves its store root on import, so the gate first points it
    at a local placeholder path.
    """
    os.environ.setdefault("GLACE_STORE_ROOT", str(Path(__file__).parent / "does-not-exist"))
    spec = importlib.util.spec_from_file_location(
        "build_year",
        Path(__file__).resolve().parents[1] / "scripts" / "build-year.py",
    )
    if spec is None or spec.loader is None:
        msg = "scripts/build-year.py did not load"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def producer_manifest() -> pd.DataFrame:
    """Two rows as the driver writes them: a measurement and its count band."""
    common = {
        "tile": "32TPS05",
        "polarization": "VV",
        "product": "COH12",
        "composite_start": "2024-06-19",
        "composite_end": "2024-09-12",
        "spatial_resolution": 10.0,
        "byte_order": "little-endian",
        # Dataset tags follow the fixed columns, one column per tag, as written.
        # These are the tags of an LRW composite; PASS_DIRECTION belongs to
        # pass composites only.
        "PROCESSING_SOFTWARE": "s1bursts 0.4.0",
        "COMPOSITING_METHOD": "LRW",
    }
    rows = [
        {
            **common,
            "filename": "S1_COH12_LRW_32TPS05_VV_20240619_20240912.tif",
            "asset_key": "coh12_vv",
            "qa": None,
            "size": 1234,
            "sha256": DIGEST,
            "valid_percent": 84.375,
            "band_name": "coherence",
            "band_unit": "",
            "data_type": "float32",
            "nodata": np.nan,
            "bits_per_sample": 32,
            "stat_minimum": 0.01,
            "stat_maximum": 0.99,
            "stat_mean": 0.5,
            "stat_stddev": 0.2,
            "stat_valid_percent": 87.5,
        },
        {
            **common,
            "filename": "S1_COH12_LRW_QA-NUM_32TPS05_VV_20240619_20240912.tif",
            "asset_key": "coh12_vv_qa_num",
            "qa": "NUM",
            "size": 99,
            "sha256": None,  # a file carried over from an earlier run
            "valid_percent": None,
            "band_name": "observations",
            "band_unit": "",
            "data_type": "uint16",
            "nodata": 0.0,
            "bits_per_sample": 16,
            "stat_minimum": 1.0,
            "stat_maximum": 3.0,
            "stat_mean": 2.9,
            "stat_stddev": 0.3,
            "stat_valid_percent": 84.375,
        },
    ]
    frame = pd.DataFrame(
        rows, columns=[*PRODUCER_COLUMNS, "PROCESSING_SOFTWARE", "COMPOSITING_METHOD"]
    )
    frame.attrs["manifest_version"] = 1
    return frame


def empty_composite_checks() -> dict[str, bool]:
    """Check that an empty measurement is dropped with its QA layers, and many stop the build."""
    full = producer_manifest()
    empty = full.copy()
    empty["tile"] = "32TNT24"
    empty["filename"] = empty["filename"].str.replace("32TPS05", "32TNT24")
    empty["valid_percent"] = np.nan
    many = pd.concat([full] * 99 + [empty], ignore_index=True)
    kept, dropped = drop_empty_composites(many)
    checks = {
        "an empty measurement is reported": dropped == ["32TNT24 coh12_vv"],
        "an empty measurement goes with its QA layers": "32TNT24" not in set(kept["tile"]),
        "a measurement with valid pixels stays, QA without a percentage too": len(kept)
        == len(full) * 99,
    }
    try:
        drop_empty_composites(pd.concat([full, empty], ignore_index=True))
        checks["a run with many empty measurements stops the build"] = False
    except ValueError:
        checks["a run with many empty measurements stops the build"] = True
    return checks


def refuses_partial_statistics() -> bool:
    """Check a manifest missing one ``stat_*`` value is refused, by column name.

    Four of five values means a producer bug. The build reports it at once,
    by column, close to the run that caused it.
    """
    frame = producer_manifest()
    frame.loc[0, "stat_mean"] = None
    try:
        manifest_raster_info(frame)
    except ValueError as error:
        return "stat_mean" in str(error)
    return False


def mosaic_tags_survive_missing_column(frame: pd.DataFrame) -> bool:
    """Check the mosaic tag collector tolerates a provenance tag the run never wrote.

    ``PROVENANCE_TAGS`` covers every product, while a manifest holds only its
    own product's tags: ``PASS_DIRECTION``, for example, appears only for pass
    composites.
    """
    tags = _build_year().mosaic_tags(frame, "coh12_vv")
    return tags.get("COMPOSITING_METHOD") == "LRW" and "PASS_DIRECTION" not in tags


def local_copy_links(tmp: Path) -> dict[str, bool]:
    """Check a local archive is hard-linked into a local store, and a rerun skips it."""
    source = tmp / "archive" / "S1_COH12_LRW_32TPS05_VV_20240619_20240912.tif"
    source.parent.mkdir()
    source.write_bytes(b"x" * 64)
    file = parse_composite_name(source)
    if file is None:
        return {"the test composite name parses": False}
    layout = StoreLayout(tmp / "store")
    listing = {source.name: 64}
    first = copy_composites([file], layout, 2024, listing)
    target = layout.tile(2024, file.tile) / file.published_name
    second = copy_composites([file], layout, 2024, listing)
    return {
        "a local composite is linked, not duplicated": (
            first.copied == 1
            and isinstance(target, Path)
            and target.stat().st_ino == source.stat().st_ino
        ),
        "a rerun skips a linked composite": second.copied == 0 and second.skipped == 1,
    }


def store_writes(tmp: Path) -> dict[str, bool]:
    """Check staged files reach a local store whole, and an S3 store through ``upload_object``."""
    staged = tmp / "staged"
    (staged / "2024").mkdir(parents=True)
    (staged / "2024" / "a.tif").write_bytes(b"new")
    store_root = tmp / "store"
    (store_root / "2024").mkdir(parents=True)
    (store_root / "2024" / "a.tif").write_bytes(b"old")
    uploaded = store.upload_tree(staged, store_root)
    store.put_object(b"thumb", store_root / "thumbnail.webp")
    local = {
        "a staged tree replaces the store's files": uploaded == 1
        and (store_root / "2024" / "a.tif").read_bytes() == b"new",
        "bytes are written to a local store": (store_root / "thumbnail.webp").read_bytes()
        == b"thumb",
        "a local write leaves no temporary file": sorted(
            path.name for path in store_root.rglob("*")
        )
        == ["2024", "a.tif", "thumbnail.webp"],
    }

    calls: list[tuple[Path | bytes, str]] = []

    def record(src: Path | bytes, dst: Path | UPath | str) -> None:
        calls.append((src, str(dst)))

    original = store.upload_object
    store.upload_object = record
    try:
        store.upload_tree(staged, UPath("s3://bucket/glace"))
    finally:
        store.upload_object = original
    return {
        **local,
        "an S3 store is written through upload_object": calls
        == [(staged / "2024" / "a.tif", "s3://bucket/glace/2024/a.tif")],
    }


def source_pattern_checks() -> dict[str, bool]:
    """Check that ``GLACE_SOURCE`` names each year's archive and refuses a pattern without one."""
    settings = _build_year().settings
    locations = settings.locations
    original = locations.source
    refused: dict[str, bool] = {}
    try:
        for name, pattern in (("an unset source", None), ("a source without {year}", "/a/b")):
            locations.source = pattern
            try:
                settings.source(2023)
                refused[f"{name} is refused"] = False
            except SystemExit as error:
                refused[f"{name} is refused"] = "GLACE_SOURCE" in str(error)
        locations.source = "/data/alps-total/{year}/s1-comp"
        named = str(settings.source(2023)) == "/data/alps-total/2023/s1-comp"
    finally:
        locations.source = original
    return {**refused, "the source pattern names the year's archive": named}


def main() -> int:
    """Run the gate."""
    missing = set(MANIFEST_COLUMNS) - set(PRODUCER_COLUMNS)
    if missing:
        print(f"error  the build requires columns the producer does not write: {sorted(missing)}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "manifest.parquet"
        producer_manifest().to_parquet(path, index=False)
        frame = read_manifest(path)

        files = manifest_composites(frame, Path(tmp))
        info = manifest_raster_info(frame)
        percents = manifest_valid_percents(frame)

    data = info["32TPS05"]["coh12_vv"]
    count = info["32TPS05"]["coh12_vv_qa_num"]
    checks = {
        "two composites parsed": [f.asset_key for f in files] == ["coh12_vv", "coh12_vv_qa_num"],
        "digest reaches RasterInfo.checksum": data.checksum == DIGEST,
        "absent digest stays None": count.checksum is None,
        "statistics reach RasterInfo.statistics": data.statistics
        == BandStatistics(minimum=0.01, maximum=0.99, mean=0.5, stddev=0.2, valid_percent=87.5),
        "size carried": data.size == 1234,
        "NaN nodata stays NaN": data.nodata is not None and np.isnan(data.nodata),
        "zero nodata stays zero": count.nodata == 0.0,
        "tags carried": data.tags.get("COMPOSITING_METHOD") == "LRW",
        "mosaic tags survive a manifest without the pass-only column": (
            mosaic_tags_survive_missing_column(frame)
        ),
        "valid percent keyed by the data asset": percents == {"32TPS05": {"coh12_vv": 84.375}},
        "a manifest missing one statistic is refused by column name": (
            refuses_partial_statistics()
        ),
    }
    checks.update(empty_composite_checks())
    checks.update(source_pattern_checks())
    with tempfile.TemporaryDirectory() as tmp:
        checks.update(local_copy_links(Path(tmp)))
    with tempfile.TemporaryDirectory() as tmp:
        checks.update(store_writes(Path(tmp)))
    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        return 1
    print(f"OK: {len(checks)} contract check(s), {len(PRODUCER_COLUMNS)} producer column(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
