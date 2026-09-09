#!/usr/bin/env python3
"""Gate on the committed MapLibre style, which is the display constants' one home.

M-27 makes ``catalog/mosaics/styles/default.json`` the **source of truth** for
every published stretch and ramp: the build reads it to make its lookup tables,
and a reader draws its legend from the same block. Before that, the numbers
existed twice — once in ``webmap.py`` and once in the style JSON — and the two
were free to disagree. They did, one repository over: ``glace-viewer`` carried a
blue channel range of ``0.75-2.75`` against the build's ``0.8-2.5``.

So this gate holds the file to what a reader of it is entitled to assume:

- every published layer has an entry, and no entry names an unpublished layer;
- every ramp names a colormap matplotlib can actually resolve, including
  Crameri's ``cmc.*`` maps;
- every range is ordered, ``vmin < vmax``;
- a false-color entry carries three channels and no ramp, and a single-band
  entry carries a ramp and no channels;
- the ``stops`` are *derived*, so they agree with the colormap and range they
  claim to come from — a hand-edited stop is the exact drift this file exists to
  prevent.

The values themselves are no longer pinned here: ``metadata/assets.yaml`` is
where a stretch is changed, with its reasoning beside it, and
``tests/test_render.py`` holds the rendered style to that source. Changing a
published stretch is still a visible diff in two places, the source and the
rendered style.

Reads no data and needs no network.

Run: uv run tests/test_styles.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from glace_catalog.metadata import RGB_KEYS, load_vocabulary
from glace_catalog.stac.naming import ASSET_KEYS
from glace_catalog.styles import RgbStyle, load_styles, ramp_colors

STYLE_PATH = Path(__file__).resolve().parent.parent / "catalog/mosaics/styles/default.json"

#: The published constants, read from the vocabulary rather than pinned here:
#: `metadata/assets.yaml` is where a stretch is changed, with its reasoning
#: beside it, and `tests/test_render.py` holds the rendered style to the same
#: source. What this gate adds is that the style the *build* reads (M-27)
#: says what the vocabulary says, and that every ramp resolves and every range
#: is ordered.
VOCABULARY = load_vocabulary()
EXPECTED_SINGLE: dict[str, tuple[str, float, float, str, bool]] = {}
EXPECTED_RGB: dict[str, tuple[tuple[tuple[float, float], ...], str, bool]] = {}
for _key in (*ASSET_KEYS, *RGB_KEYS):
    _style = VOCABULARY.display(_key)
    if isinstance(_style, RgbStyle):
        EXPECTED_RGB[_key] = (_style.ranges, _style.units, _style.decibel)
    else:
        EXPECTED_SINGLE[_key] = (
            _style.cmap,
            _style.vmin or 0.0,
            _style.vmax or 0.0,
            _style.units,
            _style.decibel,
        )

problems: list[str] = []

if not STYLE_PATH.exists():
    print(f"error  {STYLE_PATH} is absent; M-27 makes it the source of truth")
    raise SystemExit(1)

single, rgb = load_styles(STYLE_PATH)

# --- coverage, both directions -------------------------------------------
missing = sorted(set(EXPECTED_SINGLE) - set(single)) + sorted(set(EXPECTED_RGB) - set(rgb))
extra = sorted(set(single) - set(EXPECTED_SINGLE)) + sorted(set(rgb) - set(EXPECTED_RGB))
problems += [f"{key}: published but absent from the style" for key in missing]
problems += [f"{key}: in the style but not a published layer" for key in extra]

# --- single-band entries --------------------------------------------------
for key, (cmap, vmin, vmax, units, decibel) in EXPECTED_SINGLE.items():
    style = single.get(key)
    if style is None:
        continue
    if isinstance(style, RgbStyle):
        problems.append(f"{key}: read as false color, but it is single band")
        continue
    got = (style.cmap, style.vmin, style.vmax, style.units, style.decibel)
    if got != (cmap, vmin, vmax, units, decibel):
        problems.append(f"{key}: {got!r} != {(cmap, vmin, vmax, units, decibel)!r}")
        continue
    try:
        colors = ramp_colors(style.cmap)
    except KeyError:
        problems.append(f"{key}: colormap {style.cmap!r} is not registered")
        continue
    if style.stops and [stop["color"] for stop in style.stops] != colors:
        problems.append(
            f"{key}: the file's stops disagree with {style.cmap!r}; they are derived, "
            "so a hand edit here is the drift this gate exists to catch",
        )

# --- false-color entries -------------------------------------------------
for key, (ranges, units, decibel) in EXPECTED_RGB.items():
    style = rgb.get(key)
    if style is None:
        continue
    got = (style.ranges, style.units, style.decibel)
    if got != (ranges, units, decibel):
        problems.append(f"{key}: {got!r} != {(ranges, units, decibel)!r}")

# --- shared invariants ----------------------------------------------------
for key, style in {**single, **rgb}.items():
    pairs = style.ranges if isinstance(style, RgbStyle) else ((style.vmin, style.vmax),)
    for index, (low, high) in enumerate(pairs):
        if low is None or high is None:
            problems.append(
                f"{key}: channel {index} has no range; every published stretch is fixed",
            )
        elif low >= high:
            problems.append(f"{key}: channel {index} range {low} >= {high}")

if problems:
    for problem in problems:
        print(f"error  {problem}")
    print(f"FAILED: {len(problems)} problem(s)")
    raise SystemExit(1)

print(f"OK: {len(single)} single-band and {len(rgb)} false-color style(s), values unchanged")
