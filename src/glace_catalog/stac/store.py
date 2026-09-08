"""The store on object storage: where its files go, and how they get there.

A GLACE store is one self-contained, relocatable tree holding the composite
COGs, the documents describing them, the annual mosaics and the web-map tiles.
This module owns the parts of that which touch the object store: the layout, the
run manifest that says what a year's composites are, the server-side copy that
moves them in, and the build state that makes a rerun safe.

Two rules run through all of it. Composites are **copied, never rewritten**, so
the bytes and the compression the run chose survive exactly; and every step
records an **input fingerprint** rather than a "done" flag, so a step whose
inputs moved reruns itself instead of letting a stale product through.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import logging
from secrets import token_hex
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
from upath import UPath

from glace_catalog.stac.naming import CompositeFile, parse_composite_name
from glace_catalog.stac.tags import PROVENANCE_TAGS, RasterInfo

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.parquet"
BUILD_STATE_NAME = ".build-state.json"
YEAR_INDEX_NAME = ".items.parquet"
ITEMS_INDEX_NAME = "items.parquet"
"""Filename of a collection's item mirror.

Fixed by convention rather than by the specification, which requires only that
the mirror be registered as a collection asset with the ``collection-mirror``
role. Tooling looks for this name, so a collection that uses it is found without
reading the collection document first.
"""

README_NAME = "README.md"
AGENTS_NAME = "AGENTS.md"
THUMBNAIL_NAME = "thumbnail.png"

TILES_DIR = "tiles"
MOSAICS_DIR = "mosaics"
"""Directory, and collection-id suffix, of each collection.

The scope prefixes these into the published ids (``glace-ch-tiles``), but the
directory stays unprefixed: the scope is already the store root, so repeating it
one level down would only make every href longer.
"""

STATISTICS_COLUMNS: dict[str, str] = {
    "minimum": "stat_minimum",
    "maximum": "stat_maximum",
    "mean": "stat_mean",
    "stddev": "stat_stddev",
    "valid_percent": "stat_valid_percent",
}
"""The embedded band statistics, as :attr:`RasterInfo.statistics` key to manifest column.

Read from the COG's own band tags at write time, so the publisher can put them on
``bands[].statistics`` without a second read of the header.
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

The composite driver in ``deep-glacier-mapping`` writes the manifest — these
columns and, after them, one column per GeoTIFF tag. Nothing here writes one:
an archive without a manifest gets one by rerunning its year, which skips every
complete tile and describes and hashes every file. ``tests/test_manifest_contract.py``
holds the driver's column list verbatim and is where drift on either side surfaces.

The tag columns are SCREAMING_CASE and these are lowercase, so the two
vocabularies cannot collide.

``sha256`` holds the **multihash-encoded** digest — ``1220`` followed by the
sha256 hex — because that is the form ``file:checksum`` requires, and a value
stored one way and published another invites exactly one bug.
"""


@dataclass(frozen=True)
class StoreLayout:
    """Where everything in one store lives.

    The tree is **collection-owned**: every document and every byte a collection
    publishes sits beneath that collection's directory. Portolan requires it
    (core: "a collection directory MUST contain a ``collection.json`` and holds
    one subdirectory per item"), and it is what lets a year catalog exist at all,
    since a catalog may appear below a collection to organize its items.

    ::

        {root}/
        |-- catalog.json  README.md  AGENTS.md  _assets/
        |-- tiles/
        |   |-- collection.json  README.md  AGENTS.md
        |   |-- items.parquet          # collection-mirror, every year
        |   |-- thumbnail.png
        |   |-- glacier-fractions.parquet
        |   `-- {year}/
        |       |-- catalog.json  README.md  AGENTS.md
        |       |-- manifest.parquet  config.yaml
        |       |-- .build-state.json  .items.parquet
        |       `-- {tile}/item.json + 12 COGs
        `-- mosaics/
            |-- collection.json  README.md  AGENTS.md
            |-- items.parquet  thumbnail.png  styles/
            |-- pmtiles/{year}/{layer}.pmtiles
            `-- {year}/item.json + 12 analysis COGs

    One thing is deliberately not mirrored from the old flat layout: there is no
    per-year directory shared by both collections, because a year is not a thing
    either collection owns.

    The web-map archives sit under ``mosaics/pmtiles/{year}/`` rather than beside
    the rasters they render, for two reasons. They are registered as
    *collection*-level links, so putting them inside an item's own directory
    would place a collection-level product under an item; and the stems would
    otherwise collide — ``coh12_vv.tif`` is linear float32 and
    ``coh12_vv.pmtiles`` is a pre-stretched 8-bit rendering, the backscatter ones
    in decibels. Two files one letter apart holding different quantities is the
    kind of thing that reads fine until someone compares them.

    Dotfiles are build state, never published: ``.build-state.json`` records what
    a step already did, and ``.items.parquet`` is the year's slice of the item
    mirror before :mod:`finalize-catalog` concatenates it. Naming them with a
    leading dot keeps them out of the item mirror's own directory listing and out
    of the publish sync.

    Parameters
    ----------
    root
        Store root, e.g. ``s3://s1-bursts/data/glace-ch`` or ``/mnt/glace/ch``.
    """

    root: Path | UPath

    # -- root ------------------------------------------------------------

    @property
    def catalog(self) -> Path | UPath:
        """Root catalog. The publish step stamps its absolute ``self`` link."""
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
    def logo(self) -> Path | UPath:
        """Catalog logo, linked ``rel: "icon"`` from the root catalog."""
        return self.root / "_assets" / "logo.png"

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
        which is the whole of Portolan's registration requirement: no
        ``rel: "items"`` link accompanies it.
        """
        return self.tiles / ITEMS_INDEX_NAME

    @property
    def tiles_thumbnail(self) -> Path | UPath:
        """Thumbnail of the tiles collection; a Portolan MUST for a geospatial one."""
        return self.tiles / THUMBNAIL_NAME

    @property
    def glacier_fractions(self) -> Path | UPath:
        """Per-tile RGI7 glacier fractions, pinned to one inventory version."""
        return self.tiles / "glacier-fractions.parquet"

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
        """Build state of one year's publication. Not published."""
        return self.tile_year(year) / BUILD_STATE_NAME

    def year_index(self, year: int) -> Path | UPath:
        """Locate the year's slice of the item mirror, with root-relative hrefs.

        A build artefact: ``finalize-catalog`` concatenates every year's slice
        into :attr:`tiles_index` and rebases the hrefs onto the public base URL.
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

    def pmtiles(self, year: int) -> Path | UPath:
        """Directory of one year's web-map archives.

        Deliberately not inside the year's item directory: these are registered
        as collection-level links, and keeping them apart stops
        ``coh12_vv.pmtiles`` sitting beside the ``coh12_vv.tif`` it renders.
        """
        return self.mosaics / "pmtiles" / str(year)


def read_manifest(path: Path | UPath) -> pd.DataFrame:
    """Read a run manifest, checking that it carries every expected column.

    Raises
    ------
    ValueError
        If columns the publisher depends on are absent.
    """
    with UPath(path).open("rb") as handle:
        frame = pd.read_parquet(handle)
    missing = [column for column in MANIFEST_COLUMNS if column not in frame.columns]
    if missing:
        msg = f"{path} is not a usable manifest: missing columns {', '.join(missing)}."
        raise ValueError(msg)
    return frame


def manifest_digest(frame: pd.DataFrame) -> str:
    """Content digest of a manifest, as the fingerprint the copy step keys on.

    Taken over the filenames, sizes and digests rather than the whole frame, so a
    rewritten but unchanged manifest does not invalidate a copy.
    """
    payload = frame.loc[:, ["filename", "size", "sha256"]].sort_values("filename")
    digest = hashlib.sha256()
    for record in payload.to_dict("records"):
        digest.update(
            "\t".join(str(record[column]) for column in payload.columns).encode() + b"\n",
        )
    return digest.hexdigest()


def list_objects(directory: Path | UPath) -> dict[str, int]:
    """List one prefix as ``{filename: size}``.

    One listing replaces one remote open per file, which is what makes checking
    a 26 500-object year affordable at all. Only the size is taken: an ETag is
    the MD5 for a single-part upload and something else entirely for a multipart
    one, so keying on it would silently constrain how every writer uploads.
    """
    directory = UPath(directory)
    entries: dict[str, int] = {}
    for info in directory.fs.ls(str(directory.path), detail=True):
        name = str(info["name"]).rsplit("/", 1)[-1]
        if not name:
            continue
        entries[name] = int(info.get("size") or info.get("Size") or 0)
    return entries


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

    Returns one record per disagreement, empty when the two match. The manifest
    is a cache, not the source of truth — every tag it lists is also written into
    the file — so this is what stops a stale cache describing an archive that
    has moved on.

    **This is a presence and size check, not an integrity check.** A listing
    returns sizes; verifying content means reading bytes, and the manifest's
    ``sha256`` is what does that — published as ``file:checksum`` on every asset
    and verified by ``rashid check --data`` before publication. Catching a
    substituted file of identical size is that pass's job, not this one's.
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

    This is what collapses the publisher's most expensive work: the survey that
    used to be one remote open per file becomes one read of the manifest.
    """
    info: dict[str, dict[str, RasterInfo]] = {}
    for row in frame.to_dict("records"):
        tags = {
            tag: str(row[tag])
            for tag in PROVENANCE_TAGS
            if tag in row and row[tag] is not None and pd.notna(row[tag])
        }
        statistics = {
            name: float(row[column])
            for name, column in STATISTICS_COLUMNS.items()
            if column in row and row[column] is not None and pd.notna(row[column])
        }
        checksum = row["sha256"]
        nodata = row["nodata"]
        info.setdefault(str(row["tile"]), {})[str(row["asset_key"])] = RasterInfo(
            tags=tags,
            band_name=str(row["band_name"] or ""),
            band_unit=str(row["band_unit"] or ""),
            data_type=str(row["data_type"]),
            # A NaN in the column *is* the nodata value: every float composite
            # declares NaN, and parquet has no way to say "none declared" apart
            # from it. Only a missing value is "none declared".
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
        percents.setdefault(tile, {})[key] = float(record["valid_percent"])  # type: ignore[arg-type]
    return percents


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

    Each file lands at ``{root}/{year}/{tile}/{asset_key}.tif`` through a
    ``CopyObject``: no download, no upload, no egress, and the bytes and the
    compression the run chose are preserved exactly.

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
    destinations = list_objects_recursive(layout.tile_year(year))
    # An object store needs no directory, but a local store does, and creating
    # them once up front keeps that out of the copy pool.
    for tile in sorted({file.tile for file in files}):
        layout.tile(year, tile).mkdir(parents=True, exist_ok=True)

    def copy_one(file: CompositeFile) -> bool:
        target = layout.tile(year, file.tile) / file.published_name
        key = f"{file.tile}/{file.published_name}"
        found = destinations.get(key)
        # Size alone is a weak content check, and deliberately all this step
        # does. It does not have to be strong: the copy below is a server-side
        # CopyObject, which is byte-exact by construction, so a destination of
        # the right size was produced by this same copy from this same source.
        # A source replaced behind our back is caught one level up, where the
        # build state reruns the whole step whenever the manifest digest moves.
        if found is not None and found == listing[file.path.name]:
            return False
        root.fs.cp(str(UPath(file.path).path), str(UPath(target).path))
        return True

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        copied = sum(pool.map(copy_one, files))
    result = CopyResult(copied=copied, skipped=len(files) - copied)
    logger.info("Copied %d composites into %s: %s", len(files), layout.tile_year(year), result)
    return result


def list_objects_recursive(directory: Path | UPath) -> dict[str, int]:
    """List one prefix and everything under it as ``{relative path: size}``."""
    directory = UPath(directory)
    prefix = str(directory.path).rstrip("/") + "/"
    entries: dict[str, int] = {}
    if not directory.fs.exists(str(directory.path)):
        return entries
    # `detail=True` makes fsspec return `{path: info}`; its signature declares
    # only the `detail=False` list, so the mapping has to be named here.
    listing = cast(
        "dict[str, dict[str, Any]]",
        directory.fs.find(str(directory.path), detail=True),
    )
    for info in listing.values():
        name = str(info["name"])
        if not name.startswith(prefix):
            continue
        entries[name[len(prefix) :]] = int(info.get("size") or info.get("Size") or 0)
    return entries


def upload_tree(local: Path, destination: Path | UPath) -> int:
    """Upload a locally staged directory tree in one pass.

    Everything a step writes is staged locally and uploaded once: writing 2 209
    small JSON files straight to the object store would be 2 209 round trips.
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
"""Steps that are always rerun.

The VRTs are local and ephemeral, so a remote "done" can be true while the cache
is empty. They take seconds to rebuild, so there is nothing to protect.
"""


@dataclass
class BuildState:
    """Per-step input fingerprints of one year's publication.

    Recording only outputs is not enough: if ``copy`` replaces a source, or the
    grid, bounds or style change, a state file saying "mosaics: done" would let a
    stale product through. Each step records what it was built *from*, and reruns
    itself exactly when that moved.

    State is written once, atomically, after every output of a step is verified,
    never appended per output — a crash halfway through ``pmtiles`` must leave
    the step incomplete rather than done with six of fourteen archives present.
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
        path = UPath(self.path)
        if path.protocol == "s3":
            # PutObject is atomic on its own.
            path.write_text(payload)
            return
        partial = path.with_name(f"{path.name}.{token_hex(8)}.partial")
        try:
            partial.write_text(payload)
            partial.replace(path)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

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
