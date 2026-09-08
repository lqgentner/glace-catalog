"""The published display constants, and the committed style that holds them.

Every PMTiles archive bakes a colour ramp and a value range into 8-bit RGBA, so
a bad choice can only be fixed by re-tiling — and the same numbers have to reach
a reader, who needs them to draw a legend under a picture whose pixels are
display codes rather than measurements.

**The committed MapLibre style is the one place they live** (M-27).
``catalog/mosaics/styles/default.json`` carries them per layer under
``metadata.portolan:legend``; this module reads them, and the build makes its
lookup tables from what it reads. Nothing holds a second copy.

That direction is the whole point. The numbers used to sit in a table in
``webmap.py`` *and* in the style JSON, with nothing keeping the two in step, and
``glace-viewer`` briefly held a third — a blue channel range of ``0.75-2.75``
against the build's ``0.8-2.5``. Reading the reviewed file rather than
regenerating it also upholds M-21: the file that gets reviewed is the file that
gets used.

The cost is a build-time dependency on a catalog file, which is real:
``publish-year.py --steps pmtiles`` cannot run without a valid style. That is
accepted because the alternative is two copies, and two copies is the bug.

**``stops`` are derived, never authored.** They are a rendering of ``cmap`` over
``vmin``-``vmax``, kept in the file so that a client which cannot resolve a
``cmc.*`` name still has colours. ``tests/test_styles.py`` holds them to their
colormap, so editing one by hand fails the gate rather than drifting quietly.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

import cmcrameri.cm  # noqa: F401  (imported for its side effect: registers cmc.* ramps)
from matplotlib import colormaps
from matplotlib.colors import to_hex

if TYPE_CHECKING:
    from collections.abc import Mapping

    from matplotlib.colors import Colormap

#: Number of colour stops recorded for a legend gradient. A reader interpolates
#: linearly in sRGB between them, which at nine stops drifts up to 8/255 from a
#: perceptually uniform ramp; seventeen keeps it under 4/255.
LEGEND_STOPS = 17

#: Where the legend block hangs off a MapLibre layer. FTW's convention, copied
#: deliberately: it appears nowhere in `portolan-spec`, and keeping one shape
#: rather than two is the only thing that makes it worth anything if the
#: question ever settles (portolan-spec#41).
LEGEND_KEY = "portolan:legend"

#: This repository's committed style. Resolved from the package location, which
#: holds while ``catalog/`` is a sibling of ``src/`` — that is, in a checkout,
#: which is how the build runs. An installed wheel has no catalog beside it, so
#: a caller outside a checkout has to pass its own path.
DEFAULT_STYLE_PATH = Path(__file__).resolve().parents[2] / "catalog/mosaics/styles/default.json"


def resolve_cmap(name: str) -> Colormap:
    """Look up a colormap by name, including Crameri's ``cmc.*`` maps."""
    return colormaps[name]


def ramp_colors(cmap: str, n: int = LEGEND_STOPS) -> list[str]:
    """Sample ``n`` evenly spaced hex colours from a colormap, for the legend."""
    colormap = resolve_cmap(cmap)
    return [to_hex(colormap(i / (n - 1))) for i in range(n)]


@dataclass(frozen=True)
class LayerStyle:
    """Colour ramp and value range a single-band product is rendered with.

    Parameters
    ----------
    cmap
        Name of a colormap registered with matplotlib, including Crameri's
        ``cmc.*`` scientific ramps.
    vmin, vmax
        Value range the ramp is stretched over.
    units
        Unit label shown on a legend.
    decibel
        Whether the range is quoted in decibels, so the tiler must read the
        chain's decibel VRT rather than its linear one.
    measured
        Whether the range was measured on the published archive. A provisional
        range still renders; it is flagged so a build can report which layers
        have not been checked at native resolution yet.
    stops
        The legend gradient as read from the file, derived from ``cmap`` over
        ``vmin``-``vmax``. Empty when the file carried none.
    """

    cmap: str
    vmin: float | None = None
    vmax: float | None = None
    units: str = ""
    decibel: bool = False
    measured: bool = False
    stops: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class RgbStyle:
    """Value ranges of a false-colour layer, one per band.

    A three-channel image has no single ramp, which is why its legend carries
    ``type: "channels"`` rather than ``stops``. Red and green repeat the sibling
    single-band ranges; blue — the co- to cross-polarized contrast — is its own
    quantity.

    Parameters
    ----------
    ranges
        ``(vmin, vmax)`` for red, green and blue.
    units
        Unit label shown on a legend.
    decibel
        Whether the ranges are quoted in decibels.
    measured
        Whether the ranges were measured on the published archive.
    bands
        What each channel carries, for the legend. Same order as ``ranges``.
    """

    ranges: tuple[tuple[float, float], tuple[float, float], tuple[float, float]]
    units: str = ""
    decibel: bool = False
    measured: bool = False
    bands: tuple[str, ...] = ()


#: A four-digit year anywhere in a credit line. The committed style carries the
#: most recent published year, like its ``pmtiles://`` URLs do, and both are
#: re-yeared the same way.
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def load_attribution(path: Path, year: int | None = None) -> str:
    """Read the credit line every published archive and map source carries.

    One home, for the same reason the ramps have one: it was previously written
    three times — in the style's sources, in the tiler call that bakes it into a
    PMTiles header, and again at runtime in ``glace-viewer`` — and the three did
    not agree.

    Parameters
    ----------
    path
        The committed style.
    year
        Re-year the line to this. The Copernicus wording is per-year, so an
        archive built for 2022 must not credit 2024.

    Raises
    ------
    ValueError
        If no source declares one. Publishing Sentinel data without the
        attribution is a licence problem, so this is not defaulted.
    """
    document = json.loads(Path(path).read_text())
    for source in document.get("sources", {}).values():
        credit = source.get("attribution")
        if credit:
            return _YEAR.sub(str(year), credit) if year is not None else credit
    msg = f"{path}: no source declares an 'attribution'; Sentinel data requires one"
    raise ValueError(msg)


def layer_key(layer_id: str) -> str:
    """Strip a style layer id down to the asset key its archive is named after.

    ``glace-coh12_vv-2024`` is ``coh12_vv``. The year is in the id because
    ``pmtiles:layers`` names a specific archive, but the stretch is a per-product
    constant and does not vary with it — which is what lets one style file
    describe every year.
    """
    stem = layer_id.removeprefix("glace-")
    head, _, tail = stem.rpartition("-")
    return head if head and tail.isdigit() else stem


def _legend(layer: Mapping[str, Any]) -> dict[str, Any] | None:
    return (layer.get("metadata") or {}).get(LEGEND_KEY)


def load_styles(path: Path) -> tuple[dict[str, LayerStyle], dict[str, RgbStyle]]:
    """Read every layer's display constants out of a MapLibre style.

    Parameters
    ----------
    path
        The committed style, normally ``catalog/mosaics/styles/default.json``.

    Returns
    -------
    tuple
        ``(single_band, false_colour)``, both keyed by asset key.

    Raises
    ------
    ValueError
        If a legend block is malformed. A missing block is skipped — a style may
        legitimately carry a layer that is not a GLACE raster — but a present
        and broken one is an error, because it would otherwise bake a silently
        wrong stretch into an archive that can only be fixed by re-tiling.
    """
    document = json.loads(Path(path).read_text())
    single: dict[str, LayerStyle] = {}
    rgb: dict[str, RgbStyle] = {}
    for layer in document.get("layers", []):
        legend = _legend(layer)
        if legend is None:
            continue
        key = layer_key(layer["id"])
        kind = legend.get("type")
        units = legend.get("unit", "")
        decibel = bool(legend.get("decibel", False))
        measured = bool(legend.get("measured", False))
        if kind == "channels":
            channels = legend.get("channels")
            if not isinstance(channels, list) or len(channels) != 3:  # noqa: PLR2004
                msg = f"{path}: layer {layer['id']!r} is 'channels' but has no three channels"
                raise ValueError(msg)
            red, green, blue = [(float(c["vmin"]), float(c["vmax"])) for c in channels]
            rgb[key] = RgbStyle(
                ranges=(red, green, blue),
                units=units,
                decibel=decibel,
                measured=measured,
                bands=tuple(str(c.get("band", "")) for c in channels),
            )
        elif kind == "ramp":
            if "cmap" not in legend:
                msg = (
                    f"{path}: layer {layer['id']!r} is 'ramp' but names no 'cmap'. "
                    "Stops alone cannot rebuild a lookup table."
                )
                raise ValueError(msg)
            single[key] = LayerStyle(
                cmap=str(legend["cmap"]),
                vmin=float(legend["vmin"]),
                vmax=float(legend["vmax"]),
                units=units,
                decibel=decibel,
                measured=measured,
                stops=tuple(legend.get("stops", ())),
            )
        else:
            msg = (
                f"{path}: layer {layer['id']!r} has legend type {kind!r}, not 'ramp' or 'channels'"
            )
            raise ValueError(msg)
    return single, rgb
