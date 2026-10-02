"""Default display settings of the web-map layers, and their colour ramps.

The PMTiles archives store encoded values, not colours (see
:mod:`glace_catalog.encoding`). The colormap and value range a layer is drawn
with are set only in the MapLibre style, so a client can change them without
re-tiling. The defaults are defined in ``metadata/assets.yaml`` and read
through :meth:`glace_catalog.metadata.Vocabulary.display`. They end up in each
style layer's ``color-relief-color`` expression and in the collections'
``renders`` blocks. The same record gives the value range each archive is
encoded over, which the build needs.
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

#: Number of colour stops sampled from a default ramp. Renderers interpolate
#: linearly in sRGB between stops; 17 stops keep the result within 4/255 of
#: the perceptually uniform original.
RAMP_STOPS = 17

ATTRIBUTION = "University of Zurich, Contains modified Copernicus Sentinel data {year}"
"""The credit line in every archive header and style source."""

YEAR_IN_TEXT = re.compile(r"\b(?:19|20)\d{2}\b")
"""A four-digit year anywhere in a credit line.

The committed style credits the latest published year, as its ``pmtiles://``
URLs do. ``finalize-catalog`` replaces the year in each per-year style.
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
        Whether the values are drawn in decibels, in which case the tiler
        reads the decibel VRT.
    """

    cmap: str
    vmin: float
    vmax: float
    encoding: ValueEncoding
    units: str = ""
    decibel: bool = False
