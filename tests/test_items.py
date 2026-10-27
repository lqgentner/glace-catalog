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
``visual`` role, beside the COGs they render. Its footprint is simplified to a
few vertices per tile edge and still covers every tile.

The item mirrors written from both kinds of item hold exactly the columns
``mirror_columns`` in ``metadata/assets.yaml`` documents.

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
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from freezebase.mgrs import MGRSGeoBox
import pyarrow.parquet as pq
import pystac
from shapely import get_num_coordinates
from shapely.geometry import shape

from glace_catalog.metadata import vocabulary
from glace_catalog.mosaics import data_footprint
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
from glace_catalog.stac.save import (
    concat_indexes,
    index_from_items,
    item_document,
    write_stac_geoparquet,
    year_index_document,
)
from glace_catalog.stac.store import BuildState, StoreLayout
from glace_catalog.stac.tags import RasterInfo, required_tags

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

TILE = "32TPS05"
WINDOW = "20230718_20231015"


def _info(key: str, first: str, last: str, **extra: str) -> RasterInfo:
    """Build a raster info for one asset key carrying every required tag, with the given span.

    The provenance every raster of the key shares comes from the vocabulary,
    as the build checks it against.
    """
    tags = {
        "PIPELINE_SOFTWARE": "s1bursts",
        "PIPELINE_SOFTWARE_VERSION": "0.5.0",
        "PROCESSING_SOFTWARE": "ESA SNAP",
        "PROCESSING_SOFTWARE_VERSION": "14.0.0",
        "DEM": "Copernicus GLO-30",
        "PRODUCT_LEVEL": "L3",
        "MGRS_TILE": TILE,
        "COMPOSITE_START_DATE": "2023-07-18",
        "COMPOSITE_END_DATE": "2023-10-15",
        "FIRST_ACQUISITION_DATE": first,
        "LAST_ACQUISITION_DATE": last,
        "POLARIZATION": "VV",
        "CONTRIBUTING_OBSERVATIONS": "40",
        "LAYER_NAME": "x",
        "LAYER_DESCRIPTION": "x",
        **vocabulary().provenance_tags(key),
        **extra,
    }
    return RasterInfo(tags=tags, band_name="x", data_type="float32", nodata=float("nan"))


def _refuses_mixed_windows(files: Sequence[CompositeFile]) -> bool:
    """Check that a stale file from a different window makes the tile fail."""
    info = {
        "coh12_vv": _info("coh12_vv", "2023-07-21", "2023-09-30"),
        "rtc_vv": _info("rtc_vv", "2023-07-21", "2023-10-12", COMPOSITE_END_DATE="2023-10-20"),
    }
    try:
        build_tile_item(TILE, files, info)
    except ValueError as error:
        return "2023-10-20" in str(error)
    return False


def _refuses_other_provenance(files: Sequence[CompositeFile]) -> bool:
    """Check that a raster whose shared tags differ from the vocabulary makes the tile fail."""
    info = {
        "coh12_vv": _info("coh12_vv", "2023-07-21", "2023-09-30", COHERENCE_WINDOW_RANGE="20"),
        "rtc_vv": _info("rtc_vv", "2023-07-21", "2023-10-12"),
    }
    try:
        build_tile_item(TILE, files, info)
    except ValueError as error:
        return "glace:coherence_window_range" in str(error)
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


def _staging_checks() -> dict[str, bool]:
    """Check the staging directories and the scratch estimate of the year script."""
    build_year = _build_year()
    with tempfile.TemporaryDirectory() as tmp:
        args = SimpleNamespace(cache=Path(tmp), year=2023)
        killed = Path(tmp) / "glace-store" / "2023" / "pmtiles-killed"
        killed.mkdir(parents=True)
        (killed / "coh12_vv_viz.pmtiles").write_bytes(b"partial")
        with build_year.staging(args, "pmtiles") as staged:
            leftover_cleared = not killed.exists()
            empty = not any(staged.iterdir())
            (staged / "coh12_vv_viz.pmtiles").write_bytes(b"built")
        with build_year.staging(args, "pmtiles") as again:
            fresh = again != staged and not any(again.iterdir())
        removed = not any((Path(tmp) / "glace-store" / "2023").iterdir())
        too_little = False
        try:
            build_year.require_scratch_space(Path(tmp), 1 << 60)
        except SystemExit as error:
            too_little = "FREEZEBASE_CACHE" in str(error)
    alps = get_scope("alps")
    every = SimpleNamespace(layers=None, concurrent_layers=12)
    one = SimpleNamespace(layers=["coh12_vv"], concurrent_layers=12)
    pmtiles = build_year.scratch_needed(every, alps, ["pmtiles"])
    return {
        "a step stages into an empty directory": empty,
        "a killed run's staging is cleared": leftover_cleared,
        "a rerun stages into a new directory": fresh,
        "staging is removed after the step": removed,
        "too little scratch space stops the build": too_little,
        "the Alpine web maps need 15 to 40 GB of scratch": 15e9 < pmtiles < 40e9,
        "one layer needs less scratch than twelve": (
            build_year.scratch_needed(one, alps, ["pmtiles"]) < pmtiles
        ),
        "an up-to-date year needs no scratch": build_year.scratch_needed(every, alps, []) == 0,
    }


def _footprint_checks() -> dict[str, bool]:
    """Check the mosaic footprint of a block of tiles that spans two UTM zones."""
    # Alpine perimeter tiles on both sides of 6°E, where zones 31 and 32 overlap.
    tiles = [f"31TGK{e}{n}" for e in "123" for n in "012"] + ["32TKQ60", "32TKQ61"]
    files = [
        file
        for tile in tiles
        if (file := parse_composite_name(Path(f"/a/S1_RTC_LRW_{tile}_VV_{WINDOW}.tif")))
    ]
    geometry, bbox = data_footprint(files)
    footprint = shape(geometry)
    return {
        "the footprint fixture parsed": len(files) == len(tiles),
        "the footprint is valid": footprint.is_valid,
        "the footprint covers every tile": all(
            footprint.covers(MGRSGeoBox.from_mgrs(tile).geographic_extent.geom) for tile in tiles
        ),
        "the footprint is simplified": int(get_num_coordinates(footprint)) < 10 * len(tiles),
        "the mosaic bbox is the footprint's": bbox == list(footprint.bounds),
    }


def _mirror_checks(tile_item: pystac.Item) -> dict[str, bool]:
    """Write both mirrors as the build and finalization do; check their columns and links."""
    base = "https://example.org/glace"
    mosaic = _mosaic_item()
    mosaic.properties["glace:build_fingerprint"] = "0" * 64
    mosaic.properties["description"] = vocabulary().mosaic_item_description(2023)
    # Finalization reads the mosaic items back from the store, as written.
    stored = pystac.Item.from_dict(item_document(mosaic))
    with tempfile.TemporaryDirectory() as tmp:
        year, tiles = Path(tmp) / ".items.parquet", Path(tmp) / "tiles.parquet"
        mosaics = Path(tmp) / "mosaics.parquet"
        row = year_index_document(tile_item)
        write_stac_geoparquet([row], year)
        concat_indexes([year], tiles, base_url=base)
        index_from_items([stored], mosaics, base_url=base)
        tables = {"tiles": pq.read_table(tiles), "mosaics": pq.read_table(mosaics)}
    checks = {
        f"the {name} mirror holds the documented columns": set(table.column_names)
        == {column["name"] for column in vocabulary().mirror_columns(name)}
        for name, table in tables.items()
    }
    for name, table in tables.items():
        links = {link["rel"]: link["href"] for link in table.column("links").to_pylist()[0]}
        checks[f"the {name} mirror links the root absolutely"] = (
            links.get("root") == f"{base}/catalog.json"
        )
        checks[f"the {name} mirror names the collection"] = (
            table.column("collection").to_pylist()[0].endswith(name)
        )
    tile_links = {
        link["rel"]: link["href"] for link in tables["tiles"].column("links").to_pylist()[0]
    }
    checks["a tile row's parent is its year catalog"] = (
        tile_links.get("parent") == f"{base}/tiles/2023/catalog.json"
    )
    asset = tables["tiles"].column("assets").to_pylist()[0]["coh12_vv"]["href"]
    checks["a tile row's asset href is absolute"] = (
        asset == f"{base}/tiles/2023/{TILE}/coh12_vv.tif"
    )
    return checks


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
        missing = set(required_tags(file)) - set(_info(file.asset_key, "x", "x").tags)
        if missing:
            print(f"error  fixture lacks required tags {sorted(missing)}")
            return 1
    info = {
        "coh12_vv": _info("coh12_vv", "2023-07-21", "2023-09-30"),  # a pair ends earlier
        "rtc_vv": _info("rtc_vv", "2023-07-21", "2023-10-12"),
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
        "the tile names its MGRS square in the grid extension": props.get("grid:code")
        == f"MGRS-{TILE}",
        "no filename-derived span fields": "glace:coh_start" not in props
        and "glace:rtc_end" not in props,
        "the asset carries its own observation count": item.assets["coh12_vv"].extra_fields.get(
            "glace:contributing_observations",
        )
        == 40,
        "the asset leaves the shared provenance to item_assets": not any(
            name in item.assets["coh12_vv"].extra_fields
            for name in vocabulary().provenance("coh12_vv")
        ),
        "the asset leaves the dates to the item": not any(
            "date" in name for name in item.assets["coh12_vv"].extra_fields
        ),
        "the item records its software once": props.get("processing:software")
        == {"ESA SNAP": "14.0.0", "s1bursts": "0.5.0"}
        and "processing:software" not in item.assets["rtc_vv"].extra_fields,
    }
    checks["a file from another window refuses the tile"] = _refuses_mixed_windows(files)
    checks["a file stating other provenance refuses the tile"] = _refuses_other_provenance(files)
    # The archives are reached through `visual` assets alone, a recorded
    # deviation from `PORTO-FMT-011` (docs/conformance.md). Check this on an
    # item that carries the renderings.
    built = _build_year().visual_assets(_pmtiles_state())
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
    checks.update(
        _mirror_checks(
            build_tile_item(
                TILE, files, info, glacier_fraction=0.1, glacier_fraction_buffered=0.2
            ),
        ),
    )
    checks.update(_staging_checks())
    checks.update(_footprint_checks())
    # A build state recorded before the transport range changed describes an
    # archive encoded with the old range, so publishing it must fail.
    stale = _pmtiles_state()
    stale.details["pmtiles"]["coh12_vv_viz"]["scale"] = 0.005
    try:
        _build_year().visual_assets(stale)
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
