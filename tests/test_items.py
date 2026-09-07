#!/usr/bin/env python3
"""Gate on the dates a tile item publishes.

Every tile of a year is composited from the same search window, and that window
is what the filename and ``COMPOSITE_*_DATE`` tags carry, so every tile item of
a year gets the same ``start_datetime``, ``end_datetime`` and ``datetime`` and a
reader stacking them gets one time coordinate. Which acquisitions actually
contributed varies per tile and product; that is the ``FIRST_ACQUISITION_DATE``
and ``LAST_ACQUISITION_DATE`` tags, published per product as
``glace:{coh,rtc}_first_acquisition`` / ``_last_acquisition``.

Reads no data and needs no network.

Run: python3 tests/test_items.py
"""

from __future__ import annotations

from pathlib import Path
import sys

from glace_catalog.stac.items import build_tile_item
from glace_catalog.stac.naming import parse_composite_name
from glace_catalog.stac.tags import RasterInfo, required_tags

TILE = "32TPS05"
WINDOW = "20230718_20231015"


def _info(first: str, last: str, **extra: str) -> RasterInfo:
    """Build a raster info carrying every required tag, with the given span."""
    tags = {
        "METADATA_VOCABULARY": "CEOS-ARD PFS SAR v1.3.1",
        "PIPELINE_SOFTWARE": "s1bursts",
        "PIPELINE_SOFTWARE_VERSION": "0.5.0",
        "PIPELINE_SOFTWARE_URL": "https://github.com/lqgentner/s1bursts",
        "PROCESSING_SOFTWARE": "ESA SNAP",
        "PROCESSING_SOFTWARE_VERSION": "14.0.0",
        "DEM": "Copernicus GLO-30",
        "PRODUCT_TYPE": "X",
        "PRODUCT_FAMILY": "Composite",
        "PRODUCT_LEVEL": "L3",
        "SOURCE_DATA_PRODUCT_LEVEL": "L2",
        "MGRS_TILE": TILE,
        "COMPOSITE_START_DATE": "2023-07-18",
        "COMPOSITE_END_DATE": "2023-10-15",
        "FIRST_ACQUISITION_DATE": first,
        "LAST_ACQUISITION_DATE": last,
        "POLARIZATION": "VV",
        "PIXEL_COORDINATE_CONVENTION": "pixel centre",
        "COMPOSITING_METHOD": "Local resolution weighting",
        "COMPOSITING_METHOD_REFERENCE": "https://doi.org/x",
        "COMPOSITING_PARAMETERS": "aggregation=median",
        "CONTRIBUTING_OBSERVATIONS": "40",
        "LAYER_NAME": "x",
        "LAYER_DESCRIPTION": "x",
        "BACKSCATTER_MEASUREMENT": "Gamma-0",
        "BACKSCATTER_CONVENTION": "Power",
        "BACKSCATTER_CONVERSION_EQ": "dB = 10*log10(DN)",
        "MEASUREMENT_TYPE": "coherence",
        "TEMPORAL_BASELINE_DAYS": "12",
        "PHASE_QUALITY_METHOD": "x",
        "PHASE_QUALITY_REFERENCE": "x",
        "COHERENCE_WINDOW_RANGE": "35",
        "COHERENCE_WINDOW_AZIMUTH": "7",
        "COHERENCE_WEIGHTING_SHAPE": "Rectangular",
        **extra,
    }
    return RasterInfo(tags=tags, band_name="x", data_type="float32", nodata=float("nan"))


def main() -> int:
    """Run the gate."""
    names = {
        "coh12_vv": f"S1_COH12_LRW_{TILE}_VV_{WINDOW}.tif",
        "rtc_vv": f"S1_RTC_LRW_{TILE}_VV_{WINDOW}.tif",
    }
    files = [parse_composite_name(Path("/a") / name) for name in names.values()]
    assert all(files)
    for file in files:
        missing = set(required_tags(file)) - set(_info("x", "x").tags)  # type: ignore[union-attr]
        if missing:
            print(f"error  fixture lacks required tags {sorted(missing)}")
            return 1
    info = {
        "coh12_vv": _info("2023-07-21", "2023-09-30"),  # coherence ends a pair earlier
        "rtc_vv": _info("2023-07-21", "2023-10-12"),
    }
    item = build_tile_item(TILE, files, info)  # type: ignore[arg-type]
    props = item.properties

    checks = {
        "start is the window": props["start_datetime"].startswith("2023-07-18"),
        "end is the day after the window's last day": props["end_datetime"].startswith(
            "2023-10-16"
        ),
        "datetime is the window midpoint": item.datetime is not None
        and item.datetime.date().isoformat() == "2023-09-01",
        "coherence span from its tags": (
            props.get("glace:coh_first_acquisition"),
            props.get("glace:coh_last_acquisition"),
        )
        == ("2023-07-21", "2023-09-30"),
        "backscatter span from its tags": (
            props.get("glace:rtc_first_acquisition"),
            props.get("glace:rtc_last_acquisition"),
        )
        == ("2023-07-21", "2023-10-12"),
        "the filename-derived span fields are gone": "glace:coh_start" not in props
        and "glace:rtc_end" not in props,
        "acquisition tags reach the asset": item.assets["coh12_vv"].extra_fields.get(
            "glace:first_acquisition_date",
        )
        == "2023-07-21",
    }
    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        return 1
    print(f"OK: {len(checks)} item date check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
