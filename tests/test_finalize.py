#!/usr/bin/env python3
"""Checks what ``finalize-catalog`` writes for the published years.

Six steps belong to this script alone, and a mistake in any of them is easy
to miss:

- it writes one style per year from the tracked template, beside the year's
  archives, so each year has a style that names their layers;
- it registers each year's style on that year's mosaic item, and gives the
  collection the latest year's copy;
- it checks that every archive in the store appears in its year's style, so the
  run fails on an archive that no style draws;
- it refuses to finalize archives encoded over a range other than the
  metadata's, since every year style decodes with the current ranges;
- it refuses to start from a ``catalog/`` that differs from a fresh render of
  ``metadata/``, since the styles it copies must decode the archives with the
  ranges the build used;
- it bounds the tiles collection by its tiles, as the tile grid archive's
  header does, rather than by the wider mosaic grid it is rendered with.

Runs the script's own functions against a temporary store of empty files.
Writes only into the temporary directory, reads no data and needs no network.

Run: uv run tests/test_finalize.py
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

from glace_catalog import stac
from glace_catalog.metadata import vocabulary

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "catalog"
TILE_COLUMNS = [column["name"] for column in vocabulary().mirror_columns("tiles")]


def _finalizer() -> ModuleType:
    """Load ``scripts/finalize-catalog.py`` as a module.

    The script resolves its store root on import, so the gate first points it
    at a local placeholder path.
    """
    os.environ.setdefault("GLACE_STORE_ROOT", str(Path(__file__).parent / "does-not-exist"))
    spec = importlib.util.spec_from_file_location(
        "finalize_catalog",
        ROOT / "scripts" / "finalize-catalog.py",
    )
    if spec is None or spec.loader is None:
        msg = "scripts/finalize-catalog.py did not load"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _store(root: Path) -> Path:
    """Two years of archives, with a COG beside one of them to be ignored."""
    for year in (2023, 2024):
        directory = root / "mosaics" / str(year)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "coh12_vv_viz.pmtiles").write_bytes(b"")
    (root / "mosaics" / "2024" / "coh12_vv.tif").write_bytes(b"")
    return root


def _source(root: Path) -> Path:
    """Copy the tracked style and mosaics collection, as the script reads them."""
    styles = root / "mosaics" / "styles"
    styles.mkdir(parents=True, exist_ok=True)
    shutil.copy(SOURCE / "mosaics" / "styles" / "default.json", styles / "default.json")
    shutil.copy(SOURCE / "mosaics" / "collection.json", root / "mosaics" / "collection.json")
    return root


def check_styles(
    checks: dict[str, bool],
    fin: ModuleType,
    source: Path,
    staged: Path,
) -> dict[int, dict[str, Path]]:
    """Check the published styles: one per year beside its archives, and the collection's."""
    written = fin.stage_item_styles(source, staged, [2023, 2024])
    beside = staged / "mosaics" / "2023" / "styles" / "default.json"
    checks["one style per year, beside its archives"] = sorted(written) == [2023, 2024] and (
        written[2023] == {"style-default": beside}
    )
    style_2023 = json.loads(written[2023]["style-default"].read_text())
    checks["the 2023 style points at the archives beside it"] = all(
        source_entry["url"].startswith("pmtiles://../")
        and source_entry["url"].count("/") == 3
        and source_entry["url"].endswith("_viz.pmtiles")
        for source_entry in style_2023["sources"].values()
    )
    checks["the 2023 style's layer ids end in 2023"] = all(
        layer["id"].endswith("-2023") for layer in style_2023["layers"]
    )
    checks["the style name carries its year"] = "(2023)" in style_2023["name"]
    checks["the credit line carries its year"] = all(
        "2023" in source_entry["attribution"] for source_entry in style_2023["sources"].values()
    )

    collection = fin.stage_collection_styles(source, staged, 2024)
    checks["the collection's style keeps its tracked path"] = collection == {
        "style-default": staged / "mosaics" / "styles" / "default.json"
    }
    latest = json.loads(collection["style-default"].read_text())
    checks["the collection's style draws the latest year's archives"] = all(
        source_entry["url"].startswith("pmtiles://../2024/")
        for source_entry in latest["sources"].values()
    ) and all(layer["id"].endswith("-2024") for layer in latest["layers"])
    return written


def check_registration(
    checks: dict[str, bool],
    fin: ModuleType,
    written: dict[int, dict[str, Path]],
    collection: dict[str, Any],
) -> None:
    """Check a year's style asset on its mosaic item, and the collection's one style."""
    item: dict[str, Any] = {"assets": {"coh12_vv": {"href": "./coh12_vv.tif"}}}
    fin.register_item_styles(item, 2023, written[2023], collection["item_assets"])
    asset = item["assets"].get("style-default", {})
    checks["the item registers its year's style beside it"] = (
        asset.get("href") == "./styles/default.json" and "coh12_vv" in item["assets"]
    )
    checks["the item's style is its default"] = asset.get("roles") == ["style", "default"]
    checks["the item's style records its size and checksum"] = asset.get("file:size") == len(
        written[2023]["style-default"].read_bytes()
    ) and str(asset.get("file:checksum", "")).startswith("1220")
    checks["the item's style title carries its year"] = str(asset.get("title", "")).endswith(
        "– 2023"
    )
    # The item's style describes its own year, not the collection's latest.
    checks["the item's style has the year's wording"] = "2023" in asset.get(
        "description", ""
    ) and "latest" not in asset.get("description", "")
    styles = [key for key, value in collection["assets"].items() if "style" in value["roles"]]
    checks["the collection keeps one style, the default"] = styles == ["style-default"] and (
        "default" in collection["assets"]["style-default"]["roles"]
    )


def check_item_styles_published(checks: dict[str, bool], fin: ModuleType, work: Path) -> None:
    """Check that each year's style reaches the store and its mosaic item."""
    store = _store(work / "item-style-store")
    layout = stac.StoreLayout(store)
    item = Path(str(layout.mosaic_item(2023)))
    item.write_text(json.dumps({"type": "Feature", "id": "glace-mosaic-2023", "assets": {}}))
    collection = json.loads((SOURCE / "mosaics" / "collection.json").read_text())
    fin.publish_item_styles(SOURCE, layout, [2023, 2024], collection["item_assets"])
    checks["each year's style is written beside its archives"] = all(
        (store / "mosaics" / str(year) / "styles" / "default.json").is_file()
        for year in (2023, 2024)
    )
    assets = json.loads(item.read_text())["assets"]
    checks["the mosaic item in the store lists its style"] = (
        assets.get("style-default", {}).get("href") == "./styles/default.json"
    )


def check_coverage(
    checks: dict[str, bool],
    fin: ModuleType,
    store: Path,
    written: dict[int, dict[str, Path]],
) -> None:
    """Check that an archive no published style draws stops the run."""
    layout = stac.StoreLayout(store)
    try:
        fin.check_style_coverage(layout, [2023, 2024], written)
        checks["every archive is in its year's style, and the COG is ignored"] = True
    except SystemExit:
        checks["every archive is in its year's style, and the COG is ignored"] = False
    try:
        fin.check_style_coverage(layout, [2022], written)
        checks["a year with no archives in the store passes"] = True
    except SystemExit:
        checks["a year with no archives in the store passes"] = False
    try:
        fin.check_style_coverage(layout, [2024], {2023: written[2023]})
        checks["an archive whose year has no style is refused"] = False
    except SystemExit:
        checks["an archive whose year has no style is refused"] = True
    stray = store / "mosaics" / "2024" / "nonesuch_vv_viz.pmtiles"
    stray.write_bytes(b"")
    try:
        fin.check_style_coverage(layout, [2023, 2024], written)
        checks["an archive no style draws is refused"] = False
    except SystemExit as error:
        checks["an archive no style draws is refused"] = "glace-nonesuch_vv-2024" in str(error)
    finally:
        stray.unlink()


def check_skip_thumbnail(checks: dict[str, bool], fin: ModuleType, store: Path) -> None:
    """Check that skipping the thumbnails needs thumbnails already in the store."""
    layout = stac.StoreLayout(store)
    try:
        fin.require_existing_thumbnails(layout)
        checks["--skip-thumbnail refuses a store with no thumbnail"] = False
    except FileNotFoundError as error:
        checks["--skip-thumbnail refuses a store with no thumbnail"] = "--skip-thumbnail" in str(
            error
        )
    for path in (layout.tiles_thumbnail, layout.mosaics_thumbnail):
        Path(str(path)).parent.mkdir(parents=True, exist_ok=True)
        Path(str(path)).write_bytes(b"")
    try:
        fin.require_existing_thumbnails(layout)
        checks["--skip-thumbnail accepts thumbnails already written"] = True
    except FileNotFoundError:
        checks["--skip-thumbnail accepts thumbnails already written"] = False


def check_encodings(checks: dict[str, bool], fin: ModuleType, store: Path) -> None:
    """Check that an archive encoded over an old range stops finalization."""
    layout = stac.StoreLayout(store)
    for year in (2023, 2024):
        encoding = vocabulary().display("coh12_vv").encoding
        state = stac.BuildState(path=Path(str(layout.build_state(year))))
        Path(str(state.path)).parent.mkdir(parents=True, exist_ok=True)
        state.record(
            "pmtiles",
            "print",
            {"coh12_vv_viz": {"scale": encoding.scale, "offset": encoding.offset}},
        )
    try:
        fin.require_current_encodings(layout, [2023, 2024])
        checks["archives over the current ranges are accepted"] = True
    except SystemExit:
        checks["archives over the current ranges are accepted"] = False
    stale = stac.BuildState.load(layout.build_state(2023))
    stale.details["pmtiles"]["coh12_vv_viz"]["scale"] = 0.005
    stale.save()
    try:
        fin.require_current_encodings(layout, [2023, 2024])
        checks["an archive over an old range stops finalization"] = False
    except SystemExit as error:
        checks["an archive over an old range stops finalization"] = "2023 coh12_vv_viz" in str(
            error
        )


def check_render(checks: dict[str, bool], fin: ModuleType, work: Path) -> None:
    """Check that finalization accepts the tracked render and refuses a stale one."""
    rendered = work / "rendered"
    shutil.copytree(SOURCE, rendered)
    try:
        fin.require_current_render(rendered)
        checks["the tracked render is accepted"] = True
    except SystemExit:
        checks["the tracked render is accepted"] = False
    style = rendered / "mosaics" / "styles" / "default.json"
    style.write_text(style.read_text().replace('"color-relief"', '"hillshade"', 1))
    try:
        fin.require_current_render(rendered)
        checks["a stale render stops finalization"] = False
    except SystemExit as error:
        checks["a stale render stops finalization"] = "default.json" in str(error)


def check_no_published_fractions(checks: dict[str, bool], fin: ModuleType, work: Path) -> None:
    """Check that a fraction table without the leading dot stops finalization."""
    layout = stac.StoreLayout(work / "fractions-store")
    tiles = Path(str(layout.tiles))
    tiles.mkdir(parents=True)
    (tiles / ".glacier-fractions.parquet").write_bytes(b"")
    try:
        fin.require_no_published_fractions(layout)
        checks["the dot-prefixed fraction table is accepted"] = True
    except SystemExit:
        checks["the dot-prefixed fraction table is accepted"] = False
    (tiles / "glacier-fractions.parquet").write_bytes(b"")
    try:
        fin.require_no_published_fractions(layout)
        checks["a fraction table publishing would upload stops finalization"] = False
    except SystemExit as error:
        checks["a fraction table publishing would upload stops finalization"] = (
            ".glacier-fractions.parquet" in str(error)
        )


def check_grid_stamping(checks: dict[str, bool], fin: ModuleType, store: Path) -> None:
    """Check that the grid asset records the size of the archive in the store."""
    layout = stac.StoreLayout(store)
    grid = Path(str(layout.tiles_grid))
    grid.parent.mkdir(parents=True, exist_ok=True)
    grid.write_bytes(b"PMTiles" * 10)
    Path(str(layout.tiles_index)).write_bytes(b"")
    documents = {"tiles": json.loads((SOURCE / "tiles" / "collection.json").read_text())}
    fin.stamp_collection_assets(documents, layout)
    asset = documents["tiles"]["assets"]["grid"]
    checks["the grid asset records its size"] = asset.get("file:size") == 70
    checks["the grid asset records its checksum"] = str(asset.get("file:checksum", "")).startswith(
        "1220"
    )


def check_mirror_description(checks: dict[str, bool], fin: ModuleType, store: Path) -> None:
    """Check that a mirror's types and count are recorded, and undocumented columns refused."""
    layout = stac.StoreLayout(store)
    index = Path(str(layout.mosaics_index))
    index.parent.mkdir(parents=True, exist_ok=True)
    collection = json.loads((SOURCE / "mosaics" / "collection.json").read_text())
    names = [column["name"] for column in collection["assets"]["items"]["table:columns"]]
    pq.write_table(pa.Table.from_pylist([dict.fromkeys(names, "x")] * 3), index)
    documents = {"mosaics": collection}
    fin.describe_mirrors(documents, layout)
    asset = collection["assets"]["items"]
    checks["a mirror records its row count"] = asset.get("table:row_count") == 3
    checks["a mirror records each column's type"] = all(
        column.get("type") == "string" for column in asset["table:columns"]
    )
    pq.write_table(pa.Table.from_pylist([dict.fromkeys([*names, "stray"], "x")]), index)
    try:
        fin.describe_mirrors(
            {"mosaics": json.loads((SOURCE / "mosaics" / "collection.json").read_text())}, layout
        )
        checks["an undocumented mirror column stops finalization"] = False
    except SystemExit as error:
        checks["an undocumented mirror column stops finalization"] = "stray" in str(error)
    index.unlink()


def check_grid_style_staged(checks: dict[str, bool], fin: ModuleType, work: Path) -> None:
    """Check that the grid style is published beside the tiles collection, stamped."""
    source = work / "full-source"
    shutil.copytree(SOURCE, source)
    store = _store(work / "styled-store")
    documents = {
        directory: json.loads((source / directory / "collection.json").read_text())
        for directory in ("tiles", "mosaics")
    }
    fin.write_documents(
        source,
        stac.StoreLayout(store),
        documents,
        base_url="https://example.org/glace",
        years=[2023, 2024],
        updated="2026-10-17T12:00:00Z",
    )
    published = store / "tiles" / "styles" / "grid.json"
    checks["the grid style is published"] = published.is_file()
    collection = json.loads((store / "tiles" / "collection.json").read_text())
    asset = collection["assets"].get("style-grid", {})
    checks["the grid style records its size"] = published.is_file() and asset.get(
        "file:size"
    ) == len(published.read_bytes())
    default = store / "tiles" / "styles" / "default.json"
    tiles_style: dict[str, Any] = json.loads(default.read_text()) if default.is_file() else {}
    sources: dict[str, Any] = tiles_style.get("sources", {})
    checks["the tiles style draws the latest mosaic year"] = sources.get("src-coh12_vv", {}).get(
        "url"
    ) == "pmtiles://../../mosaics/2024/coh12_vv_viz.pmtiles" and "(2024)" in tiles_style.get(
        "name", ""
    )
    checks["the tiles style keeps the grid source"] = (
        sources.get("grid", {}).get("url") == "pmtiles://../grid.pmtiles"
    )
    checks["the tiles style records its checksum"] = default.is_file() and collection[
        "assets"
    ].get("style-default", {}).get("file:checksum", "").startswith("1220")
    checks["the grid style is published unchanged"] = (
        published.read_bytes() == (source / "tiles" / "styles" / "grid.json").read_bytes()
    )
    root = json.loads((store / "catalog.json").read_text())
    checks["the root and the collections carry the run's time"] = all(
        document.get("updated") == "2026-10-17T12:00:00Z"
        for document in (
            root,
            collection,
            json.loads((store / "mosaics" / "collection.json").read_text()),
        )
    )


def check_stale_year_index(checks: dict[str, bool], fin: ModuleType) -> None:
    """Check that a year indexed by an older build stops finalization."""
    current = pa.Table.from_pylist([dict.fromkeys(TILE_COLUMNS, "x")])
    fin.require_current_year_indexes([current], [2024])
    checks["a year index with the documented columns is accepted"] = True
    older = current.drop_columns(["collection"])
    try:
        fin.require_current_year_indexes([current, older], [2023, 2024])
        checks["a year index without a documented column stops finalization"] = False
    except SystemExit as error:
        checks["a year index without a documented column stops finalization"] = (
            "2024: missing ['collection']" in str(error)
        )


def check_tiles_extent(checks: dict[str, bool], fin: ModuleType, work: Path) -> None:
    """Check that the tiles collection is bounded by its tiles, rounded outward."""
    grid = gpd.GeoDataFrame(
        geometry=[
            shapely.box(8.00004, 46.40006, 8.13001, 46.49),
            shapely.box(8.1, 46.3, 8.12, 46.4),
        ],
        crs="EPSG:4326",
    )
    bbox = fin.tiles_bbox(grid)
    checks["the tiles bbox is rounded outward"] = bbox == [8.0, 46.3, 8.1301, 46.49]
    source = work / "extent-source"
    (source / "tiles").mkdir(parents=True)
    shutil.copy(SOURCE / "tiles" / "collection.json", source / "tiles" / "collection.json")
    shutil.copytree(SOURCE / "mosaics", source / "mosaics")
    documents = fin.patch_collections(source, [2024], [], bbox)
    checks["the tiles collection takes the tiles bbox"] = documents["tiles"]["extent"]["spatial"][
        "bbox"
    ] == [bbox]
    rendered = json.loads((SOURCE / "mosaics" / "collection.json").read_text())
    checks["the mosaics collection keeps the mosaic grid bbox"] = (
        documents["mosaics"]["extent"]["spatial"] == rendered["extent"]["spatial"]
    )


def check_grid_conflict_before_writes(
    checks: dict[str, bool], fin: ModuleType, work: Path
) -> None:
    """Check that years disagreeing on a tile stop finalization before it writes anything."""
    store = _store(work / "conflict-store")
    layout = stac.StoreLayout(store)
    polygon = shapely.to_wkb(shapely.box(8.0, 46.4, 8.13, 46.49))
    for year, fraction in ((2023, 0.1), (2024, 0.2)):
        index = Path(str(layout.year_index(year)))
        index.parent.mkdir(parents=True, exist_ok=True)
        # Every documented column, so the run reaches the grid check.
        row = {
            **dict.fromkeys(TILE_COLUMNS, "x"),
            "geometry": polygon,
            "glace:mgrs_tile": "32TMS55",
            "glace:glacier_fraction": fraction,
            "glace:glacier_fraction_buffered": 0.3,
            "version": "1.0",
        }
        pq.write_table(pa.Table.from_pylist([row]), index)
        encoding = vocabulary().display("coh12_vv").encoding
        state = stac.BuildState(path=Path(str(layout.build_state(year))))
        state.record(
            "pmtiles",
            "print",
            {"coh12_vv_viz": {"scale": encoding.scale, "offset": encoding.offset}},
        )
    settings, argv = getattr(fin, "settings"), sys.argv  # noqa: B009  (ModuleType has no typed attributes)
    setattr(fin, "settings", type("Settings", (), {"store_root": store})())  # noqa: B010
    sys.argv = ["finalize-catalog.py", "--skip-thumbnail", "--skip-validate"]
    try:
        fin.main()
        stopped = ""
    except SystemExit as error:
        stopped = str(error)
    except Exception as error:  # noqa: BLE001  (any other failure means the order is wrong)
        stopped = f"unexpected {type(error).__name__}"
    finally:
        setattr(fin, "settings", settings)  # noqa: B010
        sys.argv = argv
    checks["a grid conflict stops finalization"] = "32TMS55" in stopped
    checks["a grid conflict stops finalization before any write"] = not Path(
        str(layout.tiles_index),
    ).exists()


def main() -> int:
    """Run every check; print the failures."""
    fin = _finalizer()
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        store = _store(work / "store")
        source = _source(work / "source")
        staged = work / "staged"

        written = check_styles(checks, fin, source, staged)
        document = json.loads((source / "mosaics" / "collection.json").read_text())
        check_registration(checks, fin, written, document)
        check_item_styles_published(checks, fin, work)
        check_coverage(checks, fin, store, written)
        check_skip_thumbnail(checks, fin, store)
        check_render(checks, fin, work)
        check_encodings(checks, fin, store)
        check_no_published_fractions(checks, fin, work)
        check_grid_stamping(checks, fin, store)
        check_mirror_description(checks, fin, store)
        check_grid_style_staged(checks, fin, work)
        check_stale_year_index(checks, fin)
        check_tiles_extent(checks, fin, work)
        check_grid_conflict_before_writes(checks, fin, work)

    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        print(f"FAILED: {len(failed)} of {len(checks)} check(s)")
        return 1
    print(f"OK: {len(checks)} finalize check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
