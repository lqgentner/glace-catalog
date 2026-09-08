#!/usr/bin/env python3
"""Gate on what an annual mosaic COG carries about itself.

Portolan raises the embedded band statistics to an error (``PTL-DAT-009``, and
``PTL-DAT-010`` for the valid percent wherever a band declares nodata) and reads
them from the file's own ``GDAL_METADATA``: a PAM sidecar does not count, and
the sidecar is not published anyway. The per-tile composites get theirs from
s1bursts; the mosaics are written here, so this is where they have to be proved.

The digest published as ``file:checksum`` is measured on the same write, since
the file is staged locally before upload and hashing it there costs nothing.

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

from glace_catalog.mosaics import STATISTICS_TAGS, build_mosaic_cog

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
    """Write the fixture raster and a VRT over it, the shape the chain hands the writer."""
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
    """Compute the five statistics here rather than in the code under test."""
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
        # PAM off, so a value can only have come out of the file's own header --
        # which is where Portolan reads it from, and the sidecar is never published.
        with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(written.path) as src:
            band_tags = src.tags(1)
            dataset_tags = src.tags()
            description = src.descriptions[0]
        sidecars = sorted(written.path.parent.glob("*.aux.xml"))
        payload = written.path.read_bytes()
        empty_refused = _empty_band_refused(tmp)

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
