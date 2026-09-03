"""What one composite says about itself, and whether that is enough to publish.

Everything the catalogue asserts about how a composite was made and how its
pixels are to be read comes from that file's own GeoTIFF tags, which ``s1bursts``
writes on the branch that actually ran. The vocabulary follows the CEOS-ARD
Product Family Specification for SAR v1.3.1 so that terms can be looked up
there.

Nothing here is optional: a composite that cannot say how it was made is a gap
in the archive rather than a field to omit, and a raster storing decibels is
refused outright, because the specification asks for that conversion to be
declared rather than applied.
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from freezebase.raster import rasterio_open
import numpy as np

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from upath import UPath

    from glace_catalog.stac.naming import CompositeFile

logger = logging.getLogger(__name__)

SPECIFICATION_URL = "https://ceos.org/ard/files/PFS/SAR/v1.3.1/CEOS-ARD_PFS_SAR_v1.3.1.pdf"
METADATA_VOCABULARY_NOTE = (
    "Product metadata borrows the vocabulary of the CEOS-ARD Product Family "
    "Specification for SAR v1.3.1, so that terms can be looked up there. "
    "Full conformance is neither assessed nor claimed. "
)
"""What the collection says about its relationship to the specification.

The products state the same thing per file in their ``METADATA_VOCABULARY``
tag. Saying it once here is what a reader of the catalogue encounters.
"""

# Tags whose presence means the raster stores decibels. The specification asks
# for the conversion to be declared rather than applied, so the published
# dataset is linear and the catalogue refuses to describe anything else.
_DECIBEL_TAGS = ("BACKSCATTER_SCALING", "CQM_SCALING")

PROVENANCE_EXPECTATION = (
    "The catalogue describes only fully tagged, linear products: CEOS-ARD PFS SAR items "
    "3.1, 2.21 and 2.22 leave the decibel conversion to the user, and a composite that "
    "cannot say how it was made is a gap in the archive rather than a field to omit."
)
"""Why a raster was refused, appended to every provenance failure."""

UNKNOWN_TAG_VALUE = "N/A"
"""``s1bursts`` sentinel for a provenance tag it considered but could not fill.

Meaningful in a standalone file, where it separates "asked and unanswered" from
"never written". The catalogue refuses it: a published archive whose composites
cannot say which SNAP built them has a provenance gap, and the catalogue is
where that gap should surface rather than be papered over with an absent field.
"""

# Tags every composite and QA layer carries, whatever it is. Absence means the
# raster was written by a pipeline older than the tag, which is exactly the
# drift the build is meant to catch.
_REQUIRED_TAGS = (
    "METADATA_VOCABULARY",
    "PIPELINE_SOFTWARE",
    "PIPELINE_SOFTWARE_VERSION",
    "PIPELINE_SOFTWARE_URL",
    "PROCESSING_SOFTWARE",
    "PROCESSING_SOFTWARE_VERSION",
    "DEM",
    "PRODUCT_TYPE",
    "PRODUCT_FAMILY",
    "PRODUCT_LEVEL",
    "SOURCE_DATA_PRODUCT_LEVEL",
    "MGRS_TILE",
    "COMPOSITE_START_DATE",
    "COMPOSITE_END_DATE",
    "POLARIZATION",
    "PIXEL_COORDINATE_CONVENTION",
    "COMPOSITING_METHOD",
    "COMPOSITING_PARAMETERS",
    "CONTRIBUTING_OBSERVATIONS",
    "LAYER_NAME",
    "LAYER_DESCRIPTION",
)
# A pass composite applies no weighting, so it cites no method reference; only
# it carries an orbit direction, the LRW composites spanning both.
_REQUIRED_LRW_TAGS = ("COMPOSITING_METHOD_REFERENCE",)
_REQUIRED_PASS_TAGS = ("ORBIT_DIRECTION",)
# Measurement vocabulary, on the data layers only: a QA layer describes the
# composite rather than the quantity, and carries neither set.
_REQUIRED_BACKSCATTER_TAGS = (
    "BACKSCATTER_MEASUREMENT",
    "BACKSCATTER_CONVENTION",
    "BACKSCATTER_CONVERSION_EQ",
)
# The estimation window reaches a composite only through its inputs' tags, so
# requiring it here is what proves that route stayed connected. The coherence
# statistics are not required: an all-nodata tile legitimately has none.
_REQUIRED_COHERENCE_TAGS = (
    "MEASUREMENT_TYPE",
    "TEMPORAL_BASELINE_DAYS",
    "PHASE_QUALITY_METHOD",
    "PHASE_QUALITY_REFERENCE",
    "COHERENCE_WINDOW_RANGE",
    "COHERENCE_WINDOW_AZIMUTH",
    "COHERENCE_WEIGHTING_SHAPE",
)
_REQUIRED_QA_TAGS = ("QA_LAYER",)
_REQUIRED_CQM_TAGS = ("CQM_CONVERSION_EQ",)

# Software name tag -> version tag
_SOFTWARE_TAGS = (
    ("PIPELINE_SOFTWARE", "PIPELINE_SOFTWARE_VERSION"),
    ("PROCESSING_SOFTWARE", "PROCESSING_SOFTWARE_VERSION"),
)

# GeoTIFF tag -> STAC asset field
_TAG_FIELDS: dict[str, str] = {
    "METADATA_VOCABULARY": "glace:metadata_vocabulary",
    "PRODUCT_FAMILY": "glace:product_family",
    "PIXEL_COORDINATE_CONVENTION": "glace:pixel_coordinate_convention",
    "COMPOSITE_START_DATE": "glace:composite_start_date",
    "COMPOSITE_END_DATE": "glace:composite_end_date",
    # The sat extension's `sat:orbit_state` would be the standard home for
    # this, but its schema requires at least one `sat:` field in the item
    # properties, and a GLACE item mixes LRW composites (both directions) with
    # per-pass ones. An asset-level-only declaration is schema-invalid.
    "ORBIT_DIRECTION": "glace:orbit_direction",
    "PRODUCT_TYPE": "glace:product_type",
    "SOURCE_DATA_PRODUCT_LEVEL": "glace:source_data_product_level",
    "PIPELINE_SOFTWARE_URL": "glace:pipeline_software_url",
    "BACKSCATTER_MEASUREMENT": "glace:backscatter_measurement",
    "BACKSCATTER_CONVENTION": "glace:backscatter_convention",
    "BACKSCATTER_CONVERSION_EQ": "glace:backscatter_conversion_eq",
    "CQM_CONVERSION_EQ": "glace:cqm_conversion_eq",
    "QA_LAYER": "glace:qa_layer",
    "COMPOSITING_METHOD": "glace:compositing_method",
    "COMPOSITING_METHOD_REFERENCE": "glace:compositing_method_reference",
    "COMPOSITING_PARAMETERS": "glace:compositing_parameters",
    "CONTRIBUTING_OBSERVATIONS": "glace:contributing_observations",
    "MEASUREMENT_TYPE": "glace:measurement_type",
    "TEMPORAL_BASELINE_DAYS": "glace:temporal_baseline_days",
    "PHASE_QUALITY_METHOD": "glace:phase_quality_method",
    "PHASE_QUALITY_REFERENCE": "glace:phase_quality_reference",
    "COHERENCE_WINDOW_RANGE": "glace:coherence_window_range",
    "COHERENCE_WINDOW_AZIMUTH": "glace:coherence_window_azimuth",
    "COHERENCE_WEIGHTING_SHAPE": "glace:coherence_weighting_shape",
    "COHERENCE_AVERAGE": "glace:coherence_average",
    "COHERENCE_STD": "glace:coherence_std",
}
PROVENANCE_TAGS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *_REQUIRED_TAGS,
            *_REQUIRED_LRW_TAGS,
            *_REQUIRED_PASS_TAGS,
            *_REQUIRED_BACKSCATTER_TAGS,
            *_REQUIRED_COHERENCE_TAGS,
            *_REQUIRED_QA_TAGS,
            *_REQUIRED_CQM_TAGS,
            "LAYER_DESCRIPTION",
            "COHERENCE_AVERAGE",
            "COHERENCE_STD",
        ),
    ),
)
"""Every provenance tag a composite may carry, in declaration order.

The manifest reserves one column per tag, so a run's record and the raster's own
tags can be compared field by field. Which of them a given raster must carry is
:func:`required_tags`; the rest are legitimately absent on some products.
"""

_INT_TAGS = frozenset(
    {
        "CONTRIBUTING_OBSERVATIONS",
        "TEMPORAL_BASELINE_DAYS",
        "COHERENCE_WINDOW_RANGE",
        "COHERENCE_WINDOW_AZIMUTH",
    },
)
_FLOAT_TAGS = frozenset({"COHERENCE_AVERAGE", "COHERENCE_STD"})


@dataclass(frozen=True)
class RasterInfo:
    """What one raster says about itself: its tags, band and file layout.

    Read once per file and reused for every catalogue field that describes the
    pixels, so the catalogue cannot assert a convention the pipeline did not
    write.
    """

    tags: dict[str, str] = field(default_factory=dict)
    band_name: str = ""
    band_unit: str = ""
    data_type: str = "float32"
    nodata: float | None = None
    spatial_resolution: float = 10.0
    bits_per_sample: int = 32
    byte_order: str = "little-endian"
    size: int | None = None
    checksum: str | None = None
    """Multihash-encoded sha256 of the file, as ``file:checksum`` requires."""
    statistics: dict[str, float] = field(default_factory=dict)
    """Embedded band statistics, as ``minimum``/``maximum``/``mean``/``stddev``/
    ``valid_percent``.

    Portolan raises these to a MUST (``PTL-DAT-009``, and ``PTL-DAT-010`` for the
    valid percent wherever a band declares nodata), so an empty mapping here is a
    raster that cannot be published, not a field that happens to be absent.
    """

    @property
    def is_decibel(self) -> bool:
        """Whether the stored values are decibels rather than linear."""
        return any(tag in self.tags for tag in _DECIBEL_TAGS) or self.band_unit == "dB"


def read_raster_info(path: Path | UPath) -> RasterInfo:
    """Read the tags, band metadata and file layout of one raster.

    Parameters
    ----------
    path
        Local path or object-storage URI of a single-band COG.

    Returns
    -------
    RasterInfo
        Everything the catalogue needs about this file besides its pixels.

    Raises
    ------
    ValueError
        If the file does not start with a TIFF byte-order marker.
    """
    with rasterio_open(path) as src:
        info = {
            "tags": dict(src.tags()),
            "band_name": src.descriptions[0] or "",
            "band_unit": src.units[0] or "",
            "data_type": src.dtypes[0],
            "nodata": src.nodatavals[0],
            "spatial_resolution": abs(src.res[0]),
            "bits_per_sample": np.dtype(src.dtypes[0]).itemsize * 8,
        }
    with path.open("rb") as handle:
        marker = handle.read(2)
    if marker not in (b"II", b"MM"):
        msg = f"{path} does not start with a TIFF byte-order marker, got {marker!r}."
        raise ValueError(msg)
    return RasterInfo(
        **info,
        byte_order="little-endian" if marker == b"II" else "big-endian",
        size=path.stat().st_size,
    )


def collect_raster_info(
    by_tile: dict[str, list[CompositeFile]],
    *,
    max_workers: int = 8,
) -> dict[str, dict[str, RasterInfo]]:
    """Read :func:`read_raster_info` for every discovered composite, in parallel.

    Parameters
    ----------
    by_tile
        Output of :func:`discover_composites`.
    max_workers
        Size of the reader thread pool.

    Returns
    -------
    dict[str, dict[str, RasterInfo]]
        Raster metadata per tile and asset key.
    """
    jobs = [(tile, file) for tile, files in by_tile.items() for file in files]
    results: dict[str, dict[str, RasterInfo]] = defaultdict(dict)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        infos = pool.map(lambda job: read_raster_info(job[1].path), jobs)
        for (tile, file), info in zip(jobs, infos, strict=True):
            results[tile][file.asset_key] = info
    logger.info("Read metadata from %d rasters", len(jobs))
    return dict(results)


@dataclass(frozen=True)
class RasterProblems:
    """One raster that the catalogue refuses to describe, and why."""

    file: CompositeFile
    problems: list[str]

    def __str__(self) -> str:
        """One log line: the path, then every problem found with it."""
        return f"{self.file.path}: {'; '.join(self.problems)}"


def survey_tags(
    by_tile: dict[str, list[CompositeFile]],
    raster_info: Mapping[str, Mapping[str, RasterInfo]],
) -> list[RasterProblems]:
    """Check the provenance of every discovered composite, without building anything.

    Where :func:`build_item` stops at the first raster it cannot describe, this
    walks the whole archive and returns one record per bad raster, so a single
    pass says everything that needs fixing.

    Parameters
    ----------
    by_tile
        Output of :func:`discover_composites`.
    raster_info
        Output of :func:`collect_raster_info`.

    Returns
    -------
    list[RasterProblems]
        One record per raster with a problem, tile order then asset order.
        Empty when the archive is intact.
    """
    found: list[RasterProblems] = []
    for tile, files in sorted(by_tile.items()):
        infos = raster_info.get(tile, {})
        for file in sorted(files, key=lambda f: f.asset_key):
            info = infos.get(file.asset_key)
            problems = (
                ["no raster metadata was read"] if info is None else tag_problems(file, info)
            )
            if problems:
                found.append(RasterProblems(file=file, problems=problems))
    return found


def valid_percent_from_qa(qa_num_path: Path | UPath) -> float:
    """Valid-data percentage of a composite, from its QA-NUM raster."""
    with rasterio_open(qa_num_path) as src:
        num_obs = src.read(1)
    return float(np.count_nonzero(num_obs) / num_obs.size * 100.0)


def required_tags(file: CompositeFile) -> tuple[str, ...]:
    """Tags this particular product must carry, by product family and layer."""
    required: tuple[str, ...] = _REQUIRED_TAGS
    required += _REQUIRED_LRW_TAGS if file.orbit is None else _REQUIRED_PASS_TAGS
    if file.qa is None:
        required += (
            _REQUIRED_COHERENCE_TAGS
            if file.product.startswith("COH")
            else _REQUIRED_BACKSCATTER_TAGS
        )
    else:
        required += _REQUIRED_QA_TAGS
        if file.qa == "CQM":
            required += _REQUIRED_CQM_TAGS
    return required


def tag_problems(file: CompositeFile, info: RasterInfo) -> list[str]:
    """Everything wrong with one raster's provenance, as short phrases.

    Returns an empty list for a raster the catalogue can describe. Shared by
    :func:`survey_tags`, which reports across a whole archive, and by the asset
    builders, which refuse to describe a raster that fails here.
    """
    if "PIPELINE_SOFTWARE" not in info.tags:
        # An untagged raster predates the tagged products altogether. Listing
        # every absent tag on top of that would bury the one fact that matters.
        # PIPELINE_SOFTWARE is the sentinel because `s1bursts` stamps it fresh
        # on every write and never inherits it, so its absence is unambiguous
        # and it survives any rewording of the vocabulary tag.
        return ["untagged: no PIPELINE_SOFTWARE, so nothing states how its pixels are scaled"]
    problems = []
    if info.is_decibel:
        evidence = ", ".join(tag for tag in _DECIBEL_TAGS if tag in info.tags) or "band unit dB"
        problems.append(f"stores decibels ({evidence})")
    missing = [tag for tag in required_tags(file) if tag not in info.tags]
    if missing:
        problems.append(f"missing {', '.join(missing)}")
    unknown = sorted(tag for tag, value in info.tags.items() if value == UNKNOWN_TAG_VALUE)
    if unknown:
        problems.append(f"{', '.join(unknown)} recorded as {UNKNOWN_TAG_VALUE!r}")
    return problems


def require_valid(file: CompositeFile, info: RasterInfo) -> None:
    """Reject a raster whose pixels or provenance this catalogue cannot describe.

    Raises
    ------
    ValueError
        If :func:`tag_problems` finds anything. Use :func:`survey_tags` to see
        every bad raster in an archive rather than stopping at the first.
    """
    problems = tag_problems(file, info)
    if problems:
        msg = f"{file.path}: {'; '.join(problems)}. {PROVENANCE_EXPECTATION}"
        raise ValueError(msg)


def tag_fields(raster_tags: Mapping[str, str]) -> dict[str, Any]:
    """Map the GeoTIFF tags onto ``glace:`` asset fields, typing the numeric ones."""
    fields: dict[str, Any] = {}
    for tag, name in _TAG_FIELDS.items():
        value = raster_tags.get(tag)
        if value is None:
            continue
        if tag in _INT_TAGS:
            fields[name] = int(value)
        elif tag in _FLOAT_TAGS:
            fields[name] = float(value)
        else:
            fields[name] = value
    return fields


def standard_fields(raster_tags: Mapping[str, str]) -> dict[str, Any]:
    """Map the GeoTIFF tags onto fields published extensions already define.

    Kept apart from :func:`tag_fields` because ``processing:software`` is
    somebody else's vocabulary with somebody else's shape: a name-to-version
    map built from two tags, not a string copied across.
    """
    fields: dict[str, Any] = {}
    software = {}
    for name_tag, version_tag in _SOFTWARE_TAGS:
        name, version = raster_tags.get(name_tag), raster_tags.get(version_tag)
        if name is None or version is None:
            continue
        software[name] = version
    if software:
        fields["processing:software"] = software
    return fields
