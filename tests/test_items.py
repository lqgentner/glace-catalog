#!/usr/bin/env python3
"""Checks the dates and assets of tile and mosaic items.

All tiles of a year are composited from the same search window, which the
filename and the ``COMPOSITE_*_DATE`` tags record. Every tile item of a year
therefore has the same ``start_datetime``, ``end_datetime`` and ``datetime``,
so a reader stacking them gets one time coordinate. The acquisitions that
actually contributed vary by tile and product. They come from the
``FIRST_ACQUISITION_DATE`` and ``LAST_ACQUISITION_DATE`` tags and are published
per product as ``glace:{coh,rtc}_first_acquisition`` and ``_last_acquisition``.

A tile whose files disagree about the window is refused. The conversion from
window to time range is checked once, since every item uses it.

The mosaic item carries its web-map archives only as assets with the
``visual`` role, beside the COGs they render.

Reads no data and needs no network.

Run: uv run tests/test_items.py
"""

from __future__ import annotations

from datetime import UTC, date, datetime
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from typing import TYPE_CHECKING, Any

from glace_catalog.metadata import vocabulary
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
from glace_catalog.stac.store import BuildState, StoreLayout
from glace_catalog.stac.tags import RasterInfo, required_tags

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

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
    """Check that a stale file from a different window makes the tile fail."""
    info = {
        "coh12_vv": _info("2023-07-21", "2023-09-30"),
        "rtc_vv": _info("2023-07-21", "2023-10-12", COMPOSITE_END_DATE="2023-10-20"),
    }
    try:
        build_tile_item(TILE, files, info)
    except ValueError as error:
        return "2023-10-20" in str(error)
    return False


def _mosaic_item(
    checksum: str | None = None,
    extra: Mapping[str, MosaicAsset] | None = None,
) -> pystac.Item:
    """Build one year's mosaic item over a single asset, plus whatever is passed in."""
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
            **(extra or {}),
        },
    )


def _publisher() -> ModuleType:
    """Load ``scripts/publish-year.py`` as a module.

    The script resolves its store root on import, so the gate first points it
    at a local placeholder path.
    """
    os.environ.setdefault("GLACE_STORE_ROOT", str(Path(__file__).parent / "does-not-exist"))
    spec = importlib.util.spec_from_file_location(
        "publish_year",
        Path(__file__).resolve().parents[1] / "scripts" / "publish-year.py",
    )
    if spec is None or spec.loader is None:
        msg = "scripts/publish-year.py did not load"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pmtiles_state() -> BuildState:
    """Build a state holding two archives, as the ``pmtiles`` step records them."""
    state = BuildState(path=Path("/nowhere/.build-state.json"))
    archives: dict[str, dict[str, Any]] = {}
    for key, polarization in (("coh12_vv", "VV"), ("rtc_vv", "VV")):
        encoding = vocabulary().display(key).encoding
        archives[f"{key}_viz"] = {
            "url": f"{key}_viz.pmtiles",
            "size_bytes": 10,
            "checksum": "1220" + "ab" * 32,
            "scale": encoding.scale,
            "offset": encoding.offset,
            "units": "",
            "min_zoom": 5,
            "max_zoom": 12,
            "bounds": [5.9, 45.8, 10.5, 47.8],
            "product": key.split("_")[0].upper(),
            "polarization": polarization,
            "year": 2024,
            "id": f"{key}_2024",
            "start_date": "2024-07-09",
            "end_date": "2024-10-07",
        }
    state.details["pmtiles"] = archives
    return state


def _staging_is_cleared() -> bool:
    """Check that an earlier build's archives leave staging and other files stay."""
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp)
        (staged / "coh12_vv.pmtiles").write_bytes(b"old")
        (staged / "coh12_vv_viz.pmtiles").write_bytes(b"old too")
        (staged / "notes.txt").write_bytes(b"keep")
        removed = _publisher().clear_stale_archives(staged)
        return removed == 2 and sorted(p.name for p in staged.iterdir()) == ["notes.txt"]


def main() -> int:  # noqa: PLR0915  (one check per line, and there are many)
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
        "no filename-derived span fields": "glace:coh_start" not in props
        and "glace:rtc_end" not in props,
        "acquisition tags reach the asset": item.assets["coh12_vv"].extra_fields.get(
            "glace:first_acquisition_date",
        )
        == "2023-07-21",
    }
    checks["a file from another window refuses the tile"] = _refuses_mixed_windows(files)
    # The archives are reached through `visual` assets alone, a recorded
    # deviation from `PORTO-FMT-011` (docs/conformance.md). Check this on an
    # item that carries the renderings.
    built = _publisher().visual_assets(_pmtiles_state())
    rendered = _mosaic_item(extra=built)
    mosaic = _mosaic_item()
    checks["the mosaic item links no PMTiles archive"] = not rendered.get_links("pmtiles")
    checks["the mosaic item declares no web-map-links extension"] = not any(
        "web-map-links" in uri for uri in rendered.stac_extensions
    )
    # Portolan rule PTL-AST-003 asks for a checksum on every asset the run wrote.
    # Mosaics are hashed as they are staged; an asset with no measured checksum
    # omits the field.
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
        data_key("rtc_vh_qa_cqm_viz") == "rtc_vh_qa_cqm"
    )
    try:
        data_key("coh12_vv")
        checks["a key without the suffix is refused"] = False
    except ValueError:
        checks["a key without the suffix is refused"] = True
    layout = StoreLayout(Path("/store"))
    checks["a rendering sits in its year's item directory"] = layout.viz(2024, "coh12_vv") == Path(
        "/store/mosaics/2024/coh12_vv_viz.pmtiles"
    )
    checks["the layout has no pmtiles directory"] = not hasattr(layout, "pmtiles")
    checks["visual_assets keys by rendering key"] = list(built) == [
        "coh12_vv_viz",
        "rtc_vv_viz",
    ]
    viz = rendered.assets["coh12_vv_viz"]
    checks["a rendering is an item asset with the visual role"] = viz.roles == ["visual"]
    checks["a rendering is not data"] = "data" not in (viz.roles or [])
    checks["a rendering declares its own grid"] = viz.extra_fields.get("proj:code") == "EPSG:3857"
    checks["a rendering carries its digest"] = str(
        viz.extra_fields.get("file:checksum", "")
    ).startswith("1220")
    checks["a rendering is a PMTiles archive"] = viz.media_type == "application/vnd.pmtiles"
    checks["a rendering's href is beside the COG"] = viz.href == "coh12_vv_viz.pmtiles"
    checks["a rendering says what it is"] = 'read the COGs with the "data" role' in (
        viz.description or ""
    )
    band = viz.extra_fields.get("bands", [{}])[0]
    checks["a rendering's band decodes its codes"] = (
        band.get("data_type") == "uint8"
        and band.get("nodata") == 0
        and band.get("raster:scale") == vocabulary().display("coh12_vv").encoding.scale
    )
    checks["a decibel rendering's band carries its unit"] = (
        rendered.assets["rtc_vv_viz"].extra_fields["bands"][0].get("unit") == "dB"
    )
    checks["a tile band is named by its asset key"] = (
        item.assets["coh12_vv"].extra_fields["bands"][0]["name"] == "coh12_vv"
    )
    # Titles come from the vocabulary, so an item and its collection's
    # `item_assets` always name a file the same way.
    words = vocabulary()
    checks["a tile asset is titled by the vocabulary"] = (
        item.assets["coh12_vv"].title == words.asset_title("coh12_vv") == "Coherence VV"
    )
    checks["a tile asset is described by the vocabulary"] = (
        item.assets["rtc_vv"].description == words.item_asset("rtc_vv", "tiles")["description"]
    )
    checks["a tile data asset keeps its sar:product_type"] = (
        item.assets["rtc_vv"].extra_fields.get("sar:product_type") == "RTC"
    )
    checks["a rendering is titled by the vocabulary"] = viz.title == "Coherence VV – PMTiles"
    checks["a rendering's description carries the tiled zoom range"] = "z5-z12" in (
        viz.description or ""
    )
    checks["a rebuild starts from a staging directory without archives"] = _staging_is_cleared()
    # A build state recorded before the transport range changed describes an
    # archive encoded with the old range, so publishing it must fail.
    stale = _pmtiles_state()
    stale.details["pmtiles"]["coh12_vv_viz"]["scale"] = 0.005
    try:
        _publisher().visual_assets(stale)
        checks["an archive encoded over a stale range is refused"] = False
    except SystemExit:
        checks["an archive encoded over a stale range is refused"] = True

    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        return 1
    print(f"OK: {len(checks)} item check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
