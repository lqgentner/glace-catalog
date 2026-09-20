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

"""Label value for a pixel that must be excluded from loss and metrics."""

ASSET_KEYS: tuple[str, ...] = tuple(
    f"{product.lower()}_{pol.lower()}{suffix}"
    for product in PRODUCTS
    for pol in POLARIZATIONS
    for suffix in ("", "_qa_num", "_qa_cqm")
)
"""The twelve LRW asset keys a complete tile or mosaic item carries."""


def published_name(asset_key: str) -> str:
    """Filename an asset key takes in the store, e.g. ``coh12_vv.tif``.

    The date range leaves the filename — the directory carries the year and the
    item properties carry the window — so the published name is exactly the
    asset key. Every writer and every reader of a store raster goes through
    here, so the convention has one definition rather than one per call site.
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
_LABEL_RE = re.compile(
    r"^LABELS_(?P<source>[A-Z0-9-]+)"
    r"(?:_(?P<year>\d{4}))?"
    r"_(?P<tile>\d{2}[A-Z]{3}\d{2})\.tif$",
)


"""Glacier inventories available as label collections, keyed by filename token."""


@dataclass(frozen=True)
class CompositeFile:
    """A parsed per-tile LRW or pass composite (or QA) raster."""

    path: Path | UPath
    product: str  # "COH12" or "RTC"
    qa: str | None  # None, "NUM", or "CQM"
    tile: str  # MGRS 10-km code, e.g. "32TMS55"
    polarization: str
    orbit: str | None  # None for LRW composites, "A" or "D" for pass composites

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
        """Filename this composite takes in the store, e.g. ``coh12_vv.tif``."""
        return published_name(self.asset_key)


VIZ_SUFFIX = "_viz"
"""Suffix that marks a rendering: ``coh12_vv_viz.pmtiles`` is the web-map
archive of ``coh12_vv.tif``. The two sit in one directory, so the stem has to
say which is the measurement and which is the picture."""


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
    """Sort key ordering composite files by the asset key they publish under.

    A named function rather than a lambda so the parameter carries a type: every
    caller sorts the same records the same way.
    """
    return file.asset_key


def parse_composite_name(path: Path | UPath) -> CompositeFile | None:
    """Parse a composite filename; return ``None`` for non-matching names.

    The dates in the name identify the file and are matched, not read: the
    compositing window a year publishes comes from the run manifest, the one
    source of every fact about a raster, see ``composite_window``.
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

    A composite is named with its compositing window, so a rerun of the same
    window overwrites its file and a changed window writes a second one beside
    it. Neither filename is implicitly preferred: publishing one must be an
    explicit archive-maintenance decision, never an implicit overwrite of one
    by the other.

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
