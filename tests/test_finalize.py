#!/usr/bin/env python3
"""Gate on what ``finalize-catalog`` writes over the published years.

Three things only this script does, and each one can go wrong quietly:

- the published styles, one per year written from the one tracked template, so
  every year's archives have a style that names their layers;
- the style registrations on the collection, one per year with the latest the
  default;
- the coverage check between the store's archives and the styles, which is what
  makes an archive no style draws a failed run rather than a dead legend.

Runs the script's own functions against a temporary store of empty files. Reads
no data, writes nothing outside the temporary directory and needs no network.

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

from glace_catalog import stac

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "catalog"


def _finalizer() -> ModuleType:
    """Load ``scripts/finalize-catalog.py`` as a module.

    The script resolves its store root at import; a local value keeps this gate
    off object storage, and nothing here reads or writes it.
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
) -> dict[str, Path]:
    """Check the published styles: one per year, each pointing at its own archives."""
    written = fin.repoint_styles(source, staged, [2023, 2024])
    checks["one published style per year"] = sorted(written) == ["style-2023", "style-2024"]
    style_2023 = json.loads(written["style-2023"].read_text())
    checks["the 2023 style points at 2023 archives"] = all(
        "/2023/" in source_entry["url"] and source_entry["url"].endswith("_viz.pmtiles")
        for source_entry in style_2023["sources"].values()
    )
    checks["the 2023 style's layer ids end in 2023"] = all(
        layer["id"].endswith("-2023") for layer in style_2023["layers"]
    )
    checks["the style name carries its year"] = "(2023)" in style_2023["name"]
    checks["the credit line carries its year"] = all(
        "2023" in source_entry["attribution"] for source_entry in style_2023["sources"].values()
    )
    return written


def check_registration(
    checks: dict[str, bool],
    fin: ModuleType,
    document: dict[str, Any],
) -> None:
    """Check the collection's style assets: one per year, the latest the default."""
    fin.register_style_assets(document, [2023, 2024], latest=2024)
    assets = document["assets"]
    styles = {key: value for key, value in assets.items() if "style" in value["roles"]}
    checks["the collection registers one style per year"] = sorted(styles) == [
        "style-2023",
        "style-2024",
    ]
    checks["only the latest year is the default"] = (
        "default" in styles["style-2024"]["roles"]
        and "default" not in styles["style-2023"]["roles"]
    )
    checks["the tracked template entry is gone from the published document"] = (
        "style-default" not in assets
    )
    checks["the href is the year's file under styles/"] = (
        styles["style-2023"]["href"] == "./styles/2023.json"
    )
    # The tracked entry describes the template; a published entry describes one
    # year's style, so its prose is written here rather than copied.
    checks["a published style does not describe itself as the template"] = (
        "template" not in styles["style-2023"]["description"]
    )


def check_coverage(
    checks: dict[str, bool],
    fin: ModuleType,
    store: Path,
    written: dict[str, Path],
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
        checks["a year the store holds no archives of passes"] = True
    except SystemExit:
        checks["a year the store holds no archives of passes"] = False
    try:
        fin.check_style_coverage(layout, [2024], {"style-2023": written["style-2023"]})
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
        check_registration(checks, fin, document)
        check_coverage(checks, fin, store, written)
        check_skip_thumbnail(checks, fin, store)

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
