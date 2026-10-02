"""Pack physical values into the RGB channels of web-map tiles.

Each web-map archive stores a quantised value per pixel, so a client chooses
the colour ramp and stretch and can read values back under the cursor. The
packing is the one MapLibre's ``raster-dem`` source decodes with
``"encoding": "custom"``::

    value = R * redFactor + G * greenFactor + B * blueFactor - baseShift

A ``color-relief`` layer then colours that value with an ``interpolate``
expression on ``["elevation"]``. Both are part of the MapLibre style
specification. Note that MapLibre subtracts ``baseShift``, although the
specification says it is added.

Two details of MapLibre GL JS 6.9 constrain the factors:

- The colour-relief layer packs its stops with ``packDEMData``, which treats
  the factors as digit weights (red most significant) and divides by the
  smallest one, so no factor may be zero.
- A pixel is decoded as a weighted sum, so any split of one step across R, G
  and B decodes a grey pixel the same way.

:class:`ValueEncoding` therefore writes the same 8-bit code into all three
channels (a grey tile) and splits one step as ``65536 : 256 : 1``. A grey pixel
``(n, n, n)`` then decodes to ``n * scale - baseShift`` exactly, and stops
packed by ``packDEMData`` decode back to themselves. Grey tiles also survive
lossy WebP well, because only the luma plane carries signal.

Code ``0`` means nodata. The tiles also carry a binary alpha band
(:func:`glace_catalog.tiler.render_tile`). WebP stores alpha losslessly, so
nodata stays nodata despite compression artefacts at a data edge. Alpha is
either 0 or 255, because browsers premultiply alpha when decoding, which would
corrupt any code under partial alpha.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence

GREY_WEIGHTS = (65536.0, 256.0, 1.0)
"""Positional weights of the three channels; their sum is one step."""

NODATA_CODE = 0
"""The code of a pixel without data."""

MAX_CODE = 255
"""The largest code of an 8-bit channel."""


@dataclass(frozen=True)
class ValueEncoding:
    """Linear 8-bit quantisation of one layer's values.

    Parameters
    ----------
    lo, hi
        The transport range: code 1 is ``lo``, code 255 is ``hi``. Values
        outside it are clipped. Wider than the display stretch, so a client
        can restretch without running out of codes.
    """

    lo: float
    hi: float

    def __post_init__(self) -> None:
        """Refuse an empty transport range."""
        if not self.hi > self.lo:
            msg = f"Empty transport range [{self.lo}, {self.hi}]"
            raise ValueError(msg)

    @property
    def scale(self) -> float:
        """One code step, in the layer's unit."""
        return (self.hi - self.lo) / (MAX_CODE - 1)

    @property
    def offset(self) -> float:
        """The value of code ``0`` were it not nodata: ``value = code * scale + offset``."""
        return self.lo - self.scale

    def encode(self, values: np.ndarray) -> np.ndarray:
        """Quantise values to codes; non-finite values become :data:`NODATA_CODE`."""
        finite = np.isfinite(values)
        clipped = np.clip(np.where(finite, values, self.lo), self.lo, self.hi)
        codes = 1 + np.round((clipped - self.lo) / self.scale)
        return np.where(finite, codes, NODATA_CODE).astype("uint8")

    def decode(self, codes: np.ndarray) -> np.ndarray:
        """Codes back to values, ``nan`` for nodata."""
        values = codes.astype("float64") * self.scale + self.offset
        return np.where(codes == NODATA_CODE, np.nan, values)

    def maplibre(self) -> dict[str, Any]:
        """Return the ``raster-dem`` source properties that decode a grey tile."""
        total = sum(GREY_WEIGHTS)
        red, green, blue = (self.scale * weight / total for weight in GREY_WEIGHTS)
        return {
            "encoding": "custom",
            "redFactor": red,
            "greenFactor": green,
            "blueFactor": blue,
            # MapLibre subtracts it: value = R*r + G*g + B*b - baseShift.
            "baseShift": -self.offset,
        }

    def stac_band(self, unit: str) -> dict[str, Any]:
        """Return the archive's band object: STAC 1.1 with the raster extension."""
        band: dict[str, Any] = {
            "data_type": "uint8",
            "nodata": NODATA_CODE,
            "raster:scale": self.scale,
            "raster:offset": self.offset,
        }
        if unit:
            band["unit"] = unit
        return band

    def luts(self, *, stops: int = 256) -> list[list[tuple[float, int]]]:
        """Return GDAL ``<LUT>`` tables that write the code into all three bands.

        GDAL interpolates linearly between stops; with 256 stops over 254
        codes the rounding error stays under one code. Values below ``lo``
        map to code 1, so every valid pixel has a nonzero code.
        """
        values = np.linspace(self.lo, self.hi, stops)
        table = [
            (float(value), int(1 + round((value - self.lo) / self.scale))) for value in values
        ]
        return [table, table, table]

    def ramp(
        self,
        colors: Sequence[str],
        vmin: float,
        vmax: float,
    ) -> list[Any]:
        """Build a ``color-relief-color`` expression spreading ``colors`` over ``vmin``-``vmax``.

        Nodata (code 0) decodes to one step below ``lo``. The expression is
        transparent up to half a step below ``lo`` and switches to the first
        colour just after, so nodata is transparent. Valid values below ``vmin``
        take the first colour and values above ``vmax`` the last.
        """
        edge = self.lo - self.scale / 2
        stops: list[tuple[float, str]] = [
            (edge, "rgba(0, 0, 0, 0)"),
            (edge + self.scale / 100, colors[0]),
        ]
        for position, color in zip(np.linspace(vmin, vmax, len(colors)), colors, strict=True):
            if position > stops[-1][0]:
                stops.append((round(float(position), 6), color))
        expression: list[Any] = ["interpolate", ["linear"], ["elevation"]]
        for position, color in stops:
            expression += [position, color]
        return expression
