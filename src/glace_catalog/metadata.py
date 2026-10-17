"""The catalog's vocabulary: every name, description and display setting, from one source.

``metadata/assets.yaml`` defines the twelve published assets and their twelve
web-map archives; ``metadata/catalog.yaml`` holds the catalog's identity and
prose. This module reads both and answers every naming question in the
repository, so a collection's ``item_assets``, an item's assets, a ``renders``
entry, a style ramp and a README table all describe a file the same way.
``tests/test_render.py`` checks the rendered tree against it.
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jinja2
import yaml

from glace_catalog.encoding import ValueEncoding
from glace_catalog.scopes import (
    ANALYSIS_RESOLUTION,
    COMPOSITE_RESOLUTION,
    MOSAIC_ZOOM,
    VIZ_MIN_ZOOM,
    Scope,
    get_scope,
)
from glace_catalog.stac.naming import ASSET_KEYS, VIZ_SUFFIX, data_key, viz_key
from glace_catalog.styles import LayerStyle

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

Collection = str
"""``"tiles"`` or ``"mosaics"``, the directory a collection lives in."""

METADATA_DIR = Path(__file__).resolve().parents[2] / "metadata"
"""The tracked source directory, found relative to the package. This works in a
checkout, where the build and the render run."""

COG_MEDIA_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
PMTILES_MEDIA_TYPE = "application/vnd.pmtiles"

VIZ_KEYS: tuple[str, ...] = tuple(viz_key(key) for key in ASSET_KEYS)
"""The twelve web-map archives a mosaic year publishes, by asset key."""

TITLE_SEPARATOR = " – "
"""An en dash between the parts of a title: ``Coherence VV – Contributing Observations``."""


@dataclass(frozen=True)
class AssetRef:
    """One asset key, taken apart.

    Parameters
    ----------
    key
        The asset key as published, e.g. ``rtc_vh_qa_num_viz``.
    product
        ``coh12`` or ``rtc``.
    polarization
        ``vv`` or ``vh``.
    qa
        ``num`` or ``cqm``; ``None`` for a measurement.
    viz
        Whether the key names a web-map rendering.
    """

    key: str
    product: str
    polarization: str
    qa: str | None
    viz: bool

    @property
    def source(self) -> str:
        """The raster key a rendering pictures, or the key itself."""
        return data_key(self.key) if self.viz else self.key


def parse_key(key: str) -> AssetRef:
    """Take an asset key apart.

    Raises
    ------
    KeyError
        If the key is not one the vocabulary defines.
    """
    viz = key.endswith(VIZ_SUFFIX)
    stem = data_key(key) if viz else key
    parts = stem.split("_")
    product = parts[0]
    if product in ("coh12", "rtc") and len(parts) >= 2 and parts[1] in ("vv", "vh"):  # noqa: PLR2004
        if len(parts) == 2:  # noqa: PLR2004
            return AssetRef(key, product, parts[1], None, viz)
        if len(parts) == 4 and parts[2] == "qa" and parts[3] in ("num", "cqm"):  # noqa: PLR2004
            return AssetRef(key, product, parts[1], parts[3], viz)
    msg = (
        f"{key!r} is not an asset the vocabulary names; metadata/assets.yaml defines "
        f"the twelve LRW keys {', '.join(ASSET_KEYS)} and their _viz renderings"
    )
    raise KeyError(msg)


@dataclass(frozen=True)
class CollectionConfig:
    """Title and prose of one collection, from ``catalog.yaml``."""

    title: str
    short_title: str
    keywords: tuple[str, ...]
    description: str
    year_description: str = ""
    item_description: str = ""
    style_description: str = ""


@dataclass(frozen=True)
class CatalogConfig:
    """The catalog's identity and prose, from ``catalog.yaml``.

    Every prose field is a Jinja2 template until :meth:`Vocabulary.prose`
    renders it; the raw string is kept so a template can be rendered against
    the context it needs.
    """

    scope: Scope
    region: str
    first_year: int
    last_year: int
    title: str
    subtitle: str
    tagline: str
    preliminary_notice: str
    usage_notes: tuple[Mapping[str, str], ...]
    description: str
    license: str
    canonical_doi: str
    version_dois: Mapping[str, str]
    citation: str
    keywords: tuple[str, ...]
    providers: tuple[dict[str, Any], ...]
    links: Mapping[str, Mapping[str, str]]
    collections: Mapping[str, CollectionConfig]

    @property
    def short_title(self) -> str:
        """The title as ``root`` links repeat it, without the subtitle."""
        return self.title

    @property
    def full_title(self) -> str:
        """Title and subtitle, as the root catalog carries them."""
        return f"{self.title}{TITLE_SEPARATOR}{self.subtitle}"

    @property
    def years(self) -> range:
        """The years the tracked tree claims, inclusive of both ends."""
        return range(self.first_year, self.last_year + 1)


def _load_yaml(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text())
    if not isinstance(document, dict):
        msg = f"{path}: expected a mapping at the top level"
        raise TypeError(msg)
    return document


def _catalog_config(document: Mapping[str, Any]) -> CatalogConfig:
    years = document["years"]
    collections = {
        name: CollectionConfig(
            title=str(entry["title"]),
            short_title=str(entry["short_title"]),
            keywords=tuple(entry.get("keywords", ())),
            description=str(entry["description"]),
            year_description=str(entry.get("year_description", "")),
            item_description=str(entry.get("item_description", "")),
            style_description=str(entry.get("style_description", "")),
        )
        for name, entry in document["collections"].items()
    }
    return CatalogConfig(
        scope=get_scope(str(document["scope"])),
        region=str(document["region"]),
        first_year=int(years["first"]),
        last_year=int(years["last"]),
        title=str(document["title"]),
        subtitle=str(document["subtitle"]),
        tagline=str(document["tagline"]),
        preliminary_notice=str(document["preliminary_notice"]),
        usage_notes=tuple(
            {"title": str(note["title"]), "text": str(note["text"])}
            for note in document.get("usage_notes", ())
        ),
        description=str(document["description"]),
        license=str(document["license"]),
        canonical_doi=str(document.get("doi", {}).get("canonical", "")),
        version_dois={
            str(version): str(doi)
            for version, doi in document.get("doi", {}).get("versions", {}).items()
        },
        citation=str(document.get("citation", "")),
        keywords=tuple(document["keywords"]),
        providers=tuple(document["providers"]),
        links=document["links"],
        collections=collections,
    )


def _format_number(value: float) -> str:
    return f"{value:g}"


_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
)


def _in_words(count: int) -> str:
    """Spell a small count as prose does: ``twelve``, ``fourteen``."""
    return _WORDS[count] if count < len(_WORDS) else str(count)


def _pair(values: Sequence[Any]) -> tuple[float, float]:
    low, high = values
    return float(low), float(high)


def _range_text(low: float, high: float, units: str) -> str:
    unit = f" {units}" if units else ""
    return f"{_format_number(low)} to {_format_number(high)}{unit}"


class Vocabulary:
    """Every name, description and display number of the catalog.

    Construct through :func:`load_vocabulary`.
    """

    def __init__(self, assets: Mapping[str, Any], catalog: CatalogConfig) -> None:
        self._assets = assets
        self.catalog = catalog
        self._env = jinja2.Environment(  # noqa: S701  (markdown and JSON strings, not HTML)
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # -- the products and metrics ------------------------------------------

    def _product(self, ref: AssetRef) -> Mapping[str, Any]:
        return self._assets["products"][ref.product]

    def _qa(self, ref: AssetRef) -> Mapping[str, Any]:
        if ref.qa is None:
            raise KeyError(ref.key)
        return self._assets["qa"][ref.qa]

    def _pol_word(self, ref: AssetRef) -> str:
        return str(self._assets["polarizations"][ref.polarization])

    # -- titles --------------------------------------------------------------

    def asset_title(self, key: str) -> str:
        """Human title of one asset key.

        ``Coherence VV``, ``Backscatter VH – Contributing Observations``,
        ``Coherence VV – Composite Quality Map – PMTiles``.
        """
        ref = parse_key(key)
        product = self._product(ref)
        parts = [f"{product['name']} {ref.polarization.upper()}"]
        if ref.qa is not None:
            parts.append(str(self._qa(ref)["name"]))
        if ref.viz:
            parts.append("PMTiles")
        return TITLE_SEPARATOR.join(parts)

    def collection_title(self, collection: str) -> str:
        """Full title of one collection."""
        return self.catalog.collections[collection].title

    def collection_short_title(self, collection: str) -> str:
        """Short title of one collection, as links repeat it."""
        return self.catalog.collections[collection].short_title

    # -- display -------------------------------------------------------------

    def display(self, key: str) -> LayerStyle:
        """Return a layer's transport range and the ramp and stretch it is drawn with."""
        ref = parse_key(key)
        product = self._product(ref)
        if ref.qa is None:
            source = product
            ranges = {name: product[name][ref.polarization] for name in ("stretch", "transport")}
        else:
            source = self._qa(ref)
            # A count shares its ranges across products; a quality map's are per product.
            ranges = product["cqm"] if ref.qa == "cqm" else source
        decibel = bool(source["decibel"])
        low, high = _pair(ranges["stretch"])
        return LayerStyle(
            cmap=str(source["cmap"]),
            vmin=low,
            vmax=high,
            encoding=ValueEncoding(*_pair(ranges["transport"])),
            units="dB" if decibel else "",
            decibel=decibel,
        )

    def expression(self, key: str, band: str = "b1") -> str:
        """Return the decibel conversion a layer's ``renders`` entry declares, or ``""``.

        ``band`` is how the client names the band: ``{key}_b1`` in a STAC
        ``renders`` entry, ``b1`` in a titiler URL.
        """
        return f"10*log10({band})" if self.display(key).decibel else ""

    def portable_cmap(self, key: str) -> str:
        """Name a standard matplotlib colormap for clients without cmcrameri."""
        ref = parse_key(key)
        source = self._product(ref) if ref.qa is None else self._qa(ref)
        return str(source["portable_cmap"])

    def transport_text(self, key: str) -> str:
        """Format the transport range as prose: ``0 to 1`` or ``-25 to 5 dB``."""
        style = self.display(key)
        return _range_text(style.encoding.lo, style.encoding.hi, style.units)

    def render(self, key: str, collection: Collection) -> dict[str, Any]:
        """Build the ``renders`` entry of one raster, for a titiler-compatible client."""
        ref = parse_key(key)
        style = self.display(key)
        entry: dict[str, Any] = {"title": self.asset_title(key), "assets": [key]}
        if style.decibel:
            entry["expression"] = self.expression(key, band=f"{key}_b1")
        band = self._band(ref, collection)
        entry.update(
            {
                "rescale": [[style.vmin, style.vmax]],
                "colormap_name": self.portable_cmap(key),
                "resampling": "nearest" if band["data_type"].startswith("uint") else "average",
                "nodata": band["nodata"],
            },
        )
        return entry

    # -- assets ----------------------------------------------------------------

    def _band(self, ref: AssetRef, collection: Collection) -> dict[str, Any]:
        """Describe the one band of a raster: named by its asset key, typed per grid."""
        if ref.qa is None:
            return {"name": ref.key, "data_type": "float32", "nodata": "nan"}
        band = self._qa(ref)["band"][collection]
        return {"name": ref.key, "data_type": str(band["data_type"]), "nodata": band["nodata"]}

    def band_name(self, key: str) -> str:
        """Name of a raster's one band, on the file and in STAC alike: its asset key."""
        return parse_key(key).key

    def context(self) -> dict[str, Any]:
        """Return what every prose template may name: the region, the years, the counts."""
        return {
            "region": self.catalog.region,
            "first_year": self.catalog.first_year,
            "last_year": self.catalog.last_year,
            "n_assets": _in_words(len(ASSET_KEYS)),
            "n_viz": _in_words(len(VIZ_KEYS)),
            "n_assets_digits": len(ASSET_KEYS),
            "n_viz_digits": len(VIZ_KEYS),
            "max_zoom": MOSAIC_ZOOM,
            "analysis_resolution": _format_number(ANALYSIS_RESOLUTION),
            "preliminary_notice": self._env.from_string(self.catalog.preliminary_notice).render(
                region=self.catalog.region,
            ),
            "usage_notes": self.catalog.usage_notes,
        }

    def prose(self, template: str, **context: Any) -> str:
        """Render one prose template against :meth:`context` and ``context``."""
        return self._env.from_string(template).render({**self.context(), **context})

    def year_description(self, year: int) -> str:
        """Render the description of one tiles year sub-catalog."""
        return self.prose(self.catalog.collections["tiles"].year_description, year=year)

    def mosaic_item_description(self, year: int) -> str:
        """Render the description of one year's mosaic item."""
        return self.prose(self.catalog.collections["mosaics"].item_description, year=year)

    def style_description(self, year: int | None = None) -> str:
        """Render the style asset's description: the template's, or one year's copy."""
        template = self.catalog.collections["mosaics"].style_description
        return self.prose(template, **({} if year is None else {"year": year}))

    def asset_description(self, key: str, collection: Collection) -> str:
        """Render the description of one raster asset on one grid."""
        ref = parse_key(key)
        product = self._product(ref)
        kind = "data" if ref.qa is None else ref.qa
        template = str(self._assets["descriptions"][kind][collection])
        return self.prose(
            template,
            long=str(product["long"]),
            product=str(product["name"]),
            POL=ref.polarization.upper(),
            pol_word=self._pol_word(ref),
            value=str(product["value"]),
        )

    def item_asset(self, key: str, collection: Collection) -> dict[str, Any]:
        """Build the ``item_assets`` entry of one raster on one grid."""
        ref = parse_key(key)
        resolution = COMPOSITE_RESOLUTION if collection == "tiles" else ANALYSIS_RESOLUTION
        return {
            "type": COG_MEDIA_TYPE,
            "title": self.asset_title(key),
            "description": self.asset_description(key, collection),
            "roles": ["data" if ref.qa is None else "quality", "cloud-optimized"],
            "raster:spatial_resolution": resolution,
            "bands": [self._band(ref, collection)],
        }

    def viz_asset(
        self,
        key: str,
        *,
        min_zoom: int = VIZ_MIN_ZOOM,
        max_zoom: int = MOSAIC_ZOOM,
    ) -> dict[str, Any]:
        """Build the ``item_assets`` entry of one rendering: type, title, description, role, band.

        The one band is the decoded value: ``raster:scale`` and ``raster:offset``
        turn a code back into the unit the style draws, decibels for backscatter
        and the quality maps.
        """
        ref = parse_key(key)
        style = self.display(ref.source)
        description = self.prose(
            str(self._assets["descriptions"]["viz"]),
            source_title=self.asset_title(ref.source),
            transport=self.transport_text(ref.source),
            min_zoom=min_zoom,
            max_zoom=max_zoom,
        )
        return {
            "type": PMTILES_MEDIA_TYPE,
            "title": self.asset_title(key),
            "description": description,
            "roles": ["visual"],
            "bands": [style.encoding.stac_band(style.units)],
        }

    def sar_product_type(self, key: str) -> str:
        """Return the ``sar:product_type`` of one measurement, ``COH`` or ``RTC``."""
        return str(self._product(parse_key(key))["sar_product_type"])

    # -- the item mirrors --------------------------------------------------------

    def mirror_columns(self, collection: Collection) -> list[dict[str, str]]:
        """Name and describe each column of one collection's ``items.parquet``.

        The collection's ``table:columns`` and its AGENTS.md table both list
        these; finalization adds the types from the written file.
        """
        scope = self.catalog.scope
        columns = []
        for column in self._assets["mirror_columns"]:
            if collection not in column.get("collections", (collection,)):
                continue
            description = column["description"]
            if isinstance(description, dict):
                description = description[collection]
            text = self.prose(
                str(description),
                glacier_inventory=scope.glacier_inventory,
                glacier_buffer_m=_format_number(scope.glacier_buffer_m),
            )
            columns.append({"name": str(column["name"]), "description": text})
        return columns

    # -- tables for the prose --------------------------------------------------

    def asset_rows(self, collection: Collection) -> list[dict[str, Any]]:
        """One row per raster asset, for a README or AGENTS.md table."""
        rows = []
        for key in ASSET_KEYS:
            asset = self.item_asset(key, collection)
            band = asset["bands"][0]
            style = self.display(key)
            rows.append(
                {
                    "key": key,
                    "title": asset["title"],
                    "band": band["name"],
                    "data_type": band["data_type"],
                    "nodata": "NaN" if band["nodata"] == "nan" else band["nodata"],
                    "decibel": style.decibel,
                },
            )
        return rows

    @property
    def keys(self) -> Sequence[str]:
        """The twelve raster asset keys, in publication order."""
        return ASSET_KEYS


def load_vocabulary(root: Path = METADATA_DIR) -> Vocabulary:
    """Read ``assets.yaml`` and ``catalog.yaml`` under ``root``."""
    assets = _load_yaml(root / "assets.yaml")
    catalog = _catalog_config(_load_yaml(root / "catalog.yaml"))
    return Vocabulary(assets, catalog)


@functools.cache
def vocabulary() -> Vocabulary:
    """Return the checkout's vocabulary, read once.

    The files do not change during a run, so one read serves every title the
    build writes.
    """
    return load_vocabulary()
