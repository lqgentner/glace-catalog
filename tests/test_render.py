#!/usr/bin/env python3
"""Gate on the one source of the catalog's names, prose and numbers.

Two halves. The first holds the vocabulary in ``metadata/assets.yaml`` to what
every consumer of it assumes: it names exactly the twelve published asset keys
and the fourteen renderings, every title is distinct, every QA asset carries the
``cloud-optimized`` role beside ``quality`` so a browser badges it, and every
decibel layer declares its conversion in the render definition.

The second is the regeneration gate. ``catalog/`` is generated output that is
committed so that the tree is reviewable, browsable and archived with every
release; a hand edit there is overwritten by the next render and would drift
from the source it claims to reflect. So this renders ``metadata/`` into a
temporary directory and compares it, byte for byte, with the tracked tree. A
difference names the file and says which way to fix it.

Reads no data and needs no network.

Run: uv run tests/test_render.py
"""

from __future__ import annotations

import filecmp
from pathlib import Path
import sys
import tempfile

from glace_catalog.documents import render_tree
from glace_catalog.metadata import VIZ_KEYS, load_vocabulary
from glace_catalog.stac.naming import ASSET_KEYS
from glace_catalog.styles import LayerStyle, RgbStyle

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog"

problems: list[str] = []

# --- the vocabulary --------------------------------------------------------
vocabulary = load_vocabulary()

expected_viz = (*[f"{key}_viz" for key in ASSET_KEYS], "coh12_rgb_viz", "rtc_rgb_viz")
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
    if not isinstance(display, LayerStyle):
        problems.append(f"{key}: display is not a single-band style")
        continue
    if render["rescale"] != [[display.vmin, display.vmax]]:
        problems.append(f"{key}: render rescale {render['rescale']} != display range")
    if display.decibel and render.get("expression") != f"10*log10({key}_b1)":
        problems.append(f"{key}: decibel layer without its expression in the render")
    if not display.decibel and "expression" in render:
        problems.append(f"{key}: linear layer carries an expression")

for key in ("coh12_rgb", "rtc_rgb"):
    if not isinstance(vocabulary.display(key), RgbStyle):
        problems.append(f"{key}: display is not a false-color style")

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
if vocabulary.item_asset("coh12_vv_qa_num", "mosaics")["bands"][0]["name"] != "num_obs_mean":
    problems.append("the mosaic count band is not named num_obs_mean")
coh12_vv = vocabulary.display("coh12_vv")
if not isinstance(coh12_vv, LayerStyle) or coh12_vv.vmax != 0.75:
    problems.append("coh12_vv vmax is not the remeasured 0.75")

# --- the regeneration gate -------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    out = Path(tmp)
    render_tree(vocabulary, out)
    rendered = {path.relative_to(out) for path in out.rglob("*") if path.is_file()}
    tracked = {
        path.relative_to(CATALOG)
        for path in CATALOG.rglob("*")
        if path.is_file()
        and not any(part.startswith(".") for part in path.parts)
        and path.relative_to(CATALOG).parts[0] != "_assets"
    }
    for missing in sorted(tracked - rendered):
        problems.append(f"catalog/{missing} is tracked but the render does not write it")
    for extra in sorted(rendered - tracked):
        problems.append(f"catalog/{extra} is rendered but not tracked")
    for path in sorted(rendered & tracked):
        if not filecmp.cmp(out / path, CATALOG / path, shallow=False):
            problems.append(f"catalog/{path} differs from a fresh render")
    if any("differs" in problem or "not tracked" in problem for problem in problems):
        problems.append("regenerate with: uv run scripts/render-catalog.py")

if problems:
    for problem in problems:
        print(f"error  {problem}")
    print(f"FAILED: {len(problems)} problem(s)")
    sys.exit(1)

print(f"OK: {len(ASSET_KEYS)} assets, {len(VIZ_KEYS)} renderings, catalog/ matches its source")
