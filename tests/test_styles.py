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
  a client cannot draw a value the archive cannot carry.

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

STYLE_PATH = Path(__file__).resolve().parent.parent / "catalog/mosaics/styles/default.json"

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

if problems:
    for problem in problems:
        print(f"error  {problem}")
    print(f"FAILED: {len(problems)} problem(s)")
    raise SystemExit(1)

print(f"OK: {len(layers)} encoded layer(s), each decoded and coloured as the vocabulary says")
