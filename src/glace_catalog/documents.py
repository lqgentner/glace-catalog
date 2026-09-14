"""Render the tracked catalog documents from ``metadata/``.

``catalog/`` is generated output that is committed: the root catalog, both
collections, the MapLibre style template and every README and AGENTS.md down
to the collection level are written here from the vocabulary and the templates
under ``metadata/``, so that a title, a stretch or a sentence exists once and
every document that repeats it repeats the same thing. The tree stays in git
because a rendered diff is what a reviewer reads, because the Portolan browser
and rashid read a tree rather than a config, and because a release archived on
Zenodo then carries the STAC metadata itself. ``tests/test_render.py`` holds
the tracked tree to a fresh render.

Rendering is a commit-time step and nothing else: ``finalize-catalog`` still
patches the rendered documents with what only a build knows (the years, the
temporal extent, the archive links, the digests, the ``self`` link) and never
touches prose.
"""

from __future__ import annotations

from dataclasses import dataclass
import filecmp
import json
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any

import jinja2
from pyproj import Transformer
import yaml

from glace_catalog.metadata import (
    METADATA_DIR,
    PMTILES_MEDIA_TYPE,
    RGB_KEYS,
    TITLE_SEPARATOR,
    Vocabulary,
    load_vocabulary,
)
from glace_catalog.scopes import MOSAIC_ZOOM, TILE_PX, VIZ_MIN_ZOOM, Grid
from glace_catalog.stac.items import DATASET_VERSION
from glace_catalog.stac.naming import ASSET_KEYS, viz_filename
from glace_catalog.stac.save import MOSAICS_DIR, TILES_DIR
from glace_catalog.stac.store import ASSETS_DIR
from glace_catalog.styles import LayerStyle

if TYPE_CHECKING:
    from collections.abc import Mapping

Document = dict[str, Any]

ROOT = Path(__file__).resolve().parents[2]
PUBLISH_CONFIG = ROOT / "catalog.publish.yaml"
TEMPLATES_DIR = METADATA_DIR / "templates"

PORTOLAN_SCHEMA = "https://schemas.portolan-sdi.org/portolan/v0.2.0/schema.json"
RENDER_EXTENSION_URI = "https://stac-extensions.github.io/render/v2.0.0/schema.json"
WEB_MAP_LINKS_EXTENSION_URI = "https://stac-extensions.github.io/web-map-links/v1.3.0/schema.json"
RASTER_EXTENSION_URI = "https://stac-extensions.github.io/raster/v2.0.0/schema.json"
FILE_EXTENSION_URI = "https://stac-extensions.github.io/file/v2.1.0/schema.json"
ALTERNATE_ASSETS_EXTENSION_URI = (
    "https://stac-extensions.github.io/alternate-assets/v1.2.0/schema.json"
)
VERSION_EXTENSION_URI = "https://stac-extensions.github.io/version/v1.2.0/schema.json"

PORTOLAN_BROWSER = "https://browser.portolan-sdi.org/#/external/"
"""The Portolan browser opens any public catalog by its https URL without the scheme."""

STYLE_MEDIA_TYPE = "application/vnd.mapbox.style+json"
STYLE_TEMPLATE_NAME = "default"
ATTRIBUTION = "University of Zurich, Contains modified Copernicus Sentinel data {year}"
"""The credit line every archive header and style source carries (M-29)."""

STYLE_LAYER_ORDER: tuple[str, ...] = (
    "coh12_rgb",
    "coh12_vv",
    "coh12_vh",
    "rtc_rgb",
    "rtc_vv",
    "rtc_vh",
    *[key for key in ASSET_KEYS if key.endswith("_qa_num")],
    *[key for key in ASSET_KEYS if key.endswith("_qa_cqm")],
)
"""Layers of the style, first one visible: false color first, then the
measurements, then the QA layers grouped by metric."""

MARKDOWN_TEMPLATES: tuple[str, ...] = (
    "README.md",
    "AGENTS.md",
    f"{TILES_DIR}/README.md",
    f"{TILES_DIR}/AGENTS.md",
    f"{MOSAICS_DIR}/README.md",
    f"{MOSAICS_DIR}/AGENTS.md",
)
"""The markdown documents rendered into ``catalog/``; each has a ``.j2`` under
``metadata/templates/``."""


@dataclass(frozen=True)
class PublicBases:
    """Where the published tree answers, over https and over anonymous S3."""

    https: str
    s3: str

    def href(self, relative: str) -> str:
        """Absolute https href of one published path."""
        return f"{self.https}/{relative}"

    def alternate(self, relative: str) -> dict[str, Any]:
        """Build the ``alternate`` block of one published asset, over S3."""
        return {"s3": {"href": f"{self.s3}/{relative}", "alternate:name": "S3"}}


def load_public_bases(path: Path = PUBLISH_CONFIG) -> PublicBases:
    """Read ``public_base`` and ``public_s3_base`` from the publish configuration.

    Raises
    ------
    ValueError
        If either is missing. An asset href has to be absolute, and a relative
        one written here would only be found once a client failed to fetch it.
    """
    config = yaml.safe_load(path.read_text()) or {}
    missing = [key for key in ("public_base", "public_s3_base") if not config.get(key)]
    if missing:
        msg = f"{path} sets no {' and no '.join(missing)}."
        raise ValueError(msg)
    return PublicBases(
        https=str(config["public_base"]).rstrip("/"),
        s3=str(config["public_s3_base"]).rstrip("/"),
    )


def wgs84_bbox(grid: Grid, decimals: int = 4) -> list[float]:
    """Return the WGS84 bounding box of a grid, with densified edges.

    Four decimals is about ten metres, which is enough for an extent and coarse
    enough that a PROJ upgrade moving the last digit does not fail the
    regeneration gate for no metadata reason.
    """
    transformer = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    left, bottom, right, top = transformer.transform_bounds(*grid.bounds, densify_pts=21)
    return [round(v, decimals) for v in (left, bottom, right, top)]


def _link(rel: str, href: str, title: str, media_type: str = "application/json") -> Document:
    return {"rel": rel, "href": href, "type": media_type, "title": title}


def _json(document: Document) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# --- the documents ------------------------------------------------------------


def catalog_document(vocabulary: Vocabulary) -> Document:
    """Build the root ``catalog.json``.

    The ``self`` link is not here: ``finalize-catalog`` writes it from the
    deployment target, so the tracked tree stays valid in a checkout, in a
    preview and in production.
    """
    catalog = vocabulary.catalog
    links = [
        _link("root", "./catalog.json", catalog.short_title),
        _link("describedby", "./README.md", "Catalog README", "text/markdown"),
        _link("agents", "./AGENTS.md", "Catalog agent guide", "text/markdown"),
        _link("icon", "./_assets/glace-wordmark.svg", "GLACE wordmark", "image/svg+xml"),
        *[
            _link(
                "child", f"./{directory}/collection.json", vocabulary.collection_title(directory)
            )
            for directory in (TILES_DIR, MOSAICS_DIR)
        ],
        *[
            {"rel": rel, "href": link["href"], "title": link["title"]}
            for rel, link in catalog.links.items()
        ],
    ]
    return {
        "type": "Catalog",
        "stac_version": "1.1.0",
        "stac_extensions": [PORTOLAN_SCHEMA],
        "id": f"glace-{catalog.scope.key}",
        "title": catalog.full_title,
        "description": vocabulary.prose(catalog.description),
        "links": links,
    }


def _collection_assets(
    vocabulary: Vocabulary,
    collection: str,
    bases: PublicBases,
) -> Document:
    """Build the assets a collection declares over itself: mirror, thumbnail, style."""
    short = vocabulary.collection_short_title(collection)
    assets: Document = {
        "items": {
            "href": bases.href(f"{collection}/items.parquet"),
            "type": "application/vnd.apache.parquet",
            "title": "STAC-GeoParquet item mirror",
            "description": (
                "Every item of every year, one row each. The item JSON in the tree is the "
                "normative representation; this is the one to query."
            ),
            "roles": ["collection-mirror"],
            "alternate": bases.alternate(f"{collection}/items.parquet"),
        },
    }
    if collection == TILES_DIR:
        assets["glacier-fractions"] = {
            "href": bases.href(f"{collection}/glacier-fractions.parquet"),
            "type": "application/vnd.apache.parquet",
            "title": "Per-tile glacier cover fractions (RGI7)",
            "description": (
                "The share of each MGRS tile covered by RGI7 region 11 outlines, plain and "
                "buffered by 250 m, computed once per scope: the lookup behind the "
                "glace:glacier_fraction properties on every item."
            ),
            "roles": ["metadata"],
            "alternate": bases.alternate(f"{collection}/glacier-fractions.parquet"),
        }
    assets["thumbnail"] = {
        "href": bases.href(f"{collection}/thumbnail.png"),
        "type": "image/png",
        "title": f"{short} preview",
        "roles": ["thumbnail"],
    }
    if collection == MOSAICS_DIR:
        assets[f"style-{STYLE_TEMPLATE_NAME}"] = {
            "href": f"./styles/{STYLE_TEMPLATE_NAME}.json",
            "type": STYLE_MEDIA_TYPE,
            "title": "GLACE web-map style",
            "description": vocabulary.style_description(),
            "roles": ["style", "default"],
        }
    return assets


def collection_document(
    vocabulary: Vocabulary,
    collection: str,
    bases: PublicBases,
) -> Document:
    """One ``collection.json``, every field.

    What depends on a build is left for ``finalize-catalog``: the year links,
    the measured temporal extent, the archive links and the digests.
    """
    catalog = vocabulary.catalog
    scope = catalog.scope
    config = catalog.collections[collection]
    extensions = [
        PORTOLAN_SCHEMA,
        RENDER_EXTENSION_URI,
        *([WEB_MAP_LINKS_EXTENSION_URI] if collection == MOSAICS_DIR else []),
        RASTER_EXTENSION_URI,
        FILE_EXTENSION_URI,
        ALTERNATE_ASSETS_EXTENSION_URI,
        VERSION_EXTENSION_URI,
    ]
    item_assets: Document = {key: vocabulary.item_asset(key, collection) for key in ASSET_KEYS}
    if collection == MOSAICS_DIR:
        for key in (*ASSET_KEYS, *RGB_KEYS):
            item_assets[f"{key}_viz"] = vocabulary.viz_asset(f"{key}_viz")
    return {
        "type": "Collection",
        "stac_version": "1.1.0",
        "stac_extensions": extensions,
        "id": f"glace-{scope.key}-{collection}",
        "title": config.title,
        "description": vocabulary.prose(config.description),
        "license": catalog.license,
        "keywords": [*catalog.keywords, *config.keywords],
        "version": DATASET_VERSION,
        "providers": [dict(provider) for provider in catalog.providers],
        "extent": {
            "spatial": {"bbox": [wgs84_bbox(scope.display_grid)]},
            "temporal": {
                "interval": [
                    [
                        f"{catalog.first_year}-01-01T00:00:00Z",
                        f"{catalog.last_year + 1}-01-01T00:00:00Z",
                    ],
                ],
            },
        },
        "renders": {key: vocabulary.render(key, collection) for key in ASSET_KEYS},
        "item_assets": item_assets,
        "assets": _collection_assets(vocabulary, collection, bases),
        "links": [
            _link("root", "../catalog.json", catalog.short_title),
            _link("parent", "../catalog.json", catalog.short_title),
            _link(
                "describedby",
                "./README.md",
                f"{config.short_title} documentation",
                "text/markdown",
            ),
            _link("agents", "./AGENTS.md", f"{config.short_title} agent guide", "text/markdown"),
            _link(
                "license",
                "https://creativecommons.org/licenses/by/4.0/",
                catalog.license,
                "text/html",
            ),
        ],
    }


def style_document(vocabulary: Vocabulary, year: int) -> Document:
    """Build the MapLibre style template, written for one year.

    ``finalize-catalog`` re-years the copy it publishes per year, so the
    template names the last year the tracked tree claims and every year-bearing
    string takes the shape its regular expressions expect: ``/{year}/`` in each
    source url, ``({year})`` in the name, ``-{year}`` at the end of each layer
    id, and the year at the end of the credit line.
    """
    sources: Document = {}
    layers: list[Document] = []
    for index, key in enumerate(STYLE_LAYER_ORDER):
        sources[f"src-{key}"] = {
            "type": "raster",
            "url": f"pmtiles://../{year}/{viz_filename(key)}",
            "tileSize": TILE_PX,
            "minzoom": VIZ_MIN_ZOOM,
            "maxzoom": MOSAIC_ZOOM,
            "attribution": ATTRIBUTION.format(year=year),
        }
        layer: Document = {
            "id": f"glace-{key}-{year}",
            "type": "raster",
            "source": f"src-{key}",
        }
        if index > 0:
            layer["layout"] = {"visibility": "none"}
        layer["paint"] = {"raster-opacity": 1, "raster-resampling": "linear"}
        layer["metadata"] = {"portolan:legend": vocabulary.legend(key)}
        layers.append(layer)
    return {
        "version": 8,
        "name": f"GLACE{TITLE_SEPARATOR}Sentinel-1 composites ({year})",
        "metadata": {
            "description": (
                "Every published web-map layer of one year. The first is visible and the "
                "rest ship hidden, so a client switches by visibility rather than by loading "
                "another style. Each layer's metadata.portolan:legend carries the ramp and "
                "range baked into its archive; stops are derived from cmap over vmin-vmax, "
                "never authored. Rendered from metadata/assets.yaml."
            ),
        },
        "sources": sources,
        "layers": layers,
    }


# --- the prose ----------------------------------------------------------------


def _environment() -> jinja2.Environment:
    return jinja2.Environment(  # noqa: S701  (markdown, not HTML)
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _prose_context(vocabulary: Vocabulary, bases: PublicBases) -> dict[str, Any]:
    catalog = vocabulary.catalog
    return {
        **vocabulary.context(),
        "catalog": catalog,
        "public_base": bases.https,
        "public_s3_base": bases.s3,
        "browser_url": PORTOLAN_BROWSER + bases.https.removeprefix("https://") + "/catalog.json",
        "utm_zones": "/".join(catalog.scope.utm_zones),
        "tiles": catalog.collections[TILES_DIR],
        "mosaics": catalog.collections[MOSAICS_DIR],
        "tile_assets": vocabulary.asset_rows(TILES_DIR),
        "mosaic_assets": vocabulary.asset_rows(MOSAICS_DIR),
        "viz_assets": vocabulary.viz_rows(),
        "analysis_grid": catalog.scope.analysis_grid,
        "min_zoom": VIZ_MIN_ZOOM,
        "max_zoom": MOSAIC_ZOOM,
        "pmtiles_media_type": PMTILES_MEDIA_TYPE,
        "version": DATASET_VERSION,
        "rescale": lambda key: _rescale(vocabulary, key),
        "cmap": lambda key: _cmap(vocabulary, key),
        "portable_cmap": vocabulary.portable_cmap,
        "expression": vocabulary.expression,
        "title": vocabulary.asset_title,
    }


def _cmap(vocabulary: Vocabulary, key: str) -> str:
    """Name the designed colormap of one single-band layer."""
    style = vocabulary.display(key)
    if not isinstance(style, LayerStyle):
        raise KeyError(key)
    return style.cmap


def _rescale(vocabulary: Vocabulary, key: str) -> str:
    """Format the ``rescale`` query parameter of one layer, ``vmin,vmax``."""
    style = vocabulary.display(key)
    if not isinstance(style, LayerStyle):
        raise KeyError(key)
    return f"{style.vmin:g},{style.vmax:g}"


def render_markdown(
    vocabulary: Vocabulary,
    name: str,
    bases: PublicBases,
    **extra: Any,
) -> str:
    """Render one markdown template, e.g. ``tiles/README.md``, to its text."""
    template = _environment().get_template(f"{name}.j2")
    return template.render({**_prose_context(vocabulary, bases), **extra})


def render_year_prose(
    vocabulary: Vocabulary,
    year: int,
    bases: PublicBases | None = None,
) -> tuple[str, str]:
    """Render the ``README.md`` and ``AGENTS.md`` a tiles year sub-catalog owes.

    Portolan requires both in every catalog directory (``PTL-FIL-001``) and
    requires them linked. They are rendered at publication rather than tracked
    because there is one pair per year and the year is all that distinguishes
    them.
    """
    bases = bases or load_public_bases()
    return (
        render_markdown(vocabulary, f"{TILES_DIR}/year/README.md", bases, year=year),
        render_markdown(vocabulary, f"{TILES_DIR}/year/AGENTS.md", bases, year=year),
    )


# --- the tree -------------------------------------------------------------------


def tracked_documents(root: Path) -> set[Path]:
    """List the rendered documents under a catalog tree, relative to it.

    Everything except dotfiles and ``_assets/``, which the render does not write.
    """
    return {
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
        and path.relative_to(root).parts[0] != ASSETS_DIR
    }


def stale_documents(vocabulary: Vocabulary, tracked: Path) -> list[str]:
    """Compare ``tracked`` with a fresh render; return one line per difference.

    Empty when the tree is current. A file the render does not write, one it
    writes that is not tracked, and one whose bytes differ are each named, so
    the regeneration gate and ``render-catalog.py --check`` report the same
    thing.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        rendered = {path.relative_to(out) for path in render_tree(vocabulary, out)}
        existing = tracked_documents(tracked)
        problems = [
            f"{tracked / path} is tracked but not rendered" for path in sorted(existing - rendered)
        ]
        problems += [
            f"{tracked / path} is rendered but not tracked" for path in sorted(rendered - existing)
        ]
        problems += [
            f"{tracked / path} differs from a fresh render"
            for path in sorted(rendered & existing)
            if not filecmp.cmp(out / path, tracked / path, shallow=False)
        ]
    return problems


def render_tree(
    vocabulary: Vocabulary | None = None,
    out: Path = ROOT / "catalog",
    bases: PublicBases | None = None,
) -> list[Path]:
    """Write every tracked document under ``out``; return what was written.

    Files not written here (``_assets/``, dotfiles) are left alone, so the
    default target is the tracked ``catalog/`` itself.
    """
    vocabulary = vocabulary or load_vocabulary()
    bases = bases or load_public_bases()
    documents: Mapping[str, str] = {
        "catalog.json": _json(catalog_document(vocabulary)),
        f"{TILES_DIR}/collection.json": _json(collection_document(vocabulary, TILES_DIR, bases)),
        f"{MOSAICS_DIR}/collection.json": _json(
            collection_document(vocabulary, MOSAICS_DIR, bases),
        ),
        f"{MOSAICS_DIR}/styles/{STYLE_TEMPLATE_NAME}.json": _json(
            style_document(vocabulary, vocabulary.catalog.last_year),
        ),
        **{name: render_markdown(vocabulary, name, bases) for name in MARKDOWN_TEMPLATES},
    }
    written: list[Path] = []
    for name, text in documents.items():
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        written.append(path)
    return written
