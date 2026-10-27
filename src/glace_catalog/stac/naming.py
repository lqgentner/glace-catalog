"""Composite filenames in the source archive, and their names in the store.

The source archive names a composite by all its attributes, for example
``S1_COH12_LRW_32TPS05_VV_20240619_20240912.tif``. The store names it by its
asset key alone, ``coh12_vv.tif``, inside a directory that gives the tile and
year. Store filenames therefore need no parsing, and each asset key has exactly
one file per item.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from upath import UPath

POLARIZATIONS = ("VV", "VH")
PRODUCTS = ("COH12", "RTC")

ASSET_KEYS: tuple[str, ...] = tuple(
    f"{product.lower()}_{pol.lower()}{suffix}"
    for product in PRODUCTS
    for pol in POLARIZATIONS
    for suffix in ("", "_qa_num", "_qa_cqm")
)
"""The twelve asset keys a complete tile or mosaic item carries."""


def published_name(asset_key: str) -> str:
    """Return the store filename of an asset key, e.g. ``coh12_vv.tif``.

    The directory gives the year and the item properties give the compositing
    window, so the filename is just the asset key. All readers and writers of
    store rasters use this function.
    """
    return f"{asset_key}.tif"


_COMPOSITE_RE = re.compile(
    r"^S1_(?P<product>COH\d{2}|RTC)"
    r"_(?:LRW|COMP_(?P<orbit>[AD]))"
    r"(?:_QA-(?P<qa>NUM|CQM))?"
    r"_(?P<tile>\d{2}[A-Z]{3}\d{2})"
    r"_(?P<pol>VV|VH|HH|HV)"
    r"_(?P<start>\d{8})_(?P<end>\d{8})\.tif$",
)


@dataclass(frozen=True)
class CompositeFile:
    """A parsed per-tile composite or QA raster.

    LRW (locally resolution-weighted) composites combine all acquisitions;
    pass composites (``COMP_A``, ``COMP_D``) use one orbit direction.
    """

    path: Path | UPath
    product: str  # "COH12" or "RTC"
    qa: str | None  # None, "NUM", or "CQM"
    tile: str  # MGRS 10-km code, e.g. "32TMS55"
    polarization: str
    orbit: str | None  # None for LRW composites, "A" or "D" for pass composites

    @property
    def asset_key(self) -> str:
        """STAC asset key, e.g. ``coh12_vv`` or ``rtc_vh_comp_a_qa_num``.

        Polarization comes before the QA role, so a sorted listing keeps a
        band's QA directly beneath the measurement it describes.
        """
        key = f"{self.product.lower()}_{self.polarization.lower()}"
        if self.orbit:
            key += f"_comp_{self.orbit.lower()}"
        if self.qa:
            key += f"_qa_{self.qa.lower()}"
        return key

    @property
    def published_name(self) -> str:
        """Filename this composite takes in the store, e.g. ``coh12_vv.tif``."""
        return published_name(self.asset_key)


VIZ_SUFFIX = "_viz"
"""Suffix of a rendering: ``coh12_vv_viz.pmtiles`` is the web-map archive of
``coh12_vv.tif``. Both sit in one directory, and the suffix tells them apart."""


def viz_key(asset_key: str) -> str:
    """Asset key of the rendering of one data asset."""
    return f"{asset_key}{VIZ_SUFFIX}"


def viz_filename(asset_key: str) -> str:
    """Filename of the rendering of one data asset, beside its COG."""
    return f"{viz_key(asset_key)}.pmtiles"


def data_key(key: str) -> str:
    """Return the data asset a rendering key belongs to.

    Raises
    ------
    ValueError
        If ``key`` is not a rendering key.
    """
    if not key.endswith(VIZ_SUFFIX):
        msg = f"{key!r} is not a rendering key; expected the {VIZ_SUFFIX!r} suffix"
        raise ValueError(msg)
    return key[: -len(VIZ_SUFFIX)]


def by_asset_key(file: CompositeFile) -> str:
    """Sort key that orders composite files by asset key."""
    return file.asset_key


def parse_composite_name(path: Path | UPath) -> CompositeFile | None:
    """Parse a composite filename; return ``None`` for non-matching names.

    The dates in the name are matched but not used. A year's compositing window
    comes from the run manifest (see ``composite_window``).
    """
    match = _COMPOSITE_RE.match(path.name)
    if match is None:
        return None
    return CompositeFile(
        path=path,
        product=match["product"],
        qa=match["qa"],
        tile=match["tile"],
        polarization=match["pol"],
        orbit=match["orbit"],
    )


def require_unique_composites(by_tile: Mapping[str, Sequence[CompositeFile]]) -> None:
    """Reject multiple files that resolve to the same published name.

    A composite's filename includes its compositing window. Rerunning the same
    window overwrites the file; a changed window writes a second file beside
    it. Choosing between the two is a deliberate archive-maintenance step, so
    duplicates are an error.

    Raises
    ------
    ValueError
        If any tile contains more than one file with the same asset key.
    """
    duplicates: list[tuple[str, str, list[CompositeFile]]] = []
    for tile, files in sorted(by_tile.items()):
        by_key: dict[str, list[CompositeFile]] = defaultdict(list)
        for file in files:
            by_key[file.asset_key].append(file)
        duplicates.extend(
            (tile, key, matches) for key, matches in sorted(by_key.items()) if len(matches) > 1
        )
    if not duplicates:
        return

    details = "\n".join(
        f"- {tile}/{key}: " + ", ".join(str(file.path) for file in matches)
        for tile, key, matches in duplicates
    )
    msg = (
        "Duplicate composite assets found. Resolve superseded date-range files "
        f"explicitly before publishing:\n{details}"
    )
    raise ValueError(msg)
