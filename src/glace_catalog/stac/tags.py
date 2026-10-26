"""Read the provenance a composite records about itself, and check it.

Everything the catalogue states about how a composite was made, and how its
pixels are read, comes from the file's own GeoTIFF tags, written by
``s1bursts``. The vocabulary follows the CEOS-ARD Product Family Specification
for SAR v1.3.1, so terms can be looked up there.

Every provenance tag is required, and a composite missing one is reported as a
gap in the archive. Rasters must store linear values: the specification leaves
the conversion to decibels to the user, so a raster in decibels is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from glace_catalog.stac.naming import by_asset_key

if TYPE_CHECKING:
    from collections.abc import Mapping

    from glace_catalog.stac.naming import CompositeFile

logger = logging.getLogger(__name__)

SPECIFICATION_URL = "https://ceos.org/ard/files/PFS/SAR/v1.3.1/CEOS-ARD_PFS_SAR_v1.3.1.pdf"
# Tags whose presence means the raster stores decibels. Published rasters must
# be linear (see the module docstring).
_DECIBEL_TAGS = ("BACKSCATTER_SCALING", "CQM_SCALING")

PROVENANCE_EXPECTATION = (
    "The catalogue describes only fully tagged, linear products: CEOS-ARD PFS SAR items "
    "3.1, 2.21 and 2.22 leave the decibel conversion to the user, and a composite that "
    "cannot say how it was made is a gap in the archive rather than a field to omit."
)
"""Why a raster was refused, appended to every provenance failure."""

UNKNOWN_TAG_VALUE = "N/A"
"""``s1bursts`` placeholder for a provenance tag it could not fill.

In a standalone file it marks a value as unknown. The catalogue treats it as a
provenance gap and rejects the raster, so the gap is reported.
"""

# Tags every composite and QA layer carries. A missing tag means the raster
# came from an older pipeline version.
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
    "FIRST_ACQUISITION_DATE",
    "LAST_ACQUISITION_DATE",
    "POLARIZATION",
    "PIXEL_COORDINATE_CONVENTION",
    "COMPOSITING_METHOD",
    "COMPOSITING_PARAMETERS",
    "CONTRIBUTING_OBSERVATIONS",
    "LAYER_NAME",
    "LAYER_DESCRIPTION",
)
# LRW composites cite their weighting method; pass composites record their
# pass direction.
_REQUIRED_LRW_TAGS = ("COMPOSITING_METHOD_REFERENCE",)
_REQUIRED_PASS_TAGS = ("PASS_DIRECTION",)
# Measurement tags, on data layers only. QA layers describe the composite
# itself and carry the QA tags below.
_REQUIRED_BACKSCATTER_TAGS = (
    "BACKSCATTER_MEASUREMENT",
    "BACKSCATTER_CONVENTION",
    "BACKSCATTER_CONVERSION_EQ",
)
# A composite inherits the estimation window from its inputs' tags, so
# requiring it checks that those tags were passed on. The coherence statistics
# are optional, because an all-nodata tile has none.
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

# GeoTIFF tag -> STAC asset field, for the tags whose value is this file's own.
# The compositing window and the acquisition dates are published as item
# properties instead.
_FILE_TAG_FIELDS: dict[str, str] = {
    # An asset field, because one item mixes LRW composites (both directions)
    # with per-pass ones. The name follows CEOS-ARD PFS SAR.
    "PASS_DIRECTION": "glace:pass_direction",
    "CONTRIBUTING_OBSERVATIONS": "glace:contributing_observations",
    "COHERENCE_AVERAGE": "glace:coherence_average",
    "COHERENCE_STD": "glace:coherence_std",
}
# GeoTIFF tag -> STAC asset field, for the tags every raster of an asset key
# carries with the same value. The tiles collection's item_assets declare them
# once (metadata/assets.yaml), and the build checks each raster against them.
SHARED_TAG_FIELDS: dict[str, str] = {
    "METADATA_VOCABULARY": "glace:metadata_vocabulary",
    "PRODUCT_FAMILY": "glace:product_family",
    "PIXEL_COORDINATE_CONVENTION": "glace:pixel_coordinate_convention",
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
    "MEASUREMENT_TYPE": "glace:measurement_type",
    "TEMPORAL_BASELINE_DAYS": "glace:temporal_baseline_days",
    "PHASE_QUALITY_METHOD": "glace:phase_quality_method",
    "PHASE_QUALITY_REFERENCE": "glace:phase_quality_reference",
    "COHERENCE_WINDOW_RANGE": "glace:coherence_window_range",
    "COHERENCE_WINDOW_AZIMUTH": "glace:coherence_window_azimuth",
    "COHERENCE_WEIGHTING_SHAPE": "glace:coherence_weighting_shape",
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

The manifest has one column per tag, so a run's record can be compared with the
raster's own tags field by field. :func:`required_tags` gives the tags a given
raster must carry; the others apply to some products only.
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
class BandStatistics:
    """The five statistics one band publishes, under the raster extension's names.

    Portolan requires all five (``PTL-DAT-009``, and ``PTL-DAT-010`` for the
    valid percent on bands with a nodata value). :func:`band_statistics`
    builds this record and raises ``ValueError`` on a partial set, as soon as
    the manifest is read.

    Parameters
    ----------
    minimum, maximum, mean, stddev
        Value statistics over the band's valid pixels, in the band's own units.
    valid_percent
        Percentage of the band's pixels that are not nodata.
    """

    minimum: float
    maximum: float
    mean: float
    stddev: float
    valid_percent: float

    def to_dict(self) -> dict[str, float]:
        """Render as the ``bands[].statistics`` object of the raster extension."""
        return {
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.mean,
            "stddev": self.stddev,
            "valid_percent": self.valid_percent,
        }


def band_statistics(
    values: Mapping[str, float],
    *,
    names: Mapping[str, str],
    source: str,
) -> BandStatistics | None:
    """Assemble the five band statistics, refusing a partial record.

    Parameters
    ----------
    values
        The statistics found, keyed by :class:`BandStatistics` field.
    names
        Each field's name in the source (a GDAL band tag or a manifest
        column), used in the error message.
    source
        What was read, named in the error.

    Returns
    -------
    BandStatistics or None
        The record, or ``None`` when the source has none of the five, as for
        an all-nodata band.

    Raises
    ------
    ValueError
        If the source carries some of the five but not all of them.
    """
    if not values:
        return None
    missing = [names[field_name] for field_name in names if field_name not in values]
    if missing:
        msg = (
            f"{source} carries only part of the band statistics: "
            f"{', '.join(missing)} missing. All five are published together."
        )
        raise ValueError(msg)
    return BandStatistics(**values)


@dataclass(frozen=True)
class RasterInfo:
    """What one raster says about itself: its tags, band and file layout.

    Read once per file. Every catalogue field that describes the pixels comes
    from here, so the catalogue states only what the file records.
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
    statistics: BandStatistics | None = None
    """Embedded band statistics, or ``None`` if the band has none.

    Portolan requires them (``PTL-DAT-009`` and ``PTL-DAT-010``), so a raster
    with ``None`` here cannot be published.
    """

    @property
    def is_decibel(self) -> bool:
        """Whether the stored values are decibels rather than linear."""
        return any(tag in self.tags for tag in _DECIBEL_TAGS) or self.band_unit == "dB"


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
    """Check the provenance of every discovered composite.

    The item builders stop at the first bad raster; this checks the whole
    archive and returns one record per bad raster, so one pass lists
    everything to fix.

    Parameters
    ----------
    by_tile
        The year's composites, grouped by tile.
    raster_info
        What the manifest records about each file, per tile and asset key
        (:func:`~glace_catalog.stac.store.manifest_raster_info`).

    Returns
    -------
    list[RasterProblems]
        One record per raster with a problem, tile order then asset order.
        Empty when the archive is intact.
    """
    found: list[RasterProblems] = []
    for tile, files in sorted(by_tile.items()):
        infos = raster_info.get(tile, {})
        for file in sorted(files, key=by_asset_key):
            info = infos.get(file.asset_key)
            problems = (
                ["no raster metadata was read"] if info is None else tag_problems(file, info)
            )
            if problems:
                found.append(RasterProblems(file=file, problems=problems))
    return found


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
        # A raster without PIPELINE_SOFTWARE carries no provenance tags at all,
        # so report that one fact. `s1bursts` writes this tag on every write,
        # which makes it a reliable marker.
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
        If :func:`tag_problems` finds anything. Use :func:`survey_tags` to list
        every bad raster in an archive.
    """
    problems = tag_problems(file, info)
    if problems:
        msg = f"{file.path}: {'; '.join(problems)}. {PROVENANCE_EXPECTATION}"
        raise ValueError(msg)


def _typed_fields(raster_tags: Mapping[str, Any], names: Mapping[str, str]) -> dict[str, Any]:
    """Map the tags in ``names`` onto their STAC fields, typing the numeric ones."""
    fields: dict[str, Any] = {}
    for tag, name in names.items():
        value = raster_tags.get(tag)
        if value is None:
            continue
        if tag in _INT_TAGS:
            fields[name] = int(value)
        elif tag in _FLOAT_TAGS:
            fields[name] = float(value)
        else:
            fields[name] = str(value)
    return fields


def tag_fields(raster_tags: Mapping[str, str]) -> dict[str, Any]:
    """Map the tags whose value is the file's own onto ``glace:`` asset fields."""
    return _typed_fields(raster_tags, _FILE_TAG_FIELDS)


def shared_tag_fields(raster_tags: Mapping[str, Any]) -> dict[str, Any]:
    """Map the tags every raster of an asset key shares onto ``glace:`` asset fields.

    The tiles collection's ``item_assets`` carry these; :func:`shared_mismatches`
    compares one raster's tags with them.
    """
    return _typed_fields(raster_tags, SHARED_TAG_FIELDS)


def shared_mismatches(raster_tags: Mapping[str, str], declared: Mapping[str, Any]) -> list[str]:
    """List each shared field where a raster's tags differ from the declared value.

    A field the raster lacks, or one it carries and ``declared`` omits, counts
    as a difference: the collection would otherwise state something the file
    does not.
    """
    found = shared_tag_fields(raster_tags)
    return [
        f"{name} is {found.get(name)!r}, the collection declares {declared.get(name)!r}"
        for name in sorted(set(found) | set(declared))
        if found.get(name) != declared.get(name)
    ]


def standard_fields(raster_tags: Mapping[str, str]) -> dict[str, Any]:
    """Map the GeoTIFF tags onto fields published extensions already define.

    ``processing:software`` is a map from software name to version, built from
    pairs of tags, so it is assembled here separately from
    :func:`tag_fields`.
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
