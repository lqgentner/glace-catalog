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
- a false-colour entry carries three channels and no ramp, and a single-band
  entry carries a ramp and no channels;
- the ``stops`` are *derived*, so they agree with the colormap and range they
  claim to come from — a hand-edited stop is the exact drift this file exists to
  prevent.

It also pins the values themselves, so that changing a published stretch is a
visible diff in two places rather than one. The table below started as a record
that the move out of ``webmap.py`` changed *where* the numbers live and not what
they are; the QA ranges have since been re-derived from the published mosaics,
and carry their reasoning beside them.

Reads no data and needs no network.

Run: uv run tests/test_styles.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from glace_catalog.styles import RgbStyle, load_styles, ramp_colors

STYLE_PATH = Path(__file__).resolve().parent.parent / "catalog/mosaics/styles/default.json"

#: The published constants. Ramp, vmin, vmax, units, decibel. Remeasured
#: 2026-09-09 on the warped z13 display mosaics of 2021-2024 (25 native-
#: resolution windows per band per year, 2-98 %), averaged over the years and
#: rounded to 0.5; the false-colour blue bands from the derived third band.
EXPECTED_SINGLE = {
    "coh12_vv": ("cmc.lipari", 0.10, 0.75, "", False),
    "coh12_vh": ("cmc.lipari", 0.10, 0.55, "", False),
    # Never measured before 2026-09-09: the old -18.5 / -26 floors were 2-2.5 dB
    # too dark. 2021, the two-satellite year, sits ~1 dB below the others at the
    # dark end; one fixed stretch still holds (M-27).
    "rtc_vv": ("cmc.grayC", -16.5, -4.0, "dB", True),
    "rtc_vh": ("cmc.grayC", -23.5, -11.0, "dB", True),
    # QA-NUM is an acquisition count, so its ceiling moves with the
    # constellation: the exact per-file maxima are 49 (COH12) and 58 (RTC) in
    # 2021 with S1A+S1B flying, 26 and 30 from 2022 on with S1A alone. 2026
    # flies three (S1A, S1C, S1D): 58 x 1.5 = 87, rounded up to 90.
    #
    # **One range for both products, not one each.** RTC sees more acquisitions
    # than COH12, which needs pairs; a per-product range would normalise that
    # difference away and draw the two identically. Sharing 0-90 is what makes
    # it visible. cmc.glasgow, the same ramp as CQM: dark for few, bright for many.
    "coh12_vv_qa_num": ("cmc.glasgow", 0.0, 90.0, "", False),
    "coh12_vh_qa_num": ("cmc.glasgow", 0.0, 90.0, "", False),
    "rtc_vv_qa_num": ("cmc.glasgow", 0.0, 90.0, "", False),
    "rtc_vh_qa_num": ("cmc.glasgow", 0.0, 90.0, "", False),
    # CQM is one-sided: 2-98 % lands at about -2.5 / +10 dB for COH12 and
    # -3.7 / +8 dB for RTC, within 1 dB across the years. The range is the
    # measured one, not a symmetric one: 0 dB has no physical meaning in the
    # composites, so there is nothing to centre. Under the old -3 / 3 a tenth
    # of the scene saturated white.
    "coh12_vv_qa_cqm": ("cmc.glasgow", -3.0, 10.0, "dB", True),
    "coh12_vh_qa_cqm": ("cmc.glasgow", -3.0, 10.0, "dB", True),
    "rtc_vv_qa_cqm": ("cmc.glasgow", -4.0, 8.0, "dB", True),
    "rtc_vh_qa_cqm": ("cmc.glasgow", -4.0, 8.0, "dB", True),
}

#: Per-channel ranges of the two false-colour layers, red, green, blue. Red and
#: green repeat the single-band stretches; blue is the ratio (COH12) or the
#: difference in dB (RTC), measured on the rgb VRT's third band.
EXPECTED_RGB = {
    "rtc_rgb": (((-16.5, -4.0), (-23.5, -11.0), (3.5, 10.5)), "dB", True),
    "coh12_rgb": (((0.10, 0.75), (0.10, 0.55), (0.8, 2.6)), "", False),
}

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
        problems.append(f"{key}: read as false colour, but it is single band")
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

# --- false-colour entries -------------------------------------------------
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

print(f"OK: {len(single)} single-band and {len(rgb)} false-colour style(s), values unchanged")
