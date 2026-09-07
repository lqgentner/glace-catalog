#!/usr/bin/env python3
"""Gate on the run-manifest contract with ``deep-glacier-mapping``.

The composite driver there writes ``manifest.parquet``; the publisher here reads
it. The two repositories share no import (M-30), so the contract is the file,
and this gate is where a change on either side has to show up: it writes a
manifest in **exactly the producer's column layout** and checks that everything
the publisher needs survives the round trip -- including the digest and the
band statistics, which are the two values that cannot be recovered from a
header read at publication. A header read is checked too, since the publisher
reads the mosaics it writes itself the same way.

The column list below is copied verbatim from
``glacier_mapping/manifest.py::COLUMNS``. Keep it that way.

Reads no data and needs no network.

Run: python3 tests/test_manifest_contract.py
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin

from glace_catalog.stac.store import (
    MANIFEST_COLUMNS,
    manifest_composites,
    manifest_raster_info,
    manifest_valid_percents,
    read_manifest,
)
from glace_catalog.stac.tags import read_raster_info

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
        # Dataset tags follow the fixed columns, one column each, uncurated.
        "PROCESSING_SOFTWARE": "s1bursts 0.4.0",
        "PASS_DIRECTION": "Ascending",
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
            "sha256": None,  # a resumed run's file, written by nobody this run
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
        rows, columns=[*PRODUCER_COLUMNS, "PROCESSING_SOFTWARE", "PASS_DIRECTION"]
    )
    frame.attrs["manifest_version"] = 1
    return frame


def header_checks(tmp: Path) -> dict[str, bool]:
    """Check the header reader picks up the band statistics the writer embeds."""
    path = tmp / "S1_COH12_LRW_32TPS05_VV_20240619_20240912.tif"
    data = np.full((4, 4), 0.5, dtype=np.float32)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="float32",
        crs="EPSG:32632",
        transform=from_origin(500_000, 5_200_000, 10, 10),
        nodata=np.nan,
    ) as dst:
        dst.write(data, 1)
        dst.update_tags(
            1, STATISTICS_MINIMUM="0.5", STATISTICS_MAXIMUM="0.5", STATISTICS_VALID_PERCENT="100"
        )
    info = read_raster_info(path)
    return {
        "header reader fills statistics from band tags": info.statistics
        == {"minimum": 0.5, "maximum": 0.5, "valid_percent": 100.0},
    }


def main() -> int:
    """Run the gate."""
    missing = set(MANIFEST_COLUMNS) - set(PRODUCER_COLUMNS)
    if missing:
        print(
            f"error  the publisher requires columns the producer does not write: {sorted(missing)}"
        )
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "manifest.parquet"
        producer_manifest().to_parquet(path, index=False)
        frame = read_manifest(path)

        files = manifest_composites(frame, Path(tmp))
        info = manifest_raster_info(frame)
        percents = manifest_valid_percents(frame)
        header = header_checks(Path(tmp))

    data = info["32TPS05"]["coh12_vv"]
    count = info["32TPS05"]["coh12_vv_qa_num"]
    checks = {
        "two composites parsed": [f.asset_key for f in files] == ["coh12_vv", "coh12_vv_qa_num"],
        "digest reaches RasterInfo.checksum": data.checksum == DIGEST,
        "absent digest stays None": count.checksum is None,
        "statistics reach RasterInfo.statistics": data.statistics
        == {"minimum": 0.01, "maximum": 0.99, "mean": 0.5, "stddev": 0.2, "valid_percent": 87.5},
        "size carried": data.size == 1234,
        "NaN nodata stays NaN": data.nodata is not None and np.isnan(data.nodata),
        "zero nodata stays zero": count.nodata == 0.0,
        "tags carried": data.tags.get("PASS_DIRECTION") == "Ascending",
        "valid percent keyed by the data asset": percents == {"32TPS05": {"coh12_vv": 84.375}},
    }
    checks.update(header)
    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        return 1
    print(f"OK: {len(checks)} contract check(s), {len(PRODUCER_COLUMNS)} producer column(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
