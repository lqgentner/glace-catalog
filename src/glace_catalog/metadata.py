"""The catalog's vocabulary: every name, description and display number, from one source.

``metadata/assets.yaml`` names the twelve published assets and their fourteen
renderings once; ``metadata/catalog.yaml`` holds the catalog's identity and
prose. This module reads both and answers every naming question the repository
has, so that a collection's ``item_assets``, an item's assets, a ``renders``
entry, a style legend, a ``rel: "pmtiles"`` link and a README table all say the
same thing about the same file because they were all asked here.

Before this module, the asset titles were written in eight places that did not
read each other, and the ``renders`` stretches had drifted from the style's
after a remeasurement. The vocabulary is the fix: one origin, and
``tests/test_render.py`` holds the rendered tree to it.
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jinja2
import yaml

from glace_catalog.scopes import (
    ANALYSIS_RESOLUTION,
    COMPOSITE_RESOLUTION,
    MOSAIC_ZOOM,
    VIZ_MIN_ZOOM,
    Scope,
    get_scope,
)
from glace_catalog.stac.naming import ASSET_KEYS, VIZ_SUFFIX, data_key, viz_key
from glace_catalog.styles import LEGEND_STOPS, LayerStyle, RgbStyle, ramp_colors

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

Collection = str
"""``"tiles"`` or ``"mosaics"``, the directory a collection lives in."""

METADATA_DIR = Path(__file__).resolve().parents[2] / "metadata"
"""The tracked source directory. Resolved from the package location, which holds
in a checkout, where the build and the render run."""

COG_MEDIA_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
PMTILES_MEDIA_TYPE = "application/vnd.pmtiles"

RGB_KEYS: tuple[str, ...] = ("coh12_rgb", "rtc_rgb")
"""The two false-color layers, which exist only as renderings."""

VIZ_KEYS: tuple[str, ...] = tuple(viz_key(key) for key in (*ASSET_KEYS, *RGB_KEYS))
"""The fourteen web-map archives a mosaic year publishes, by asset key."""

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
        ``vv`` or ``vh``; ``None`` for a false-color layer.
    qa
        ``num`` or ``cqm``; ``None`` for a measurement.
    viz
        Whether the key names a rendering rather than a raster.
    """

    key: str
    product: str
    polarization: str | None
    qa: str | None
    viz: bool

    @property
    def source(self) -> str:
        """The raster key a rendering pictures, or the key itself."""
        return data_key(self.key) if self.viz else self.key

    @property
    def rgb(self) -> bool:
        """Whether this is a false-color layer."""
        return self.polarization is None


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
    if product in ("coh12", "rtc") and len(parts) >= 2:  # noqa: PLR2004
        if parts[1] == "rgb" and len(parts) == 2:  # noqa: PLR2004
            return AssetRef(key, product, None, None, viz)
        if parts[1] in ("vv", "vh"):
            if len(parts) == 2:  # noqa: PLR2004
                return AssetRef(key, product, parts[1], None, viz)
            if len(parts) == 4 and parts[2] == "qa" and parts[3] in ("num", "cqm"):  # noqa: PLR2004
                return AssetRef(key, product, parts[1], parts[3], viz)
    msg = (
        f"{key!r} is not an asset the vocabulary names; metadata/assets.yaml defines "
        f"the twelve LRW keys {', '.join(ASSET_KEYS)}, their _viz renderings and "
        f"{', '.join(RGB_KEYS)}"
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
    description: str
    license: str
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
        description=str(document["description"]),
        license=str(document["license"]),
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

    @staticmethod
    def _pol(ref: AssetRef) -> str:
        if ref.polarization is None:
            raise KeyError(ref.key)
        return ref.polarization

    def _pol_word(self, ref: AssetRef) -> str:
        return str(self._assets["polarizations"][self._pol(ref)])

    # -- titles --------------------------------------------------------------

    def asset_title(self, key: str) -> str:
        """Human title of one asset key.

        ``Coherence VV``, ``Backscatter VH – Contributing Observations``,
        ``Coherence VV – Composite Quality Map – PMTiles``,
        ``Coherence False Color – PMTiles``.
        """
        ref = parse_key(key)
        product = self._product(ref)
        if ref.rgb:
            parts = [str(product["false_color"]["title"])]
        else:
            parts = [f"{product['name']} {self._pol(ref).upper()}"]
            if ref.qa is not None:
                parts.append(str(self._qa(ref)["name"]))
        if ref.viz:
            parts.append("PMTiles")
        return TITLE_SEPARATOR.join(parts)

    def pmtiles_link_title(self, key: str, year: int) -> str:
        """Title of the collection's ``rel: "pmtiles"`` link to one archive."""
        return f"{self.asset_title(key)}, {year}"

    def collection_title(self, collection: str) -> str:
        """Full title of one collection."""
        return self.catalog.collections[collection].title

    def collection_short_title(self, collection: str) -> str:
        """Short title of one collection, as links repeat it."""
        return self.catalog.collections[collection].short_title

    # -- display -------------------------------------------------------------

    def display(self, key: str) -> LayerStyle | RgbStyle:
        """Return the ramp and range a layer is drawn with, and a rendering bakes."""
        ref = parse_key(key)
        product = self._product(ref)
        if ref.rgb:
            false_color = product["false_color"]
            red = _pair(product["stretch"]["vv"])
            green = _pair(product["stretch"]["vh"])
            blue = _pair(false_color["blue"])
            unit = "dB" if product["decibel"] else ""
            return RgbStyle(
                ranges=(red, green, blue),
                units=unit,
                decibel=bool(product["decibel"]),
                measured=True,
                bands=tuple(str(band) for band in false_color["channels"]),
            )
        if ref.qa is None:
            low, high = (float(v) for v in product["stretch"][self._pol(ref)])
            cmap, decibel = str(product["cmap"]), bool(product["decibel"])
        else:
            qa = self._qa(ref)
            cmap, decibel = str(qa["cmap"]), bool(qa["decibel"])
            low, high = (float(v) for v in (product["cqm"] if ref.qa == "cqm" else qa["range"]))
        unit = "dB" if decibel else ""
        stops = tuple(
            {
                "value": round(low + (high - low) * index / (LEGEND_STOPS - 1), 6),
                "color": color,
                "label": _format_number(low if index == 0 else high)
                if index in (0, LEGEND_STOPS - 1)
                else "",
            }
            for index, color in enumerate(ramp_colors(cmap))
        )
        return LayerStyle(
            cmap=cmap,
            vmin=low,
            vmax=high,
            units=unit,
            decibel=decibel,
            measured=True,
            stops=stops,
        )

    def expression(self, key: str, band: str = "b1") -> str:
        """Return the decibel conversion a layer's ``renders`` entry declares, or ``""``.

        ``band`` is how the client names the band: ``{key}_b1`` in a STAC
        ``renders`` entry, ``b1`` in a titiler URL.
        """
        return f"10*log10({band})" if self.display(key).decibel else ""

    def portable_cmap(self, key: str) -> str:
        """Name the matplotlib colormap a client without cmcrameri can resolve (M-10)."""
        ref = parse_key(key)
        source = self._product(ref) if ref.qa is None else self._qa(ref)
        return str(source["portable_cmap"])

    def legend(self, key: str) -> dict[str, Any]:
        """Build the ``portolan:legend`` block of one style layer."""
        style = self.display(key)
        if isinstance(style, RgbStyle):
            return {
                "title": self.asset_title(key),
                "type": "channels",
                "unit": style.units,
                "decibel": style.decibel,
                "measured": style.measured,
                "channels": [
                    {"band": band, "vmin": low, "vmax": high}
                    for band, (low, high) in zip(style.bands, style.ranges, strict=True)
                ],
            }
        return {
            "title": self.asset_title(key),
            "type": "ramp",
            "cmap": style.cmap,
            "vmin": style.vmin,
            "vmax": style.vmax,
            "unit": style.units,
            "decibel": style.decibel,
            "measured": style.measured,
            "stops": list(style.stops),
        }

    def stretch_text(self, key: str) -> str:
        """Format the stretch as prose: ``0.1 to 0.75``, ``-16.5 to -4 dB``, or three ranges."""
        style = self.display(key)
        unit = f" {style.units}" if style.units else ""
        if isinstance(style, RgbStyle):
            ranges = "; ".join(
                f"{_format_number(low)} to {_format_number(high)}" for low, high in style.ranges
            )
            return f"{ranges}{unit}"
        return f"{_format_number(style.vmin or 0)} to {_format_number(style.vmax or 0)}{unit}"

    def render(self, key: str, collection: Collection) -> dict[str, Any]:
        """Build the ``renders`` entry of one raster, for a titiler-compatible client."""
        ref = parse_key(key)
        style = self.display(key)
        if not isinstance(style, LayerStyle):
            raise KeyError(key)
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
            POL=self._pol(ref).upper(),
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
        """Build the ``item_assets`` entry of one rendering: type, title, description, role."""
        ref = parse_key(key)
        style = self.display(ref.source)
        if ref.rgb:
            product = ref.product
            read_instead = f"{product}_vv.tif and {product}_vh.tif"
            template = str(self._assets["descriptions"]["viz_rgb"])
            description = self.prose(
                template,
                channels=", ".join(style.bands) if isinstance(style, RgbStyle) else "",
                stretch=self.stretch_text(ref.source),
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                read_instead=read_instead,
            )
        else:
            template = str(self._assets["descriptions"]["viz"])
            description = self.prose(
                template,
                source_title=self.asset_title(ref.source),
                stretch=self.stretch_text(ref.source),
                cmap=style.cmap if isinstance(style, LayerStyle) else "",
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                read_instead=f"{ref.source}.tif",
            )
        return {
            "type": PMTILES_MEDIA_TYPE,
            "title": self.asset_title(key),
            "description": description,
            "roles": ["visual"],
        }

    def sar_product_type(self, key: str) -> str:
        """Return the ``sar:product_type`` of one measurement, ``COH`` or ``RTC``."""
        return str(self._product(parse_key(key))["sar_product_type"])

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
                    "stretch": self.stretch_text(key),
                    "cmap": style.cmap if isinstance(style, LayerStyle) else "",
                    "decibel": style.decibel,
                },
            )
        return rows

    def viz_rows(self) -> list[dict[str, Any]]:
        """One row per rendering, for the mosaics prose."""
        return [
            {
                "key": key,
                "title": self.asset_title(key),
                "stretch": self.stretch_text(data_key(key)),
            }
            for key in VIZ_KEYS
        ]

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

    The build reads it for every title it writes, and the files do not change
    under a run.
    """
    return load_vocabulary()
