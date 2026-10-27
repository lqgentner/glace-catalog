#!/usr/bin/env python3
"""Gate on the committed MapLibre style and the display records behind it.

Every web-map archive holds value codes. The style is what decodes and colours
them, so this gate holds it to what a client is entitled to assume:

- every published layer has exactly one source and one layer, and nothing else;
- each source is a ``raster-dem`` with the custom encoding of the layer's
  transport range, so the codes decode to the values the archive was built from;
- each layer is a ``color-relief`` whose ramp is the default colormap over the
  default stretch, and that colormap resolves;
- every default stretch is ordered and lies inside its transport range, since
  a client cannot draw a value the archive cannot carry;
- the tile grid's style draws the grid archive's one vector layer, filled by
  the buffered glacier fraction in steps, so the Portolan browser can draw a
  legend;
- the tiles collection's default style draws the mosaics' coherence VV archive
  as the mosaics style does, with the tile grid outlined over it;
- the tiles collection registers the grid archive as Portolan requires of a
  collection with PMTiles: a ``visual`` asset, a ``rel: "pmtiles"`` link that
  names its layer, the web-map-links extension and a style; the mosaics
  collection carries no such link (docs/conformance.md).

Reads no data and needs no network.

Run: uv run tests/test_styles.py
"""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from glace_catalog.metadata import load_vocabulary
from glace_catalog.stac.naming import ASSET_KEYS
from glace_catalog.styles import ramp_colors

CATALOG = Path(__file__).resolve().parent.parent / "catalog"
STYLE_PATH = CATALOG / "mosaics/styles/default.json"
GRID_STYLE_PATH = CATALOG / "tiles/styles/grid.json"
TILES_STYLE_PATH = CATALOG / "tiles/styles/default.json"
WEB_MAP_LINKS = "https://stac-extensions.github.io/web-map-links/v1.3.0/schema.json"
STYLE_MEDIA_TYPE = "application/vnd.mapbox.style+json"

VOCABULARY = load_vocabulary()
problems: list[str] = []

style = json.loads(STYLE_PATH.read_text())
sources = style["sources"]
layers = {layer["source"].removeprefix("src-"): layer for layer in style["layers"]}

expected = set(ASSET_KEYS)
for name, found in (
    ("source", {key.removeprefix("src-") for key in sources}),
    ("layer", set(layers)),
):
    problems += [
        f"{key}: published but no {name} in the style" for key in sorted(expected - found)
    ]
    problems += [
        f"{key}: a {name} in the style but not published" for key in sorted(found - expected)
    ]

for key in sorted(expected & set(layers)):
    display = VOCABULARY.display(key)
    encoding = display.encoding
    source = sources[f"src-{key}"]
    if source.get("type") != "raster-dem":
        problems.append(f"{key}: source is {source.get('type')!r}, not raster-dem")
    if {name: source.get(name) for name in encoding.maplibre()} != encoding.maplibre():
        problems.append(f"{key}: source factors differ from the transport range {encoding}")
    layer = layers[key]
    if layer.get("type") != "color-relief":
        problems.append(f"{key}: layer is {layer.get('type')!r}, not color-relief")
    try:
        ramp = encoding.ramp(ramp_colors(display.cmap), display.vmin, display.vmax)
    except KeyError:
        problems.append(f"{key}: colormap {display.cmap!r} is not registered")
        continue
    if layer.get("paint", {}).get("color-relief-color") != ramp:
        problems.append(f"{key}: the ramp is not {display.cmap} over the default stretch")
    if not encoding.lo <= display.vmin < display.vmax <= encoding.hi:
        problems.append(
            f"{key}: default stretch {display.vmin}-{display.vmax} is not ordered inside "
            f"the transport range {encoding.lo}-{encoding.hi}",
        )

grid_sources: dict[str, dict[str, object]] = {}
if not GRID_STYLE_PATH.is_file():
    problems.append(f"{GRID_STYLE_PATH.relative_to(CATALOG)} is missing")
else:
    grid_style = json.loads(GRID_STYLE_PATH.read_text())
    grid_sources = grid_style.get("sources", {})
    if grid_style.get("version") != 8:
        problems.append("grid style: not MapLibre style version 8")
    if [source.get("type") for source in grid_sources.values()] != ["vector"]:
        problems.append("grid style: needs exactly one vector source")
    if [source.get("url") for source in grid_sources.values()] != ["pmtiles://../grid.pmtiles"]:
        problems.append("grid style: the source does not point at ../grid.pmtiles")
    grid_layers = grid_style.get("layers", [])
    if not grid_layers or any(layer.get("source-layer") != "mgrs" for layer in grid_layers):
        problems.append("grid style: every layer must draw source-layer mgrs")
    fills = [layer for layer in grid_layers if layer.get("type") == "fill"]
    if not fills or "glacier_fraction_buffered" not in json.dumps(
        fills[0].get("paint", {}).get("fill-color"),
    ):
        problems.append("grid style: the fill colour does not read glacier_fraction_buffered")
    elif fills[0]["paint"]["fill-color"][0] != "step":
        problems.append("grid style: the fill is not a step expression, so it has no legend")

tiles_style = json.loads(TILES_STYLE_PATH.read_text())
tiles_sources = tiles_style.get("sources", {})
tiles_relief = [
    layer for layer in tiles_style.get("layers", []) if layer["type"] == "color-relief"
]
if [layer["source"] for layer in tiles_relief] != ["src-coh12_vv"]:
    problems.append("tiles style: needs one color-relief layer, of coh12_vv")
elif tiles_relief[0] != {**layers["coh12_vv"], "id": tiles_relief[0]["id"]}:
    problems.append("tiles style: coh12_vv is not drawn as the mosaics style draws it")
if tiles_sources.get("src-coh12_vv", {}) | {"url": ""} != sources["src-coh12_vv"] | {"url": ""}:
    problems.append("tiles style: the coh12_vv source differs from the mosaics style's")
if not str(tiles_sources.get("src-coh12_vv", {}).get("url", "")).startswith(
    "pmtiles://../../mosaics/",
):
    problems.append("tiles style: coh12_vv does not point at the mosaics collection's archive")
if tiles_sources.get("grid") != grid_sources.get("grid"):
    problems.append("tiles style: the grid source differs from the grid style's")

tiles = json.loads((CATALOG / "tiles/collection.json").read_text())
grid = tiles["assets"].get("grid", {})
if "glacier-fractions" in tiles["assets"]:
    problems.append("tiles collection: still registers glacier-fractions")
if grid.get("roles") != ["visual"] or grid.get("type") != "application/vnd.pmtiles":
    problems.append("tiles collection: grid must be an application/vnd.pmtiles visual asset")
if not str(grid.get("href", "")).endswith("/tiles/grid.pmtiles"):
    problems.append("tiles collection: grid href does not name tiles/grid.pmtiles")
pmtiles_links = [link for link in tiles["links"] if link.get("rel") == "pmtiles"]
if [
    (link.get("href"), link.get("type"), link.get("pmtiles:layers")) for link in pmtiles_links
] != [(grid.get("href"), "application/vnd.pmtiles", ["mgrs"])]:
    problems.append("tiles collection: needs one pmtiles link to the grid, layers [mgrs]")
if WEB_MAP_LINKS not in tiles["stac_extensions"]:
    problems.append("tiles collection: web-map-links extension not declared")
tiles_styles = {
    key: (asset.get("href"), asset.get("roles"), asset.get("type"))
    for key, asset in tiles["assets"].items()
    if "style" in asset.get("roles", [])
}
if tiles_styles != {
    "style-default": ("./styles/default.json", ["style", "default"], STYLE_MEDIA_TYPE),
    "style-grid": ("./styles/grid.json", ["style"], STYLE_MEDIA_TYPE),
}:
    problems.append(
        "tiles collection: needs style-default (the default) and style-grid, "
        f"found {tiles_styles}",
    )
mosaics = json.loads((CATALOG / "mosaics/collection.json").read_text())
if any(link.get("rel") == "pmtiles" for link in mosaics["links"]):
    problems.append("mosaics collection: carries a pmtiles link")

if problems:
    for problem in problems:
        print(f"error  {problem}")
    print(f"FAILED: {len(problems)} problem(s)")
    raise SystemExit(1)

print(
    f"OK: {len(layers)} encoded layer(s), each decoded and coloured as the vocabulary says; "
    "the tile grid is registered and styled",
)
