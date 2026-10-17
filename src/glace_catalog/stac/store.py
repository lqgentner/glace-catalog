"""The store layout, the run manifest, the server-side copy and the build state.

A GLACE store is one self-contained, relocatable tree holding the composite
COGs, the documents describing them, the annual mosaics and the web-map tiles.
This module handles the parts that touch object storage: where each file goes,
the run manifest listing a year's composites, the server-side copy that brings
them in, and the build state that makes reruns safe.

Two rules apply throughout. Composites are copied byte for byte, keeping the
compression the run chose. Each step records a fingerprint of its inputs and
reruns whenever those inputs change.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from freezebase.s3 import atomic_write_text, list_object_sizes
from freezebase.utils import file_sha256
import pandas as pd
from upath import UPath

from glace_catalog.grid import GRID_ARCHIVE
from glace_catalog.stac.naming import CompositeFile, parse_composite_name, viz_filename
from glace_catalog.stac.tags import PROVENANCE_TAGS, RasterInfo, band_statistics

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.parquet"
BUILD_STATE_NAME = ".build-state.json"
YEAR_INDEX_NAME = ".items.parquet"
ITEMS_INDEX_NAME = "items.parquet"
"""Filename of a collection's item mirror.

The specification only requires the mirror to be a collection asset with the
``collection-mirror`` role; this filename is a convention. Tools look for it,
so they find the mirror without reading the collection document first.
"""

README_NAME = "README.md"
AGENTS_NAME = "AGENTS.md"
THUMBNAIL_NAME = "thumbnail.webp"
ASSETS_DIR = "_assets"
"""Root directory of files the catalog documents link to without describing them."""

TILES_DIR = "tiles"
MOSAICS_DIR = "mosaics"
"""Directory, and collection-id suffix, of each collection.

The scope prefixes these to form the published ids (``glace-ch-tiles``). The
directories themselves are unprefixed, because the store root already belongs
to one scope.
"""

STATISTICS_COLUMNS: dict[str, str] = {
    "minimum": "stat_minimum",
    "maximum": "stat_maximum",
    "mean": "stat_mean",
    "stddev": "stat_stddev",
    "valid_percent": "stat_valid_percent",
}
"""Manifest column of each embedded band statistic, by :class:`BandStatistics` field.

The upstream writer copies them from the COG's band tags, so the build
fills ``bands[].statistics`` from the manifest alone.
"""

MANIFEST_COLUMNS: tuple[str, ...] = (
    "filename",
    "tile",
    "asset_key",
    "product",
    "qa",
    "polarization",
    "composite_start",
    "composite_end",
    "size",
    "sha256",
    "valid_percent",
    "band_name",
    "band_unit",
    "data_type",
    "nodata",
    "spatial_resolution",
    "bits_per_sample",
    "byte_order",
    *STATISTICS_COLUMNS.values(),
)
"""The columns :func:`read_manifest` requires, before the one column per tag.

The composite driver in ``deep-glacier-mapping`` writes the manifest: these
columns, then one column per GeoTIFF tag. To create a manifest for an archive,
rerun its year upstream; complete tiles are skipped, and every file is
described and hashed. ``tests/test_manifest_contract.py`` holds the driver's
column list and catches drift on either side.

Tag columns are upper case and these are lower case, so names cannot collide.

``sha256`` holds the multihash-encoded digest (``1220`` followed by the sha256
hex), the form ``file:checksum`` publishes, so the stored and published values
are identical.
"""


@dataclass(frozen=True)
class StoreLayout:
    """Where everything in one store lives.

    Every document and file a collection publishes sits beneath that
    collection's directory. Portolan requires this ("a collection directory
    MUST contain a ``collection.json`` and holds one subdirectory per item").
    Year catalogs sit below a collection and group its items.

    ::

        {root}/
        |-- catalog.json  README.md  AGENTS.md  _assets/
        |-- tiles/
        |   |-- collection.json  README.md  AGENTS.md
        |   |-- items.parquet          # collection-mirror, every year
        |   |-- thumbnail.webp
        |   |-- grid.pmtiles           # tile grid with glacier fractions
        |   |-- styles/grid.json
        |   |-- .glacier-fractions.parquet  # build input, not published
        |   `-- {year}/
        |       |-- catalog.json  README.md  AGENTS.md
        |       |-- .build-state.json  .items.parquet
        |       `-- {tile}/item.json + 12 COGs
        `-- mosaics/
            |-- collection.json  README.md  AGENTS.md
            |-- items.parquet  thumbnail.webp
            |-- styles/default.json    # the latest year's style
            `-- {year}/item.json + 12 analysis COGs + 12 web-map archives
                `-- styles/default.json  # the year's style, an item asset

    Each web-map archive is an asset of its year's mosaic item, so it sits in
    that item's directory beside the raster it shows. The ``_viz`` suffix tells
    the two apart: ``coh12_vv.tif`` holds linear float32 values, while
    ``coh12_vv_viz.pmtiles`` holds 8-bit value codes for display (in
    decibels for backscatter and QA-CQM).

    Dotfiles are build state and are excluded from publication:
    ``.build-state.json`` records which steps have run, and ``.items.parquet``
    is the year's slice of the item mirror, which ``finalize-catalog``
    concatenates.

    Parameters
    ----------
    root
        Store root, e.g. ``s3://path/to/store`` or ``/path/to/store``.
    """

    root: Path | UPath

    # -- root ------------------------------------------------------------

    @property
    def catalog(self) -> Path | UPath:
        """Root catalog. Finalization stamps its absolute ``self`` link."""
        return self.root / "catalog.json"

    @property
    def readme(self) -> Path | UPath:
        """Root ``README.md``, linked ``rel: "describedby"``."""
        return self.root / README_NAME

    @property
    def agents(self) -> Path | UPath:
        """Root ``AGENTS.md``, linked ``rel: "agents"``."""
        return self.root / AGENTS_NAME

    @property
    def icon(self) -> Path | UPath:
        """Catalog wordmark, linked ``rel: "icon"`` from the root catalog.

        An SVG, so registries and browsers can draw it at any card size
        (``PTL-LNK-007``). The viewer's header uses the same file.
        """
        return self.root / ASSETS_DIR / "glace-wordmark.svg"

    # -- tiles collection ------------------------------------------------

    @property
    def tiles(self) -> Path | UPath:
        """Directory owning the per-tile collection."""
        return self.root / TILES_DIR

    @property
    def tiles_collection(self) -> Path | UPath:
        """Collection document of the per-tile items."""
        return self.tiles / "collection.json"

    @property
    def tiles_index(self) -> Path | UPath:
        """Item mirror of every tile item, across every year.

        Registered as a collection asset with the ``collection-mirror`` role,
        which is all Portolan requires.
        """
        return self.tiles / ITEMS_INDEX_NAME

    @property
    def tiles_thumbnail(self) -> Path | UPath:
        """Thumbnail of the tiles collection, which Portolan requires."""
        return self.tiles / THUMBNAIL_NAME

    @property
    def tiles_grid(self) -> Path | UPath:
        """Vector PMTiles of the tile grid, with each tile's glacier fractions."""
        return self.tiles / GRID_ARCHIVE

    @property
    def glacier_fractions(self) -> Path | UPath:
        """Per-tile glacier fractions, pinned to one inventory version.

        A build input: the dot keeps it out of the upload.
        """
        return self.tiles / ".glacier-fractions.parquet"

    def tile_year(self, year: int) -> Path | UPath:
        """Directory of the sub-catalog organizing one year's tile items."""
        return self.tiles / str(year)

    def tile_year_catalog(self, year: int) -> Path | UPath:
        """Sub-catalog listing one year's tile items."""
        return self.tile_year(year) / "catalog.json"

    def tile(self, year: int, tile: str) -> Path | UPath:
        """Directory of one tile item, holding its twelve COGs and its ``item.json``."""
        return self.tile_year(year) / tile

    def build_state(self, year: int) -> Path | UPath:
        """Build state of one year's publication, excluded from publication."""
        return self.tile_year(year) / BUILD_STATE_NAME

    def year_index(self, year: int) -> Path | UPath:
        """Locate the year's slice of the item mirror, with root-relative hrefs.

        A build file: ``finalize-catalog`` concatenates every year's slice into
        :attr:`tiles_index` and rebases the hrefs onto the public base URL.
        """
        return self.tile_year(year) / YEAR_INDEX_NAME

    # -- mosaics collection ----------------------------------------------

    @property
    def mosaics(self) -> Path | UPath:
        """Directory owning the annual-mosaic collection."""
        return self.root / MOSAICS_DIR

    @property
    def mosaics_collection(self) -> Path | UPath:
        """Collection document of the annual mosaic items."""
        return self.mosaics / "collection.json"

    @property
    def mosaics_index(self) -> Path | UPath:
        """Item mirror of the annual mosaic items, one row per year."""
        return self.mosaics / ITEMS_INDEX_NAME

    @property
    def mosaics_thumbnail(self) -> Path | UPath:
        """Thumbnail of the mosaics collection."""
        return self.mosaics / THUMBNAIL_NAME

    def mosaic_year(self, year: int) -> Path | UPath:
        """Directory of one year's mosaic item and its rasters."""
        return self.mosaics / str(year)

    def mosaic_item(self, year: int) -> Path | UPath:
        """Item document of one year's mosaics."""
        return self.mosaic_year(year) / "item.json"

    @property
    def styles(self) -> Path | UPath:
        """MapLibre GL style assets of the mosaics collection."""
        return self.mosaics / "styles"

    def viz(self, year: int, asset_key: str) -> Path | UPath:
        """One year's web-map archive of ``asset_key``, beside the COG it renders.

        Same directory, different stem: ``coh12_vv_viz.pmtiles`` next to
        ``coh12_vv.tif``. The archive is an asset of the year item with the
        ``visual`` role.
        """
        return self.mosaic_year(year) / viz_filename(asset_key)


def read_manifest(path: Path | UPath) -> pd.DataFrame:
    """Read a run manifest, checking that it carries every expected column.

    Raises
    ------
    ValueError
        If columns the build depends on are absent.
    """
    with UPath(path).open("rb") as handle:
        frame = pd.read_parquet(handle)
    missing = [column for column in MANIFEST_COLUMNS if column not in frame.columns]
    if missing:
        msg = f"{path} is not a usable manifest: missing columns {', '.join(missing)}."
        raise ValueError(msg)
    return frame


MULTIHASH_SHA256 = "1220"
"""Multihash prefix of a 32-byte sha256 digest, the form ``file:checksum`` requires."""


def multihash_sha256(payload: bytes) -> str:
    """Multihash-encoded sha256 of some bytes, the form ``file:checksum`` requires.

    ``1220`` is the multihash prefix for a 32-byte sha256 digest. The run
    manifest uses the same encoding for every composite.
    """
    return MULTIHASH_SHA256 + hashlib.sha256(payload).hexdigest()


def multihash_sha256_file(path: Path | UPath) -> str:
    """Multihash-encoded sha256 of a file, read as a stream.

    Same encoding as :func:`multihash_sha256`, for the files this repository
    writes. Streaming keeps memory low for annual mosaics of hundreds of
    megabytes.
    """
    return MULTIHASH_SHA256 + file_sha256(path)


def manifest_digest(frame: pd.DataFrame) -> str:
    """Content digest of a manifest, as the fingerprint the copy step keys on.

    Taken over the filenames, sizes and digests only, so a rewritten manifest
    with the same content keeps the copy current.
    """
    payload = frame.loc[:, ["filename", "size", "sha256"]].sort_values("filename")
    digest = hashlib.sha256()
    for record in payload.to_dict("records"):
        digest.update(
            "\t".join(str(record[column]) for column in payload.columns).encode() + b"\n",
        )
    return digest.hexdigest()


@dataclass(frozen=True)
class ManifestMismatch:
    """One file the manifest and the archive disagree about."""

    filename: str
    problem: str

    def __str__(self) -> str:
        """One log line: the file, then what disagrees about it."""
        return f"{self.filename}: {self.problem}"


def verify_manifest(
    frame: pd.DataFrame,
    listing: Mapping[str, int],
) -> list[ManifestMismatch]:
    """Compare a manifest against a listing of the archive it describes.

    Returns one record per disagreement, or an empty list when they match. The
    manifest caches what the files themselves record, so this check catches a
    manifest that is out of date with its archive.

    This checks presence and size only. File contents are verified through the
    manifest's ``sha256``, published as ``file:checksum`` on every asset and
    checked by ``rashid check --data`` before publication.
    """
    problems: list[ManifestMismatch] = []
    for record in frame.loc[:, ["filename", "size"]].to_dict("records"):
        name = str(record["filename"])
        recorded_size = int(record["size"])
        size = listing.get(name)
        if size is None:
            problems.append(ManifestMismatch(name, "listed in the manifest but absent"))
        elif recorded_size != size:
            problems.append(
                ManifestMismatch(name, f"size {size} on the archive, {recorded_size} recorded"),
            )
    extra = sorted(set(listing) - set(frame["filename"]))
    problems.extend(
        ManifestMismatch(name, "present in the archive but absent from the manifest")
        for name in extra
        if name.endswith(".tif")
    )
    return problems


def manifest_composites(frame: pd.DataFrame, src_dir: Path | UPath) -> list[CompositeFile]:
    """Reconstruct the parsed composites a manifest describes.

    Raises
    ------
    ValueError
        If a listed filename does not follow the composite naming convention.
    """
    files: list[CompositeFile] = []
    for name in frame["filename"]:
        parsed = parse_composite_name(src_dir / str(name))
        if parsed is None:
            msg = f"{name} in the manifest is not a composite filename."
            raise ValueError(msg)
        files.append(parsed)
    return files


def manifest_raster_info(frame: pd.DataFrame) -> dict[str, dict[str, RasterInfo]]:
    """Rebuild the per-asset raster metadata a manifest carries.

    One read of the manifest gives the metadata of every file, without
    opening the remote files.

    Raises
    ------
    ValueError
        If a row carries some but not all of the five ``stat_*`` columns.
    """
    info: dict[str, dict[str, RasterInfo]] = {}
    for row in frame.to_dict("records"):
        tags = {
            tag: str(row[tag])
            for tag in PROVENANCE_TAGS
            if tag in row and row[tag] is not None and pd.notna(row[tag])
        }
        statistics = band_statistics(
            {
                name: float(row[column])
                for name, column in STATISTICS_COLUMNS.items()
                if column in row and row[column] is not None and pd.notna(row[column])
            },
            names=STATISTICS_COLUMNS,
            source=f"{row['filename']} in the manifest",
        )
        checksum = row["sha256"]
        nodata = row["nodata"]
        info.setdefault(str(row["tile"]), {})[str(row["asset_key"])] = RasterInfo(
            tags=tags,
            band_name=str(row["band_name"] or ""),
            band_unit=str(row["band_unit"] or ""),
            data_type=str(row["data_type"]),
            # NaN in this column is the nodata value itself, which every float
            # composite declares. Only None means that no nodata is declared.
            nodata=None if nodata is None else float(nodata),
            spatial_resolution=float(row["spatial_resolution"]),
            bits_per_sample=int(row["bits_per_sample"]),
            byte_order=str(row["byte_order"]),
            size=int(row["size"]),
            checksum=None if checksum is None or pd.isna(checksum) else str(checksum),
            statistics=statistics,
        )
    return info


def manifest_valid_percents(frame: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Valid-data percentage per tile and data-asset key, as the manifest records it."""
    percents: dict[str, dict[str, float]] = {}
    columns = ["tile", "asset_key", "valid_percent"]
    for record in frame.loc[frame["valid_percent"].notna(), columns].to_dict("records"):
        tile, key = str(record["tile"]), str(record["asset_key"])
        percents.setdefault(tile, {})[key] = float(record["valid_percent"])
    return percents


EMPTY_COMPOSITE_LIMIT = 0.01
"""Largest share of a year's measurements that may hold no valid pixel.

A few empty composites are an edge effect: a burst assigned to a tile that
none of its pixels reach. More point at a broken run, which should stop the
build rather than thin out the catalog.
"""


def drop_empty_composites(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Remove each measurement with no valid pixel from a manifest, with its QA layers.

    The manifest's ``valid_percent`` of a measurement is missing or 0 when no
    pixel holds a value. Such a file is not published, and neither are the
    count and quality layers of the same tile, product and polarization. A
    tile left with no files gets no item.

    Returns
    -------
    tuple
        The remaining rows, and the ``{tile} {asset_key}`` of each dropped
        measurement.

    Raises
    ------
    ValueError
        If more than :data:`EMPTY_COMPOSITE_LIMIT` of the measurements are
        empty.
    """
    group = ["tile", "product", "polarization"]
    data = frame.loc[frame["qa"].isna()]
    empty = data.loc[data["valid_percent"].fillna(0) <= 0]
    if len(empty) > EMPTY_COMPOSITE_LIMIT * max(len(data), 1):
        msg = (
            f"{len(empty)} of {len(data)} measurements hold no valid pixel, more than "
            f"{EMPTY_COMPOSITE_LIMIT:.0%}; check the composite run"
        )
        raise ValueError(msg)
    dropped = frame.set_index(group).index.isin(empty.set_index(group).index)
    labels = sorted(f"{row.tile} {row.asset_key}" for row in empty.itertuples())
    return frame.loc[~dropped].reset_index(drop=True), labels


@dataclass(frozen=True)
class CopyResult:
    """What one ``copy`` step did."""

    copied: int
    skipped: int

    def __str__(self) -> str:
        """One log line summarising the copy."""
        return f"{self.copied} copied, {self.skipped} already present"


def copy_composites(
    files: Sequence[CompositeFile],
    layout: StoreLayout,
    year: int,
    listing: Mapping[str, int],
    *,
    max_workers: int = 16,
) -> CopyResult:
    """Copy a year's composites into the store, server-side.

    Each file is copied to ``{root}/tiles/{year}/{tile}/{asset_key}.tif`` with
    a server-side ``CopyObject``, so no data passes through this machine and
    the bytes and compression stay exactly as the run wrote them. When the
    archive and the store are on the same local disk, each file is hard-linked
    instead, so the store takes no extra space.

    Parameters
    ----------
    files
        Parsed composites to publish.
    layout
        Store the files are copied into.
    year
        Year directory the files belong to.
    listing
        Size per source filename, used as the idempotency key.
    max_workers
        Size of the copy thread pool.

    Returns
    -------
    CopyResult
        How many files were copied and how many were already in place.
    """
    root = UPath(layout.root)
    destinations = list_object_sizes(layout.tile_year(year), recursive=True)
    # A local store needs its directories; create them once, before the copy
    # pool starts.
    if isinstance(layout.root, Path):
        for tile in sorted({file.tile for file in files}):
            layout.tile(year, tile).mkdir(parents=True, exist_ok=True)

    def copy_one(file: CompositeFile) -> bool:
        target = layout.tile(year, file.tile) / file.published_name
        key = f"{file.tile}/{file.published_name}"
        found = destinations.get(key)
        # A destination of the right size counts as copied: the server-side
        # CopyObject is byte-exact, so it came from this same copy. A changed
        # source changes the manifest digest, which reruns the whole step
        # (see BuildState).
        if found is not None and found == listing[file.path.name]:
            return False
        linked = (
            isinstance(file.path, Path)
            and isinstance(target, Path)
            and hard_link(file.path, target)
        )
        if not linked:
            root.fs.cp_file(str(file.path), str(target))
        return True

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        copied = sum(pool.map(copy_one, files))
    result = CopyResult(copied=copied, skipped=len(files) - copied)
    logger.info("Copied %d composites into %s: %s", len(files), layout.tile_year(year), result)
    return result


def hard_link(source: Path, target: Path) -> bool:
    """Hard-link ``target`` to ``source``, replacing whatever is there.

    The link is made under a temporary name and moved into place, so a failed
    or interrupted link leaves the previous file untouched.

    Returns
    -------
    bool
        False if the two paths cannot share a link, for example because they
        are on different disks; the caller then copies the file.
    """
    staged = target.with_name(f".{target.name}.link")
    staged.unlink(missing_ok=True)
    try:
        staged.hardlink_to(source)
    except OSError:
        return False
    staged.replace(target)
    return True


def upload_tree(local: Path, destination: Path | UPath) -> int:
    """Upload every file of a locally staged directory tree.

    Steps write their outputs to a local directory first and upload them here
    once complete.
    """
    destination = UPath(destination)
    sources = sorted(path for path in local.rglob("*") if path.is_file())
    for path in sources:
        target = destination / path.relative_to(local).as_posix()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    logger.info("Uploaded %d files to %s", len(sources), destination)
    return len(sources)


STEPS: tuple[str, ...] = ("copy", "vrt", "mosaics", "pmtiles", "items")
"""The publication steps, in the order they must run."""

_UNFINGERPRINTED = frozenset({"vrt"})
"""Steps that always rerun.

The VRTs live in a local scratch cache, which may be empty even when the store
records the step as done. They rebuild in seconds.
"""


@dataclass
class BuildState:
    """Per-step input fingerprints of one year's publication.

    Each step records what it was built from (sources, grid, bounds, style
    settings) and reruns exactly when that changes.

    The state is written atomically once all of a step's outputs are verified,
    so an interrupted step stays incomplete.
    """

    path: Path | UPath
    fingerprints: dict[str, str] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | UPath) -> BuildState:
        """Read the state beside a published year, or start an empty one."""
        path = UPath(path)
        if not path.exists():
            return cls(path=path)
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("Ignoring unreadable build state %s: %s", path, error)
            return cls(path=path)
        return cls(
            path=path,
            fingerprints=dict(payload.get("fingerprints", {})),
            details=dict(payload.get("details", {})),
        )

    def is_current(self, step: str, fingerprint: str) -> bool:
        """Whether ``step`` has already run against exactly these inputs."""
        if step in _UNFINGERPRINTED:
            return False
        return self.fingerprints.get(step) == fingerprint

    def record(self, step: str, fingerprint: str, detail: Any = None) -> None:
        """Mark a step complete against its inputs and persist the whole state."""
        self.fingerprints[step] = fingerprint
        if detail is not None:
            self.details[step] = detail
        self.save()

    def save(self) -> None:
        """Write the state atomically."""
        payload = json.dumps(
            {"fingerprints": self.fingerprints, "details": self.details},
            indent=2,
            sort_keys=True,
            default=str,
        )
        atomic_write_text(self.path, payload)

    def invalidate_from(self, step: str) -> None:
        """Drop the fingerprint of ``step`` and of every step that follows it."""
        index = STEPS.index(step)
        for later in STEPS[index:]:
            self.fingerprints.pop(later, None)
            self.details.pop(later, None)


def fingerprint(*parts: Any) -> str:
    """Stable digest over the values a step's output depends on."""
    payload = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def resolve_steps(requested: Iterable[str]) -> list[str]:
    """Order and validate the requested steps.

    Raises
    ------
    ValueError
        If a step is not one of :data:`STEPS`.
    """
    wanted = set(requested)
    unknown = sorted(wanted - set(STEPS))
    if unknown:
        msg = f"Unknown step(s) {', '.join(unknown)}; known steps are {', '.join(STEPS)}."
        raise ValueError(msg)
    return [step for step in STEPS if step in wanted]
