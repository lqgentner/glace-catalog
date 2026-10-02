"""Display defaults of the web-map layers, and the colour ramps they name.

Every PMTiles archive carries values, not colours (:mod:`glace_catalog.encoding`),
so no ramp and no stretch is baked into a pixel. What a layer is *drawn with by
default* — a colormap over a value range — is a property of the style alone,
and a client is free to replace it without re-tiling anything. The defaults
live in ``metadata/assets.yaml`` with their reasoning, reach the committed
MapLibre style as each layer's ``color-relief-color`` expression and the
collections' ``renders`` blocks, and are read through
:meth:`glace_catalog.metadata.Vocabulary.display`. What the build does need is
the transport range each archive is encoded over, which travels with the same
record.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

import cmcrameri.cm  # noqa: F401  (imported for its side effect: registers cmc.* ramps)
from matplotlib import colormaps
from matplotlib.colors import to_hex

if TYPE_CHECKING:
    from matplotlib.colors import Colormap

    from glace_catalog.encoding import ValueEncoding

#: Colour stops a default ramp is sampled at. A renderer interpolates linearly
#: in sRGB between them, which at nine stops drifts up to 8/255 from a
#: perceptually uniform ramp; seventeen keeps it under 4/255.
RAMP_STOPS = 17

ATTRIBUTION = "University of Zurich, Contains modified Copernicus Sentinel data {year}"
"""The credit line every archive header and style source carries (M-29)."""

YEAR_IN_TEXT = re.compile(r"\b(?:19|20)\d{2}\b")
"""A four-digit year anywhere in a credit line.

The committed style carries the most recent published year, like its
``pmtiles://`` URLs do; ``finalize-catalog`` re-years both for the style it
publishes per year.
"""


def resolve_cmap(name: str) -> Colormap:
    """Look up a colormap by name, including Crameri's ``cmc.*`` maps."""
    return colormaps[name]


def ramp_colors(cmap: str, n: int = RAMP_STOPS) -> list[str]:
    """Sample ``n`` evenly spaced hex colours from a colormap."""
    colormap = resolve_cmap(cmap)
    return [to_hex(colormap(i / (n - 1))) for i in range(n)]


@dataclass(frozen=True)
class LayerStyle:
    """How a single-band product is encoded, and drawn by default.

    Parameters
    ----------
    cmap
        Default colormap, registered with matplotlib, including Crameri's
        ``cmc.*`` scientific ramps.
    vmin, vmax
        Default value range the ramp is stretched over.
    encoding
        The transport range the web-map archive is quantised over. Fixed per
        layer, so that a year slider compares like with like.
    units
        Unit label of the drawn values.
    decibel
        Whether the values are drawn in decibels, so the tiler must read the
        chain's decibel VRT rather than its linear one.
    """

    cmap: str
    vmin: float
    vmax: float
    encoding: ValueEncoding
    units: str = ""
    decibel: bool = False
