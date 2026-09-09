#!/usr/bin/env python3
"""Gate on the dates a tile item publishes.

Every tile of a year is composited from the same search window, and that window
is what the filename and ``COMPOSITE_*_DATE`` tags carry, so every tile item of
a year gets the same ``start_datetime``, ``end_datetime`` and ``datetime`` and a
reader stacking them gets one time coordinate. Which acquisitions actually
contributed varies per tile and product; that is the ``FIRST_ACQUISITION_DATE``
and ``LAST_ACQUISITION_DATE`` tags, published per product as
``glace:{coh,rtc}_first_acquisition`` / ``_last_acquisition``.

A tile whose files disagree about the window is refused rather than widened,
and the window-to-range translation is checked once, since every item uses it.

The mosaic item is checked for what it must *not* carry: the web-map archives
are registered on the mosaics collection, never on an item.

Reads no data and needs no network.

Run: uv run tests/test_items.py
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
import sys
from typing import TYPE_CHECKING

from glace_catalog.scopes import get_scope
from glace_catalog.stac.items import (
    MosaicAsset,
    build_mosaic_item,
    build_tile_item,
    published_time_range,
)
from glace_catalog.stac.naming import (
    CompositeFile,
    data_key,
    parse_composite_name,
    viz_filename,
    viz_key,
)
from glace_catalog.stac.tags import RasterInfo, required_tags

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pystac

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


def _refuses_mixed_windows(files: Sequence[CompositeFile]) -> bool:
    """Check that a stale file from a different window refuses the tile, never widens it."""
    info = {
        "coh12_vv": _info("2023-07-21", "2023-09-30"),
        "rtc_vv": _info("2023-07-21", "2023-10-12", COMPOSITE_END_DATE="2023-10-20"),
    }
    try:
        build_tile_item(TILE, files, info)
    except ValueError as error:
        return "2023-10-20" in str(error)
    return False


def _mosaic_item(checksum: str | None = None) -> pystac.Item:
    """Build one year's mosaic item over a single asset."""
    return build_mosaic_item(
        get_scope("ch"),
        2023,
        geometry={"type": "Polygon", "coordinates": [[[6.0, 45.8], [10.5, 45.8], [6.0, 47.8]]]},
        bbox=[6.0, 45.8, 10.5, 47.8],
        start=datetime(2023, 7, 18, tzinfo=UTC),
        end=datetime(2023, 10, 16, tzinfo=UTC),
        assets={
            "coh12_vv": MosaicAsset(
                href="coh12_vv.tif",
                title="x",
                roles=["data"],
                checksum=checksum,
            ),
        },
    )


def main() -> int:
    """Run the gate."""
    names = {
        "coh12_vv": f"S1_COH12_LRW_{TILE}_VV_{WINDOW}.tif",
        "rtc_vv": f"S1_RTC_LRW_{TILE}_VV_{WINDOW}.tif",
    }
    parsed = [parse_composite_name(Path("/a") / name) for name in names.values()]
    files = [file for file in parsed if file is not None]
    if len(files) != len(parsed):
        print("error  fixture filename did not parse")
        return 1
    for file in files:
        missing = set(required_tags(file)) - set(_info("x", "x").tags)
        if missing:
            print(f"error  fixture lacks required tags {sorted(missing)}")
            return 1
    info = {
        "coh12_vv": _info("2023-07-21", "2023-09-30"),  # coherence ends a pair earlier
        "rtc_vv": _info("2023-07-21", "2023-10-12"),
    }
    item = build_tile_item(TILE, files, info)
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
    checks["a file from another window refuses the tile"] = _refuses_mixed_windows(files)
    # PMTiles are registered on the mosaics collection (PORTO-FMT-011), which is
    # `finalize-catalog`'s job. An item that also linked them would claim a
    # rendering is one of its measurements, and would declare an extension whose
    # version the collection alone is the source of.
    mosaic = _mosaic_item()
    checks["the mosaic item links no PMTiles archive"] = not mosaic.get_links("pmtiles")
    checks["the mosaic item declares no web-map-links extension"] = not any(
        "web-map-links" in uri for uri in mosaic.stac_extensions
    )
    # PTL-AST-003 wants a digest on every asset whose bytes the run wrote; the
    # mosaics are hashed as they are staged, and an unmeasured asset says nothing
    # rather than guessing.
    digest = "1220" + "ab" * 32
    checks["a measured mosaic asset publishes its digest"] = (
        _mosaic_item(digest).assets["coh12_vv"].extra_fields.get("file:checksum") == digest
    )
    checks["an unmeasured one carries no digest field"] = (
        "file:checksum" not in mosaic.assets["coh12_vv"].extra_fields
    )
    checks["the published range is the helper's"] = published_time_range(
        (date(2023, 7, 18), date(2023, 10, 15))
    ) == (datetime(2023, 7, 18, tzinfo=UTC), datetime(2023, 10, 16, tzinfo=UTC))
    checks["a rendering's key is the data stem plus _viz"] = viz_key("coh12_vv") == "coh12_vv_viz"
    checks["a rendering's filename follows its key"] = (
        viz_filename("rtc_vh_qa_cqm") == "rtc_vh_qa_cqm_viz.pmtiles"
    )
    checks["the data stem is recoverable from the rendering key"] = (
        data_key("coh12_rgb_viz") == "coh12_rgb"
    )
    try:
        data_key("coh12_vv")
        checks["a key without the suffix is refused"] = False
    except ValueError:
        checks["a key without the suffix is refused"] = True
    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        return 1
    print(f"OK: {len(checks)} item check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
