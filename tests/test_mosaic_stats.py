#!/usr/bin/env python3
"""Checks the metadata an annual mosaic COG carries in its own header.

Portolan requires embedded band statistics (``PTL-DAT-009``), plus the valid
percent for bands that declare nodata (``PTL-DAT-010``). It reads them from the
file's own ``GDAL_METADATA`` tag, since only the COG itself is published.
The per-tile composites get their statistics from s1bursts; the mosaics are
written in this repository, so they are checked here.

The ``file:checksum`` digest is computed during the same write, while the file
is still on local disk. The writer also checks that the overviews reach down
to one tile (``PTL-DAT-011``).

Writes a tiny raster into a temporary directory. Reads no data and needs no
network.

Run: uv run tests/test_mosaic_stats.py
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile

import numpy as np
import rasterio
import rasterio.shutil
from rasterio.transform import from_origin

from glace_catalog.mosaics import (
    COG_CREATION,
    STATISTICS_TAGS,
    build_mosaic_cog,
    overview_factors,
    require_overviews,
)

VALUES = np.array(
    [
        [0.25, 0.75, np.nan, 1.0],
        [0.5, 0.5, 0.5, np.nan],
        [np.nan, np.nan, 0.0, 1.5],
        [0.25, 0.75, 1.25, np.nan],
    ],
    dtype=np.float32,
)
"""Twelve finite values in sixteen pixels, so the valid percent is not 100."""


def _source(tmp: Path) -> Path:
    """Write the fixture raster and a VRT over it, as the VRT chain passes it on."""
    source = tmp / "source.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=VALUES.shape[1],
        height=VALUES.shape[0],
        count=1,
        dtype="float32",
        crs="EPSG:3035",
        transform=from_origin(4_000_000, 2_700_000, 40, 40),
        nodata=np.nan,
    ) as dst:
        dst.write(VALUES, 1)
    vrt = tmp / "source.vrt"
    with rasterio.open(source) as src:
        rasterio.shutil.copy(src, str(vrt), driver="VRT")
    return vrt


def _expected() -> dict[str, float]:
    """Compute the five expected statistics independently of the code under test."""
    finite = VALUES[np.isfinite(VALUES)].astype("float64")
    return {
        "minimum": float(finite.min()),
        "maximum": float(finite.max()),
        "mean": float(finite.mean()),
        "stddev": float(finite.std()),  # population, as GDAL computes it
        "valid_percent": 100.0 * finite.size / VALUES.size,
    }


def _empty_band_refused(tmp: Path) -> bool:
    """Check an all-nodata band is a run failure that names the asset key."""
    source = tmp / "empty.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="float32",
        crs="EPSG:3035",
        transform=from_origin(4_000_000, 2_700_000, 40, 40),
        nodata=np.nan,
    ) as dst:
        dst.write(np.full((4, 4), np.nan, dtype=np.float32), 1)
    vrt = tmp / "empty.vrt"
    with rasterio.open(source) as src:
        rasterio.shutil.copy(src, str(vrt), driver="VRT")
    try:
        build_mosaic_cog(
            vrt,
            tmp / "empty_out.tif",
            asset_key="rtc_vh_qa_cqm",
            band_name="x",
        )
    except ValueError as error:
        return "rtc_vh_qa_cqm" in str(error)
    return False


def _overview_checks(tmp: Path) -> dict[str, bool]:
    """Check the overview levels of a mosaic larger than one tile, and a refusal."""
    source = tmp / "wide.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=600,
        height=300,
        count=1,
        dtype="float32",
        crs="EPSG:3035",
        transform=from_origin(4_000_000, 2_700_000, 40, 40),
        nodata=np.nan,
    ) as dst:
        dst.write(np.ones((300, 600), dtype=np.float32), 1)
    vrt = tmp / "wide.vrt"
    with rasterio.open(source) as src:
        rasterio.shutil.copy(src, str(vrt), driver="VRT")
    written = build_mosaic_cog(vrt, tmp / "wide_out.tif", asset_key="coh12_vv", band_name="x")
    with rasterio.open(written.path) as src:
        levels = src.overviews(1)

    # Written directly, with one overview level where the size needs two.
    shallow = tmp / "shallow.tif"
    with rasterio.open(source) as src:
        rasterio.shutil.copy(
            src, str(shallow), **{**COG_CREATION, "MAX_Z_ERROR": 0, "OVERVIEW_COUNT": 1}
        )
    try:
        require_overviews(shallow)
    except ValueError as error:
        shallow_message = str(error)
    else:
        shallow_message = ""

    return {
        "overviews: the levels GDAL writes for the Alps mosaic": overview_factors(
            24_100, 14_475, 256
        )
        == [2, 4, 8, 16, 32, 64, 128],
        "overviews: none for a raster within one tile": overview_factors(256, 256, 256) == [],
        "overviews: sides are rounded down, as GDAL does": overview_factors(513, 100, 256) == [2],
        "overviews: the mosaic writer writes every level": levels == [2, 4],
        "overviews: a COG missing a level is refused": "[2]" in shallow_message
        and "[2, 4]" in shallow_message,
    }


def main() -> int:
    """Run the gate."""
    expected = _expected()
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        written = build_mosaic_cog(
            _source(tmp),
            tmp / "out" / "coh12_vv.tif",
            asset_key="coh12_vv",
            band_name="12-day repeat-pass coherence VV",
            tags={"COMPOSITING_METHOD": "LRW"},
        )
        # With PAM off, GDAL reads only the file's own header, as Portolan does.
        with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(written.path) as src:
            band_tags = src.tags(1)
            dataset_tags = src.tags()
            description = src.descriptions[0]
        sidecars = sorted(written.path.parent.glob("*.aux.xml"))
        payload = written.path.read_bytes()
        empty_refused = _empty_band_refused(tmp)
        overview_checks = _overview_checks(tmp)

    embedded = {
        field: float(band_tags[tag]) for field, tag in STATISTICS_TAGS.items() if tag in band_tags
    }
    checks = {
        "the five statistics come back from the COG's own header": set(embedded)
        == set(STATISTICS_TAGS),
        "they are the values of the source pixels": all(
            abs(embedded.get(field, float("inf")) - value) < 1e-6
            for field, value in expected.items()
        ),
        "the returned record carries the same statistics": all(
            abs(value - expected[field]) < 1e-6
            for field, value in written.statistics.to_dict().items()
        ),
        "no PAM sidecar was written beside the COG": not sidecars,
        "the dataset tags survive beside the band ones": dataset_tags.get("COMPOSITING_METHOD")
        == "LRW",
        "the band description survives": description == "12-day repeat-pass coherence VV",
        "the checksum is the multihash of the written bytes": written.checksum
        == "1220" + hashlib.sha256(payload).hexdigest(),
        "an all-nodata band is refused, by asset key": empty_refused,
        **overview_checks,
    }
    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        print(f"       band tags: {band_tags}")
        print(f"       expected:  {expected}")
        return 1
    print(f"OK: {len(checks)} mosaic-statistics check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
