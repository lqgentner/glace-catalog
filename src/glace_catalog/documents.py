"""Render the tracked catalog documents from ``metadata/``.

``catalog/`` is generated output that is committed. The root catalog, both
collections, the MapLibre style template, and the README and AGENTS.md files
down to collection level are rendered from the vocabulary and templates under
``metadata/``, so each title, stretch or sentence is defined once. The tree is
committed so that reviewers see rendered diffs, the Portolan browser and
rashid can read it, and a release archived on Zenodo includes the STAC
metadata. ``tests/test_render.py`` checks that the tracked tree matches a fresh
render.

Rendering happens at commit time. ``finalize-catalog`` later adds what only a
build knows (the years, the temporal extent, the digests, the ``self`` link)
and keeps the prose.
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

from glace_catalog.grid import GRID_ARCHIVE, GRID_LAYER, GRID_MAX_ZOOM, GRID_MIN_ZOOM
from glace_catalog.metadata import (
    METADATA_DIR,
    PMTILES_MEDIA_TYPE,
    TITLE_SEPARATOR,
    Vocabulary,
    load_vocabulary,
)
from glace_catalog.scopes import MOSAIC_ZOOM, TILE_PX, VIZ_MIN_ZOOM, Grid
from glace_catalog.stac.items import DATASET_VERSION
from glace_catalog.stac.naming import ASSET_KEYS, viz_filename
from glace_catalog.stac.save import MOSAICS_DIR, TILES_DIR
from glace_catalog.stac.store import ASSETS_DIR
from glace_catalog.styles import ATTRIBUTION, ramp_colors

if TYPE_CHECKING:
    from collections.abc import Mapping

Document = dict[str, Any]

ROOT = Path(__file__).resolve().parents[2]
PUBLISH_CONFIG = ROOT / "catalog.publish.yaml"
TEMPLATES_DIR = METADATA_DIR / "templates"

PORTOLAN_SCHEMA = "https://schemas.portolan-sdi.org/portolan/v0.2.0/schema.json"
RENDER_EXTENSION_URI = "https://stac-extensions.github.io/render/v2.0.0/schema.json"
RASTER_EXTENSION_URI = "https://stac-extensions.github.io/raster/v2.0.0/schema.json"
FILE_EXTENSION_URI = "https://stac-extensions.github.io/file/v2.1.0/schema.json"
ALTERNATE_ASSETS_EXTENSION_URI = (
    "https://stac-extensions.github.io/alternate-assets/v1.2.0/schema.json"
)
VERSION_EXTENSION_URI = "https://stac-extensions.github.io/version/v1.2.0/schema.json"
SCIENTIFIC_EXTENSION_URI = "https://stac-extensions.github.io/scientific/v1.0.0/schema.json"
WEB_MAP_LINKS_EXTENSION_URI = "https://stac-extensions.github.io/web-map-links/v1.3.0/schema.json"
DOI_RESOLVER = "https://doi.org/"

PORTOLAN_BROWSER = "https://browser.portolan-sdi.org/#/external/"
"""Portolan browser base URL; append a public catalog's https URL without the scheme."""

STYLE_MEDIA_TYPE = "application/vnd.mapbox.style+json"
STYLE_TEMPLATE_NAME = "default"
GRID_STYLE_NAME = "grid"
GRID_CMAP = "cmc.devon_r"
"""Colour map the grid style fills the buffered glacier fraction with."""

STYLE_LAYER_ORDER: tuple[str, ...] = (
    "coh12_vv",
    "coh12_vh",
    "rtc_vv",
    "rtc_vh",
    *[key for key in ASSET_KEYS if key.endswith("_qa_num")],
    *[key for key in ASSET_KEYS if key.endswith("_qa_cqm")],
)
"""Layers of the style, first one visible: the measurements, then the QA layers
grouped by metric."""

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
        If either is missing, because collection asset hrefs must be
        absolute.
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

    Four decimals is about ten metres: precise enough for an extent, and
    coarse enough that a PROJ upgrade changing the last digit leaves the
    render test passing.
    """
    transformer = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    left, bottom, right, top = transformer.transform_bounds(*grid.bounds, densify_pts=21)
    return [round(v, decimals) for v in (left, bottom, right, top)]


def _link(rel: str, href: str, title: str, media_type: str = "application/json") -> Document:
    return {"rel": rel, "href": href, "type": media_type, "title": title}


def _json(document: Document) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# --- the documents ------------------------------------------------------------


def version_doi(vocabulary: Vocabulary) -> str:
    """Return the DOI of ``DATASET_VERSION``, or an empty string if the catalog has none.

    Raises
    ------
    ValueError
        If the catalog lists version DOIs but none for ``DATASET_VERSION``, so
        a version bump cannot keep the previous version's DOI.
    """
    dois = vocabulary.catalog.version_dois
    if dois and DATASET_VERSION not in dois:
        msg = f"metadata/catalog.yaml lists no DOI for version {DATASET_VERSION}"
        raise ValueError(msg)
    return dois.get(DATASET_VERSION, "")


def citation(vocabulary: Vocabulary) -> str:
    """Render the recommended citation of this version, or an empty string without a DOI."""
    doi = version_doi(vocabulary)
    if not doi:
        return ""
    return vocabulary.prose(vocabulary.catalog.citation, version=DATASET_VERSION, doi=doi)


def _doi_links(vocabulary: Vocabulary) -> list[Document]:
    """Link the version DOI as ``cite-as`` and the canonical DOI as ``related``."""
    links = []
    if doi := version_doi(vocabulary):
        links.append(
            _link("cite-as", DOI_RESOLVER + doi, f"DOI of version {DATASET_VERSION}", "text/html")
        )
    if canonical := vocabulary.catalog.canonical_doi:
        links.append(
            _link("related", DOI_RESOLVER + canonical, "Canonical DOI, every version", "text/html")
        )
    return links


def catalog_document(vocabulary: Vocabulary) -> Document:
    """Build the root ``catalog.json``.

    ``finalize-catalog`` adds the ``self`` link from the deployment target, so
    the tracked tree is valid in a checkout, in a preview and in production.
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
        *_doi_links(vocabulary),
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
    """Build the assets a collection declares over itself: mirror, grid, thumbnail, style."""
    short = vocabulary.collection_short_title(collection)
    scope = vocabulary.catalog.scope
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
        assets["grid"] = {
            "href": bases.href(f"{collection}/{GRID_ARCHIVE}"),
            "type": PMTILES_MEDIA_TYPE,
            "title": "Tile grid with glacier fractions",
            "description": (
                f"Every MGRS tile of any year as a polygon in the vector layer {GRID_LAYER}, "
                "with the properties mgrs_tile, glacier_fraction and "
                "glacier_fraction_buffered: the share of the tile covered by "
                f"{scope.glacier_inventory} outlines, plain and buffered by "
                f"{scope.glacier_buffer_m:g} m."
            ),
            "roles": ["visual"],
            "alternate": bases.alternate(f"{collection}/{GRID_ARCHIVE}"),
        }
    assets["thumbnail"] = {
        "href": bases.href(f"{collection}/thumbnail.webp"),
        "type": "image/webp",
        "title": f"{short} preview",
        "roles": ["thumbnail"],
    }
    if collection == TILES_DIR:
        assets[f"style-{GRID_STYLE_NAME}"] = {
            "href": f"./styles/{GRID_STYLE_NAME}.json",
            "type": STYLE_MEDIA_TYPE,
            "title": "GLACE tile grid style",
            "description": (
                "Draws the tile grid, each tile filled by its buffered glacier fraction."
            ),
            "roles": ["style", "default"],
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
    """Build one ``collection.json``.

    ``finalize-catalog`` later adds what depends on a build: the year links,
    the measured temporal extent and the digests.
    """
    catalog = vocabulary.catalog
    scope = catalog.scope
    config = catalog.collections[collection]
    extensions = [
        PORTOLAN_SCHEMA,
        RENDER_EXTENSION_URI,
        RASTER_EXTENSION_URI,
        FILE_EXTENSION_URI,
        ALTERNATE_ASSETS_EXTENSION_URI,
        VERSION_EXTENSION_URI,
    ]
    web_map_links: list[Document] = []
    if collection == TILES_DIR:
        # Portolan registers a collection's PMTiles through a link
        # (PORTO-FMT-011); the tile grid is the tiles collection's one archive.
        extensions.append(WEB_MAP_LINKS_EXTENSION_URI)
        web_map_links.append(
            {
                "rel": "pmtiles",
                "href": bases.href(f"{TILES_DIR}/{GRID_ARCHIVE}"),
                "type": PMTILES_MEDIA_TYPE,
                "title": "Tile grid with glacier fractions",
                "pmtiles:layers": [GRID_LAYER],
            },
        )
    scientific: Document = {}
    if doi := version_doi(vocabulary):
        extensions.append(SCIENTIFIC_EXTENSION_URI)
        scientific = {"sci:doi": doi, "sci:citation": citation(vocabulary)}
    item_assets: Document = {key: vocabulary.item_asset(key, collection) for key in ASSET_KEYS}
    if collection == MOSAICS_DIR:
        for key in ASSET_KEYS:
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
        **scientific,
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
            *_doi_links(vocabulary),
            *web_map_links,
        ],
    }


def grid_style_document() -> Document:
    """Build the MapLibre style of the tile grid.

    One vector source, the grid archive, drawn twice: a fill coloured by each
    tile's buffered glacier fraction over 0–1, and an outline.
    """
    colors = ramp_colors(GRID_CMAP, 9)
    stops: list[Any] = []
    for index, color in enumerate(colors):
        stops += [round(index / (len(colors) - 1), 3), color]
    return {
        "version": 8,
        "name": f"GLACE{TITLE_SEPARATOR}tile grid",
        "metadata": {
            "description": (
                "The MGRS tiles of the tiles collection, each filled by its glacier "
                "fraction within the buffered outlines. Rendered by glace_catalog.documents."
            ),
        },
        "sources": {
            "grid": {
                "type": "vector",
                "url": f"pmtiles://../{GRID_ARCHIVE}",
                "minzoom": GRID_MIN_ZOOM,
                "maxzoom": GRID_MAX_ZOOM,
            },
        },
        "layers": [
            {
                "id": "glace-grid-fill",
                "type": "fill",
                "source": "grid",
                "source-layer": GRID_LAYER,
                "paint": {
                    "fill-color": [
                        "interpolate",
                        ["linear"],
                        ["get", "glacier_fraction_buffered"],
                        *stops,
                    ],
                    "fill-opacity": 0.6,
                },
            },
            {
                "id": "glace-grid-outline",
                "type": "line",
                "source": "grid",
                "source-layer": GRID_LAYER,
                "paint": {"line-color": "#555555", "line-width": 0.5},
            },
        ],
    }


def style_document(vocabulary: Vocabulary, year: int) -> Document:
    """Build the MapLibre style template, written for one year.

    Each archive is a ``raster-dem`` source whose custom encoding decodes its
    codes back to values, drawn by a ``color-relief`` layer over the layer's
    default ramp and stretch (:mod:`glace_catalog.encoding`). The ramp lives
    only in the style, so a client may replace it.

    ``finalize-catalog`` publishes one copy per year, replacing the year with
    regular expressions. The template uses the last year of the tracked tree,
    in the forms those expressions expect: ``/{year}/`` in each source url,
    ``({year})`` in the name, ``-{year}`` at the end of each layer id, and the
    year at the end of the credit line.
    """
    sources: Document = {}
    layers: list[Document] = []
    for index, key in enumerate(STYLE_LAYER_ORDER):
        style = vocabulary.display(key)
        sources[f"src-{key}"] = {
            "type": "raster-dem",
            "url": f"pmtiles://../{year}/{viz_filename(key)}",
            "tileSize": TILE_PX,
            "minzoom": VIZ_MIN_ZOOM,
            "maxzoom": MOSAIC_ZOOM,
            "attribution": ATTRIBUTION.format(year=year),
            **style.encoding.maplibre(),
        }
        layer: Document = {
            "id": f"glace-{key}-{year}",
            "type": "color-relief",
            "source": f"src-{key}",
        }
        if index > 0:
            layer["layout"] = {"visibility": "none"}
        layer["paint"] = {
            "color-relief-opacity": 1,
            # Nearest-neighbour keeps every drawn value a real code, also at
            # nodata edges.
            "resampling": "nearest",
            "color-relief-color": style.encoding.ramp(
                ramp_colors(style.cmap),
                style.vmin,
                style.vmax,
            ),
        }
        layers.append(layer)
    return {
        "version": 8,
        "name": f"GLACE{TITLE_SEPARATOR}Sentinel-1 composites ({year})",
        "metadata": {
            "description": (
                "Every published web-map layer of one year. The first is visible and the "
                "rest ship hidden, so a client switches by visibility rather than by loading "
                "another style. Each archive holds value codes, decoded by its raster-dem "
                "source's custom encoding and drawn by a color-relief layer over a default "
                "ramp a client may replace. Rendered from metadata/assets.yaml."
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
        "analysis_grid": catalog.scope.analysis_grid,
        "min_zoom": VIZ_MIN_ZOOM,
        "max_zoom": MOSAIC_ZOOM,
        "pmtiles_media_type": PMTILES_MEDIA_TYPE,
        "version": DATASET_VERSION,
        "citation": citation(vocabulary),
    }


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
    """Render the ``README.md`` and ``AGENTS.md`` of one tiles year sub-catalog.

    Portolan requires both, linked, in every catalog directory
    (``PTL-FIL-001``). They are rendered at publication time, one pair per
    year, from templates that differ only in the year.
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

    The list is empty when the tree is current. It names every file that is
    tracked but not rendered, rendered but not tracked, or different. The
    render test and ``render-catalog.py --check`` both report this list.
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

    Other files (``_assets/``, dotfiles) are kept as they are, so the default
    target is the tracked ``catalog/`` itself.
    """
    vocabulary = vocabulary or load_vocabulary()
    bases = bases or load_public_bases()
    documents: Mapping[str, str] = {
        "catalog.json": _json(catalog_document(vocabulary)),
        f"{TILES_DIR}/collection.json": _json(collection_document(vocabulary, TILES_DIR, bases)),
        f"{MOSAICS_DIR}/collection.json": _json(
            collection_document(vocabulary, MOSAICS_DIR, bases),
        ),
        f"{TILES_DIR}/styles/{GRID_STYLE_NAME}.json": _json(grid_style_document()),
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
