# Metadata generation: one source for the catalog's prose, names and numbers

**Status: approved 2026-09-09.** Decisions taken: the generated `catalog/` stays
committed under a regeneration gate; the style is generated too; the `vcs` and
`issues` links stay and no `git:*` fields are added; the projection extension is
dropped from both collections.

## Problem

The published metadata was reviewed in the Portolan browser on 2026-09-09
(`.cache/portolan-metadata-issues.md`). Behind the visible inconsistencies is one
structural cause: the names, descriptions and display numbers of the twelve
assets are written in eight places that do not read each other.

| where | written by | example |
| --- | --- | --- |
| `tiles/collection.json` `item_assets` | hand | "COH12 VV — 12-day InSAR coherence, co-polarised" |
| tile items | `items.py` + GeoTIFF tags | "12-day repeat-pass coherence VV, LRW composite"; QA from `QA_LAYER` |
| `mosaics/collection.json` `item_assets` | hand | "COH12 VV annual mosaic" |
| mosaic items | `publish-year.py` | "coh12_vv annual mosaic", no description |
| viz assets | `publish-year.py` | "coh12_vv web-map tiles" |
| `renders` in both collections | hand, twice | stale stretches, see below |
| style legend titles | `default.json` | "COH12 VV — 12-day coherence, co-polarised" |
| `rel: pmtiles` link titles | `finalize-catalog.py` | "coh12_vv web-map tiles, 2024" |

The `renders` blocks and two `AGENTS.md` carry the stretches from before the
2026-09-09 remeasurement (0.1–0.8 against the style's 0.1–0.75 for COH12 VV,
±6 dB diverging against −3..10 dB sequential for CQM, 0–30 against 0–90 for
the counts), and the catalog `AGENTS.md` still advises a diverging CQM ramp that
M-28 reversed. The published prose repeats the LRW explanation four times and
the linear-units note five times; the mosaics description explains link
registration mechanics; the UZH provider text differs between the collections.

Portolan's documentation best practice names the fix: the STAC description, the
README and the AGENTS.md are three surfaces for the same facts, so generate
them from one source.

## Design

### Source: `metadata/`

Tracked, reviewed, the only place prose and numbers are typed.

- **`metadata/catalog.yaml`** — identity and prose: `scope`, the region wording,
  the root title and subtitle, the description paragraphs, providers, license,
  keywords, the `vcs`, `issues` and `about` links (the viewer), the preliminary
  notice, the years the tracked tree claims, and the two collections' titles and
  descriptions. Ids, grids and bounding boxes come from `scopes.py`; nothing
  scope-derived is retyped.
- **`metadata/assets.yaml`** — the asset vocabulary. Per product (`coh12`,
  `rtc`): short name, long name, band unit, portable and designed colormaps,
  per-polarization stretch, the false-colour blue range, and the description
  sentences for the tile and the mosaic collection. Per QA metric (`num`,
  `cqm`): name, description sentences, dtype and nodata on each grid, ramp and
  range. The reasoning that `tests/test_styles.py` carried beside its pinned
  numbers moves here, beside the numbers.
- **`metadata/templates/`** — Jinja2 templates for the root, both collections
  and the tiles year catalog: `README.md.j2` and `AGENTS.md.j2` each. They
  receive the vocabulary tables and the catalog config; no asset name, unit or
  stretch is typed in a template.

### Vocabulary: `glace_catalog.metadata`

Loads both YAML files once and answers every naming question the repository
has:

- `asset_title(key)` — "Coherence VV", "Backscatter VH – Contributing
  Observations", "Coherence VV – Contributing Observations – PMTiles",
  "Coherence False Color – PMTiles".
- `item_asset(key, collection)` — the full `item_assets` entry: media type,
  title, description, roles, `raster:spatial_resolution`, `bands`. QA assets
  carry `cloud-optimized` beside `quality` so the browser badges them.
- `render(key)` — the `renders` entry: `rescale` from the stretch, the portable
  `colormap_name`, the decibel `expression` where the stretch is in decibels.
- `display(key)` — the ramp and range the tiler bakes, as `LayerStyle` /
  `RgbStyle`.
- `viz_asset(key, detail)` — the `visual` asset of one archive, and
  `pmtiles_link_title(key, year)`.
- `collection_title(collection)`, `catalog_short_title` — for links.

Spelling is American throughout ("polarized", "color", "meter" in prose is
avoided by writing "m").

### Renderer: `glace_catalog.documents` and `scripts/render-catalog.py`

Writes `catalog/` from `metadata/`:

- `catalog.json` — identity, links (`root`, `describedby`, `agents`, `icon`,
  `child` ×2, `vcs`, `issues`, `about`).
- `tiles/collection.json`, `mosaics/collection.json` — every field. `renders`
  covers all twelve keys in both. The projection extension is not declared;
  `proj:code` leaves the viz `item_assets` (the items carry it). The spatial
  extent is the scope's display grid reprojected to WGS84; the temporal extent is
  `[first year-01-01, last year+1-01-01)` from `catalog.yaml`, which
  `finalize-catalog` overwrites with the measured window as it does today.
- `mosaics/styles/default.json` — sources, layers, legends and derived stops
  from the vocabulary, for the last year `catalog.yaml` names.
- the six `README.md` / `AGENTS.md`.

Output is deterministic and in the canonical form `tests/test_data_format.py`
defines. `render-catalog.py --check` renders to a temporary directory and
exits 1 on any difference, naming the files.

### The build reads the same vocabulary

- `items.py` `_data_asset` / `_qa_asset` take title and description from the
  vocabulary; the GeoTIFF's own `LAYER_DESCRIPTION` and `QA_LAYER` stay in the
  file's tags and the mirrored `glace:*` fields.
- `publish-year.py` `mosaic_assets` and `visual_assets` take theirs from it.
- `finalize-catalog.py` `pmtiles_links` takes the link titles from it.
- `save.py`: `_collection_title` and `year_catalog_prose` go; the year catalog's
  markdown is rendered from the two year templates, and its description no
  longer carries the tile count or query directions.
- Root link titles use the config's short title rather than `Scope.catalog_title`.
- `webmap.py` keeps reading the committed style; the regeneration gate makes the
  style and the vocabulary identical, so M-27 holds with the vocabulary as the
  origin.

### Gates

- **`tests/test_render.py`** — regenerates into a temporary directory and diffs
  against the tracked `catalog/`. Also checks the vocabulary covers exactly
  `ASSET_KEYS` plus the two false-colour layers, and that every title is unique.
- **`tests/test_styles.py`** — keeps its invariants (coverage both ways, every
  colormap resolvable, ordered ranges, stops derived) and reads the expected
  values from the vocabulary instead of a table of its own.
- Every existing gate over `catalog/` runs unchanged.
- `run_all.py` gains `test_render.py` after `test_data_format.py`.

### Publication is unchanged

`catalog/` stays the tree `finalize-catalog` stages and patches (links, extent,
digests, the `self` link) and the tree rashid checks in CI and, mirrored from
the store, before publication. Rendering is a commit-time step; publication
still never rewrites prose. A release archived on Zenodo therefore carries the
rendered STAC metadata as well as its source.

### Superseded

- R-11 (no `metadata.yaml`): the config is now the only copy, so the "second
  copy" objection no longer applies.
- M-21 / M-27 wording: the style is generated from the reviewed vocabulary, and
  the file that is reviewed is `assets.yaml`.
- DESIGN.md §4 "hand-maintained documents" and the AGENTS.md rule: the
  hand-maintained files are now `metadata/*`; `catalog/` is generated output
  under a gate.

## Content decisions

- Root title "GLACE Switzerland (preliminary)"; subtitle "Sentinel-1 Glacier
  Coherence and Backscatter". Root description is the user's text of
  2026-09-09 with "90-day" and "tags". The preliminary notice survives as one
  callout in the root README and one sentence in the root AGENTS.md.
- Collection titles: "GLACE tiles – Sentinel-1 Composites on MGRS squares
  (10 m)" and "GLACE mosaics – Sentinel-1 Composites on ETRS89-LAEA (40 m)".
- The region is "the Alpine Convention perimeter" everywhere below the root
  title.
- Provider descriptions are written once. The UZH entry no longer names Source
  Cooperative or the issue tracker.
- Asset descriptions describe; arguments (why LAEA, why linear, why a mean)
  move to the READMEs, once each. The WebMercator 19.109 m digression leaves
  the published prose.
- The `glacier-fractions` asset description is one sentence.

## Out of scope

Thumbnails over a basemap (`thumbnails.py`), the browser's "Show on map"
button, and the browser's title casing of render keys.
