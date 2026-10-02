#!/usr/bin/env python3
"""Checks ``metadata/`` and that the tracked ``catalog/`` matches it.

The first half checks the asset vocabulary in ``metadata/assets.yaml``: it
names exactly the twelve published asset keys and their twelve renderings,
every title is distinct, every QA asset has the ``cloud-optimized`` role
beside ``quality`` so STAC browsers badge it, and every decibel layer states
its conversion in the render definition.

The second half checks that ``catalog/`` is up to date. ``catalog/`` is
generated, and committed so it can be reviewed, browsed and archived with each
release; the next render overwrites any hand edit there. This renders
``metadata/`` into a temporary directory and compares it byte for byte with the
tracked tree, naming each file that differs.

Reads no data and needs no network.

Run: uv run tests/test_render.py
"""

from __future__ import annotations

from pathlib import Path
import sys

from glace_catalog.documents import stale_documents
from glace_catalog.metadata import VIZ_KEYS, load_vocabulary
from glace_catalog.stac.naming import ASSET_KEYS

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog"

problems: list[str] = []

# --- the asset vocabulary --------------------------------------------------
vocabulary = load_vocabulary()

expected_viz = tuple(f"{key}_viz" for key in ASSET_KEYS)
if sorted(VIZ_KEYS) != sorted(expected_viz):
    problems.append(f"VIZ_KEYS is {sorted(VIZ_KEYS)}, expected {sorted(expected_viz)}")

titles: dict[str, str] = {}
for key in (*ASSET_KEYS, *VIZ_KEYS):
    try:
        titles[key] = vocabulary.asset_title(key)
    except KeyError as error:
        problems.append(f"{key}: no title ({error})")
duplicates = {title for title in titles.values() if list(titles.values()).count(title) > 1}
problems += [f"title {title!r} is used by more than one asset" for title in sorted(duplicates)]

for key in ASSET_KEYS:
    for collection in ("tiles", "mosaics"):
        asset = vocabulary.item_asset(key, collection)
        roles = asset["roles"]
        if "_qa_" in key and roles != ["quality", "cloud-optimized"]:
            problems.append(
                f"{collection}/{key}: roles {roles}, expected quality + cloud-optimized"
            )
        if "_qa_" not in key and roles != ["data", "cloud-optimized"]:
            problems.append(f"{collection}/{key}: roles {roles}, expected data + cloud-optimized")
        if "proj:code" in asset:
            problems.append(f"{collection}/{key}: item_assets must not carry proj:code")
        if not asset.get("description"):
            problems.append(f"{collection}/{key}: no description")
        if "polaris" in asset.get("description", "") or "colour" in asset.get("description", ""):
            problems.append(f"{collection}/{key}: British spelling in the description")
    render = vocabulary.render(key, "tiles")
    display = vocabulary.display(key)
    if render["rescale"] != [[display.vmin, display.vmax]]:
        problems.append(f"{key}: render rescale {render['rescale']} != display range")
    if display.decibel and render.get("expression") != f"10*log10({key}_b1)":
        problems.append(f"{key}: decibel layer without its expression in the render")
    if not display.decibel and "expression" in render:
        problems.append(f"{key}: linear layer carries an expression")

if vocabulary.asset_title("coh12_vv") != "Coherence VV":
    problems.append(f"coh12_vv titled {vocabulary.asset_title('coh12_vv')!r}")
if vocabulary.asset_title("rtc_vh_qa_num") != "Backscatter VH – Contributing Observations":
    problems.append(f"rtc_vh_qa_num titled {vocabulary.asset_title('rtc_vh_qa_num')!r}")
if (
    vocabulary.asset_title("coh12_vv_qa_cqm_viz")
    != "Coherence VV – Composite Quality Map – PMTiles"
):
    problems.append(
        f"coh12_vv_qa_cqm_viz titled {vocabulary.asset_title('coh12_vv_qa_cqm_viz')!r}"
    )
for key in ASSET_KEYS:
    for collection in ("tiles", "mosaics"):
        if vocabulary.item_asset(key, collection)["bands"][0]["name"] != key:
            problems.append(f"{collection}/{key}: the band is not named by its asset key")
coh12_vv = vocabulary.display("coh12_vv")
if coh12_vv.vmax != 0.75:
    problems.append("coh12_vv default vmax is not 0.75")

# --- catalog/ matches a fresh render ---------------------------------------
stale = stale_documents(vocabulary, CATALOG)
problems += stale
if stale:
    problems.append("regenerate with: uv run scripts/render-catalog.py")

if problems:
    for problem in problems:
        print(f"error  {problem}")
    print(f"FAILED: {len(problems)} problem(s)")
    sys.exit(1)

print(f"OK: {len(ASSET_KEYS)} assets, {len(VIZ_KEYS)} renderings, catalog/ matches its source")
