"""Filenames of the GLACE archive, and the published names they map onto.

Two vocabularies meet here. The *staging* archive names a composite by
everything about it — ``S1_COH12_LRW_32TPS05_VV_20240619_20240912.tif`` — while
the *store* names it by its role alone: ``coh12_vv.tif``, in a directory that
already says which tile and which year. The published name is the asset key, so
nothing in the store has to be parsed to be understood, and two files resolving
to one asset key becomes structurally impossible.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from upath import UPath

logger = logging.getLogger(__name__)

POLARIZATIONS = ("VV", "VH")
PRODUCTS = ("COH12", "RTC")
QA_METRICS = ("NUM", "CQM")

IGNORE_VALUE = 255
"""Label value for a pixel that must be excluded from loss and metrics."""

ASSET_KEYS: tuple[str, ...] = tuple(
    f"{product.lower()}_{pol.lower()}{suffix}"
    for product in PRODUCTS
    for pol in POLARIZATIONS
    for suffix in ("", "_qa_num", "_qa_cqm")
)
"""The twelve LRW asset keys a complete tile or mosaic item carries."""

_COMPOSITE_RE = re.compile(
    r"^S1_(?P<product>COH\d{2}|RTC)"
    r"_(?:LRW|COMP_(?P<orbit>[AD]))"
    r"(?:_QA-(?P<qa>NUM|CQM))?"
    r"_(?P<tile>\d{2}[A-Z]{3}\d{2})"
    r"_(?P<pol>VV|VH|HH|HV)"
    r"_(?P<start>\d{8})_(?P<end>\d{8})\.tif$",
)
_LABEL_RE = re.compile(
    r"^LABELS_(?P<source>[A-Z0-9-]+)"
    r"(?:_(?P<year>\d{4}))?"
    r"_(?P<tile>\d{2}[A-Z]{3}\d{2})\.tif$",
)


@dataclass(frozen=True)
class LabelSource:
    """A glacier inventory published as a collection of label masks.

    ``year_split`` sources are rasterized once per acquisition year: each mask
    holds only the glaciers mapped in that year, with the other years' outlines
    burned as :data:`IGNORE_VALUE` so every glacier is seen exactly once across
    the years. ``year_column`` names the outline attribute the acquisition year
    is drawn from; it may hold a year directly or a full acquisition date.
    """

    key: str
    title: str
    description: str
    start: datetime
    end: datetime
    year_split: bool = False
    year_column: str = "year_acq"

    @property
    def collection_id(self) -> str:
        """STAC collection id, e.g. ``labels-sgi2016``."""
        return f"labels-{self.key.lower()}"


LABEL_SOURCES: dict[str, LabelSource] = {
    "PAULETAL2020": LabelSource(
        key="PAULETAL2020",
        title="Paul et al. (2020) Alpine glacier inventory",
        description=(
            "Glacier outlines of the European Alps derived from Sentinel-2 imagery "
            "acquired 2015-2017 (https://doi.org/10.5194/essd-12-1805-2020)."
        ),
        # Source imagery window of the inventory
        start=datetime(2015, 8, 1, tzinfo=UTC),
        end=datetime(2017, 10, 31, tzinfo=UTC),
        year_split=True,
        year_column="date",
    ),
    "SGI2016": LabelSource(
        key="SGI2016",
        title="Swiss Glacier Inventory 2016 (SGI2016-R2020)",
        description=(
            "Glacier outlines of Switzerland from aerial imagery acquired 2013-2018 "
            "(https://doi.org/10.3389/feart.2021.704189)."
        ),
        start=datetime(2013, 1, 1, tzinfo=UTC),
        end=datetime(2018, 12, 31, tzinfo=UTC),
        year_split=True,
    ),
    "SGI2023": LabelSource(
        key="SGI2023",
        title="Swiss Glacier Inventory 2023 (SGI2023-R2026)",
        description=(
            "Glacier outlines of Switzerland from aerial imagery acquired 2021-2024. "
            "Work in progress, for internal use only."
        ),
        start=datetime(2021, 1, 1, tzinfo=UTC),
        end=datetime(2024, 12, 31, tzinfo=UTC),
        year_split=True,
    ),
}
"""Glacier inventories available as label collections, keyed by filename token."""


@dataclass(frozen=True)
class LabelFile:
    """A parsed per-tile label mask raster."""

    path: Path | UPath
    source: str  # key into LABEL_SOURCES
    tile: str
    year: int | None  # acquisition year for year-split sources, else None

    @property
    def item_id(self) -> str:
        """STAC item id, e.g. ``32TMS25-sgi2016-2016``."""
        stem = f"{self.tile}-{self.source.lower()}"
        return stem if self.year is None else f"{stem}-{self.year}"


@dataclass(frozen=True)
class CompositeFile:
    """A parsed per-tile LRW or pass composite (or QA) raster."""

    path: Path | UPath
    product: str  # "COH12" or "RTC"
    qa: str | None  # None, "NUM", or "CQM"
    tile: str  # MGRS 10-km code, e.g. "32TMS55"
    polarization: str
    orbit: str | None  # None for LRW composites, "A" or "D" for pass composites
    start: datetime
    end: datetime

    @property
    def method(self) -> str:
        """Compositing-method token, e.g. ``"LRW"`` or ``"COMP_A"``."""
        return "LRW" if self.orbit is None else f"COMP_{self.orbit}"

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
        """Filename this composite takes in the store, e.g. ``coh12_vv.tif``.

        The date range leaves the filename — the directory carries the year and
        the item properties carry the window — so the published name is exactly
        the asset key.
        """
        return f"{self.asset_key}.tif"


def parse_composite_name(path: Path | UPath) -> CompositeFile | None:
    """Parse a composite filename; return ``None`` for non-matching names."""
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
        start=datetime.strptime(match["start"], "%Y%m%d").replace(tzinfo=UTC),
        end=datetime.strptime(match["end"], "%Y%m%d").replace(tzinfo=UTC),
    )


def discover_composites(comp_dir: Path | UPath) -> dict[str, list[CompositeFile]]:
    """Discover and parse all per-tile composite rasters in a directory."""
    by_tile: dict[str, list[CompositeFile]] = defaultdict(list)
    for path in comp_dir.glob("S1_*.tif"):
        parsed = parse_composite_name(path)
        if parsed is not None:
            by_tile[parsed.tile].append(parsed)
    logger.info("Discovered %d tiles in %s", len(by_tile), comp_dir)
    return dict(by_tile)


def require_unique_composites(by_tile: Mapping[str, Sequence[CompositeFile]]) -> None:
    """Reject multiple files that resolve to the same published name.

    A shorter date range can remain beside a newer composite when an earlier
    processing run skipped bursts. Neither filename is implicitly preferred:
    publishing one must be an explicit archive-maintenance decision, never an
    implicit overwrite of one by the other.

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


def discover_labels(labels_dir: Path | UPath) -> list[LabelFile]:
    """Discover and parse all label mask COGs in a directory, sorted by item id.

    Raises
    ------
    KeyError
        If a mask names an inventory absent from :data:`LABEL_SOURCES`.
    """
    labels: list[LabelFile] = []
    for path in labels_dir.glob("LABELS_*.tif"):
        match = _LABEL_RE.match(path.name)
        if match is None:
            continue
        if match["source"] not in LABEL_SOURCES:
            msg = f"Unknown label source {match['source']!r} in {path.name}"
            raise KeyError(msg)
        labels.append(
            LabelFile(
                path=path,
                source=match["source"],
                tile=match["tile"],
                year=int(match["year"]) if match["year"] else None,
            ),
        )
    logger.info("Discovered %d label masks in %s", len(labels), labels_dir)
    return sorted(labels, key=lambda label: label.item_id)


def check_completeness(
    by_tile: Mapping[str, Sequence[CompositeFile]],
    expected_tiles: Iterable[str] | None = None,
) -> dict[str, list[str]]:
    """Return missing asset keys per tile (empty dict means complete).

    Parameters
    ----------
    by_tile
        Output of :func:`discover_composites`.
    expected_tiles
        Tile codes that must be present. Defaults to the discovered tiles.
    """
    missing: dict[str, list[str]] = {}
    for tile in sorted(expected_tiles if expected_tiles is not None else by_tile):
        present = {f.asset_key for f in by_tile.get(tile, [])}
        gaps = [key for key in ASSET_KEYS if key not in present]
        if gaps:
            missing[tile] = gaps
    return missing
