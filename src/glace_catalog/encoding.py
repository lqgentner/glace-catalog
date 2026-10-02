"""Value-encoded web-map tiles: physical values packed into RGB.

A web-map archive carries the quantised value rather than a colour, so a client
chooses the ramp and the stretch and can read a value back under the cursor.
The packing is the one MapLibre's ``raster-dem`` source decodes with
``"encoding": "custom"``::

    value = R * redFactor + G * greenFactor + B * blueFactor - baseShift

and the ``color-relief`` layer colours that value through an ``interpolate``
expression on ``["elevation"]``. Both are in the MapLibre style
specification, so a style that declares the four numbers is self-describing.
MapLibre subtracts ``baseShift``, although the specification says it is added;
Terrarium's built-in ``baseShift = 32768`` confirms the subtraction.

Two constraints of MapLibre GL JS 6.9 shape the factors:

- The colour-relief layer packs its *stops* with ``packDEMData``, which treats
  the factors as the weights of a positional number (red the most
  significant): it divides by the smallest factor, so none may be zero.
- A value is decoded as a dot product, so any ``R:G:B`` split of one step
  decodes the same grey pixel.

:class:`ValueEncoding` writes the 8-bit code into all three channels — a grey
tile — and splits one step ``scale`` over them as ``65536 : 256 : 1``. A grey
pixel ``(n, n, n)`` decodes to ``n * scale - baseShift`` exactly, and a stop
packed positionally decodes to itself. A grey tile survives lossy WebP: the
encoder's chroma planes are flat and only luma carries the signal.

Code ``0`` is nodata. The tiles keep a *binary* alpha band beside it
(:func:`glace_catalog.tiler.render_tile`): WebP stores alpha losslessly, so the
encoder's ringing at a data edge cannot leak a code into nodata, and partial
alpha is never written, because browsers premultiply alpha on decode and would
corrupt the code beneath it.
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
        """GDAL ``<LUT>`` tables that write the code into all three bands.

        The lookup is piecewise-linear, so a value between two stops takes a
        code between theirs; at 256 stops over 254 codes the round-off is
        under one code. Values below ``lo`` are clamped to code 1, so a valid
        pixel never reads as nodata.
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
        """Build a ``color-relief-color`` expression: ``colors`` over ``vmin``-``vmax``.

        Code 0 decodes one step under ``lo``. The ramp is transparent up to
        half a step under ``lo`` and jumps to the first colour a hundredth of
        a step later, so nodata draws nothing and every valid code below
        ``vmin`` clamps to the first colour, as above ``vmax`` it clamps to
        the last.
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
