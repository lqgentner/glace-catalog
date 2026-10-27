# The design of the GLACE catalog

Why the store is shaped the way it is: what holds now, and why. How to build
and publish it is in [`AGENTS.md`](../AGENTS.md); the validator findings this
catalog accepts are in [`conformance.md`](conformance.md); open work is in the
GitHub issues.

---

## 1. Identity, license and providers

The license is `CC-BY-4.0` (`PTL-LIC-001`), and the data contains modified
Copernicus Sentinel data.

**UZH is both producer and host**, in one provider entry carrying every role.
Portolan compares the producer and host names to decide whether a catalog is
official or a mirror, and a mirror owes a `via` link to its source. An
official catalog carries none, so the root links its Source Cooperative page as
`alternate` and the production pipeline as `related`. Source
Cooperative is the storage vendor, not the host: the spec defines the host as
the organization operating this copy, "not the underlying cloud vendor whose
storage it happens to sit on". Listing it as host triggers `PTL-PRO-001` and
`PTL-PRO-003`.

**ESA / Copernicus is a second producer**, listed first, with the `licensor`
role: it operates Sentinel-1, and Copernicus terms govern reuse of the input.

---

## 2. The layout

Portolan requires a collection to own its items: a collection directory holds
a `collection.json` and one subdirectory per item. A catalog may sit *below* a
collection to group items. So each collection has its own year directories,
and the year sub-catalogs fit naturally.

```
{root}/
├── catalog.json  README.md  AGENTS.md  _assets/glace-wordmark.svg
├── tiles/
│   ├── collection.json  README.md  AGENTS.md
│   ├── items.parquet            ← collection-mirror, every year in one file
│   ├── thumbnail.webp  grid.pmtiles  styles/{default,grid}.json
│   ├── .glacier-fractions.parquet               ← build input, never published
│   └── {year}/
│       ├── catalog.json  README.md  AGENTS.md
│       ├── .build-state.json  .items.parquet     ← never published
│       └── {tile}/item.json + 12 COGs
└── mosaics/
    ├── collection.json  README.md  AGENTS.md
    ├── items.parquet  thumbnail.webp
    ├── styles/default.json          # MapLibre GL, the latest year's style
    └── {year}/item.json + 12 analysis COGs + 12 {stem}_viz.pmtiles
        └── styles/default.json      # the year's style, an item asset
```

**Each collection mirrors its items in `items.parquet`**, a
`collection-mirror` asset. No `rel: "items"` link points at it: that relation
names an item endpoint, which a static catalog does not have.

**One collection per product, not one per year.** Per-year collections make
each year independently publishable. Here they would mean twelve copies of the
same providers, license and prose, and a cross-year query would become a
concatenation. A dataloader wants one collection with year sub-catalogs and one
mirror covering the series.

**Year sub-catalogs only, no third grouping level.** A year holds up to 2 210
tiles, so `PTL-CAT-001` warns on each year; [`conformance.md`](conformance.md)
gives the reasons for taking the warning.

**Filenames carry no EPSG code, year or scope.** The directory names the
collection and therefore the grid, `proj:code` states it, and the asset key is
the filename stem, so a suffix would only lengthen every href. The strongest
objection is the year: copying four years of `coh12_vv.tif` into one directory
collides. That flattening is the user's choice; `odc.stac` and `rasterio` read
by URL, and item ids disambiguate. Fields of the World (FTW) and portolan-nl
qualify fully; this catalog prefers short, predictable hrefs.

**Web-map archives sit beside the rasters they render**, as
`{stem}_viz.pmtiles` in the year's item directory. Each is an asset of that
item with the `visual` role, so it carries the year's footprint and window.
The `_viz` suffix separates the measurement from the picture in the one
listing where both appear: `coh12_vv.tif` is linear float32, while
`coh12_vv_viz.pmtiles` holds lossy 8-bit codes, in decibels for backscatter.
A client lists the archives from the year items and draws them through the
year's style, also an item asset, as layer `glace-{stem}-{year}`. The
mosaics collection carries no `rel: "pmtiles"` links, a recorded deviation
from `PORTO-FMT-011` ([`conformance.md`](conformance.md)).

**The tile grid is the tiles collection's one archive.** `tiles/grid.pmtiles`
is a vector PMTiles, layer `mgrs`, zooms 0–10, with a polygon per tile of any
year and its two glacier fractions as properties. The grid does not change
between years, so one archive serves all of them. It is registered as
`PORTO-FMT-011` asks: a `visual` asset, a `rel: "pmtiles"` link and the style
`styles/grid.json`, whose stepped fill gives the Portolan browser a legend. A
web map draws the grid from it without reading `items.parquet`, whose footer is
most of what such a read costs. Finalization draws the grid from the item
indexes; the fraction table they came from is a build input under a leading
dot, so publishing skips it.

**The tiles collection's default style shows coherence, not the grid.**
The grid is an index; coherence is the product. `styles/default.json` draws the
latest year's `coh12_vv` from the mosaics collection's archive, resampled from
these tiles, and outlines the tiles over it. The thumbnail draws the same layer
with the same ramp and stretch, from one tile's COG.

**Per-year run records are not published.** The manifest is the build's
cache and the configuration is pipeline state. Both stay beside the source
archive; the items carry every value a reader needs.

**Every tile item of a year shares one time range.** The `COMPOSITE_*_DATE`
tags carry the compositing window, which the pipeline takes from a committed
per-scope windows file and passes to s1bursts (0.5.0 and later). The build
reads the window from the manifest and requires one window per year: a file
from another window stops the year rather than widening its extent.
`published_time_range` is the one place that turns the inclusive days into
`start_datetime` / `end_datetime`. The observed span is published per product
as `glace:*_first_acquisition` / `_last_acquisition`.

**A tile asset carries only what is its file's own.** Most provenance
tags have one value per asset key across every tile and year: the compositing
method, the coherence window, the pipeline. `metadata/assets.yaml` declares
them once, the tiles collection's `item_assets` publish them, and the build
refuses a raster whose tags differ. Each asset keeps its observation count and,
for coherence, the tile's coherence statistics. The window and acquisition
dates are item properties already, and `processing:software` is one item
property.

**Dotfiles are build state.** `.build-state.json` and `.items.parquet` (the
year's slice of the mirror, before `finalize-catalog` combines them) start
with a dot so publishing skips them.

---

## 3. Visualization

**Published: twelve float32 LERC analysis COGs per year on the equal-area
grid, drawn client-side through the `renders` block, plus PMTiles archives
with MapLibre styles as the web-map product.** The mosaics collection links its
twelve items directly, below the twenty-child threshold for sub-catalogs.

**PMTiles, not COGs, for the web map.** As COGs, the mosaics cost about four
times the bytes on the wire; the data type, not the container, causes it. A
single-band uint8 display COG saves too little to justify a second product:
95 % of the LERC baseline for coherence, 49 % for backscatter.

**The archives carry values, not colours.** Each tile holds an 8-bit code,
repeated in all three channels and linear over a fixed *transport range* per
layer. Code 0 is nodata, with a binary alpha band. Tiles are lossy WebP at
quality 90. The year style declares the decoding as a MapLibre `raster-dem`
source with `"encoding": "custom"` and draws it with a `color-relief` layer
over a default ramp and stretch. A client can recolour, restretch and read
values without new tiles. The item asset declares the same decoding as
`raster:scale` and `raster:offset` on its band. No `layers.json` sits beside
the archives: the PMTiles header, the item and the style's `metadata` hold
every field a client needs.

Lossy grey tiles beat lossless ones on speckled SAR: lossless tiles are twice
the size of colour-mapped archives, while grey tiles at quality 90 are smaller
and add no more value error than colour mapping. The transport ranges are
fixed across years and set in `metadata/assets.yaml`. Changing one means
re-tiling every year; changing a default ramp does not. There are no
false-colour archives: `color-relief` colours one value, and a client can
combine polarizations itself.

**Two grids, neither derived from the other.** The annual analysis mosaics
use **ETRS89-LAEA (EPSG:3035) at 40 m**, because WebMercator is not
equal-area: a nominal 19.1 m pixel covers 13.2 m on the ground at 46.5° N,
and the factor varies across the perimeter. Area statistics on it would need
a correction the catalog does not declare. The web-map archives are tiled on
WebMercatorQuad up to z13. Both are resampled directly from the 10 m UTM
composites, so the display path never passes through LAEA and no layer is
warped twice.

**Each layer is warped once, then tiled locally.** The encoded RGBA VRT over
the display chain is copied to a local GeoTIFF without overviews, and the
tiler reads that copy. Tiling from the VRT would re-warp the remote UTM
composites for every tile at every zoom level: 174 s per layer against 10 s
from the local copy, on a Swiss test.

The tiler is in-tree (`glace_catalog.tiler`, adapted from rio-pmtiles under
its MIT license; see `THIRD_PARTY_NOTICES.md`). One worker pool serves every
layer of a year, and three output rules differ from rio-pmtiles on purpose:

- the tile image declares no nodata, so code 255 is kept rather than clamped
  to 254;
- a tile is empty only when its own footprint is, with no one-pixel margin;
- alpha is binary, and the codes beneath transparent pixels are zero.

The pmtiles fingerprint includes the tiler's identity, so changing the tiler
rebuilds every archive. The `pmtiles` package writes the archive file; this
repository owns the warp of each tile.

**The style is rendered from the vocabulary and committed.**
`mosaics/styles/default.json` holds every layer of one year. Its encodings,
ramps and stretches come from `metadata/assets.yaml`, and
`scripts/render-catalog.py` writes the file. Decisions are reviewed in the
source; the rendered file is what a client downloads. `finalize-catalog` writes
one copy per year beside the year's archives, `{year}/styles/default.json`,
registered on the mosaic item as `style-default`, and publishes the latest
year's copy as the collection's style, a recorded deviation from
`PORTO-CORE-069` ([`conformance.md`](conformance.md)). One style per year gives
every archive a layer that draws it; one tracked file per year would copy
twelve layers each and let them drift, and one file for all years would hold
144 sources with one visible. The encoding and default ramp are per product and
fixed across years, so the copies differ only in the year. Ranges, ramps and
units are written once, in `metadata/assets.yaml`, and reach the build, the
styles, the `renders` blocks, the item assets and the prose tables through one
vocabulary.

**QA ramps are sequential**, `cmc.glasgow` for both QA-NUM and QA-CQM: dark red
for few or poor, bright blue for many or good. CQM is a quality indicator where
higher is better, not a ratio around 1.0, so its range is one-sided and
measured; 0 dB has no physical meaning in the composites. Every style source
and every archive header carries one credit line, `University of Zurich,
Contains modified Copernicus Sentinel data {year}`.

**Thumbnails** show the latest year's mosaic and the `coh12_vv.tif` of tile
`32TMS24` as 1400 × 933 WebP at quality 90 over grayscale OSM, with the
provider's credit. 3:2 is the Portolan browser card's shape (guidance, not a
rashid rule), and 1400 px is twice the widest card, so a 2x display does not
upscale. Both show the full footprint in its projected proportions with at
least 5 % margins.

### How standard is any of this

| layer | status |
| --- | --- |
| MapLibre GL Style Spec v8, the file format | **De-facto standard, and mandated.** Implemented by MapLibre GL JS and Native, deck.gl, QGIS, ArcGIS, tileserver-gl, Martin, Felt. Portolan pins it by name and media type and `PTL-VIZ-005` enforces it |
| `"type": "raster-dem"` sources with `"encoding": "custom"`, `color-relief` layers | **Standard.** Style-spec v8; custom encoding since MapLibre GL JS 3.4. MapLibre Native does not decode custom encodings yet |
| the `pmtiles://` URL scheme | **Ecosystem convention, in no specification.** A client must call `addProtocol("pmtiles", …)` first. FTW writes it the same way |
| raster PMTiles styling as a whole | **Unspecified; this catalog is the first case.** Every style file in FTW and portolan-nl uses a vector source |

The container is standard and required. Raster styling is not yet in the
Portolan specification; this design may change when it is. One reviewed style
template keeps that cost bounded.

**The `renders` block.** Both collections declare `render` **v2.0.0**, the
version the Portolan profile registry pins, which places an item's `renders`
in `properties`. It carries per-layer `rescale`, `colormap_name`,
`resampling`, `nodata` and, for RTC and QA-CQM, `expression:
"10*log10(...)"`, so the catalog itself declares the decibel convention.
`colormap_name` names portable matplotlib maps (`magma`, `gray`), because a
generic client will not have cmcrameri registered; the intended cmcrameri
maps are named in each collection's `AGENTS.md`.

---

## 4. Repository and publication model

Metadata in git, data on object storage, one uploader.

| | tracked | published |
| --- | --- | --- |
| `metadata/`: `catalog.yaml`, `assets.yaml`, `templates/` | yes, the source | no |
| `catalog/catalog.json`, both `collection.json`, both `styles/` directories | yes, rendered | yes, patched |
| every `README.md` / `AGENTS.md` down to collection level | yes, rendered | yes |
| `catalog/_assets/` | yes | yes |
| items, year catalogs, `items.parquet`, thumbnails, `.tif`, `.pmtiles` | no | yes |
| `.build-state.json`, `.items.parquet`, any other dotfile | no | no |

**`catalog/` is rendered from `metadata/` and committed.** Every title,
description and display number is written once, in `metadata/assets.yaml` or
`metadata/catalog.yaml`. `scripts/render-catalog.py` writes the tracked
documents from them, and `tests/test_render.py` fails when the tracked tree
differs from a fresh render. The rendered tree stays in git for three
reasons: a reviewer reads the rendered diff, rashid and the Portolan browser
read a tree, and a tagged release then carries the STAC metadata.
The build uses the same vocabulary (`glace_catalog.metadata`) for every asset
title it writes, so an item and its collection's `item_assets` always name a
file the same way. There is no `.portolan/metadata.yaml`: `portolan add` is
not used, and `metadata/catalog.yaml` holds the title, license and contact.
Neither collection declares the projection extension: the tiles use no
`proj:*` field, and the mosaics' `proj:code` is on the items.

**The build writes only to the store.** `scripts/build-year.py` and
`scripts/finalize-catalog.py` read the composites from the archive that
`GLACE_SOURCE` names for the year and write to `GLACE_STORE_ROOT`: a prefix on
the private UZH gateway, or a local directory. `finalize-catalog` copies the
tracked documents into the store and *patches* those copies with what depends
on the build: year links, temporal and spatial extent, the latest year's style,
`file:size` and `file:checksum` on the collection assets it wrote, the column
types and row count of each item mirror, and `updated`. It leaves prose,
providers, license, `item_assets` and `renders` alone. Rendering happens at
commit time, with a reviewable diff; publication does not rewrite prose.

**The store is the published tree.** `tools/publish.py` streams every object
under `GLACE_STORE_ROOT` to the `write_prefix` in `catalog.publish.yaml`,
documents and data alike, object to object, without staging. Both sides are
`UPath` roots. An `s3://` store comes from `freezebase.make_s3_upath`, so its
listing, reads and writes share one client configuration and retry budget with
the target. Each write is committed explicitly and discarded on failure, so a
broken transfer publishes nothing. Uploading the tracked `catalog/` over the
store would replace the patched documents with unpatched ones, so nothing
does.

Change detection ([`AGENTS.md`](../AGENTS.md#publish-the-store)) uses one
listing per side and reads no bytes. Its fallback, uploading an object whose
source is newer, is the `aws s3 sync` rule; it holds because the build writes
the store before publication copies it. MD5 ETags settle equal sizes when both
sides list one, but are never required, so no writer is limited to single-part
uploads. A local store lists `mtime` and no ETags, so its equal-size objects
fall to the timestamp rule.

**Links.** Structural links are relative, so one set of files is valid in the
repository, in a preview and in production. The style sources are relative for
the same reason: a store copied to a local disk or a preview bucket draws its
own archives. Item asset hrefs are relative to the item, and the collections'
style asset hrefs are relative to the collection. The other collection asset
hrefs and the hrefs in `items.parquet` are absolute HTTPS built from
`public_base`, because a client reading one object alone has no base to resolve
against, and the Source Cooperative file listing shows asset hrefs as download
links that work only when absolute. `s3://` appears only under the `alternate`
extension. Finalization writes the root `self` link; it is not tracked, because
its correct value depends on the deployment target.

**Generated files are not committed.** The Alps have about 2 200 items a year
over twelve years. Some 26 500 generated JSON files would weigh on every clone
and every diff.

---

## 5. The source archive, checksums and band statistics

The composites come from the composite run in `deep-glacier-mapping`, which
calls the `s1bursts` writer. **No repository imports another**; they share
files. This catalog reads the composites, their run manifest and the
glacier-fraction table (`AGENTS.md` step 1), and takes MGRS tile geometry from
`freezebase`, as the pipeline does.

**`manifest.parquet` sits beside each year's composites**, one row per file,
so the build reads one file instead of opening some 26 500. The composite
driver writes it at the end of a run, beside its `config.yaml`: the bytes' own
properties are the writer's, but arranging rows in this catalog's vocabulary
and describing a resumed run belong to the run loop. A row carries the
filename and what it parses to (tile, asset key, product, QA layer,
polarization, compositing window), the size from a listing taken last, the
`sha256`, the valid percent, the band header (name, unit, data type, nodata,
resolution), the five `STATISTICS_*` values, and one column per GeoTIFF tag.
`tests/test_manifest_contract.py` holds the producer's column list verbatim,
and `read_manifest` and `survey_tags` stop the build on a manifest that
drifts. The parquet metadata carries `manifest_version = 2`, bumped when a
column changes meaning, not when one is added.

The manifest is a cache, not the source of truth: every value in it is also in
the file, or is the digest of its bytes. The driver passes `checksum=True`;
without it every `sha256` is empty and the items carry no `file:checksum`. A
resumed run describes the whole year, carrying forward the rows of the files
it skipped. To give an archive a manifest, rerun its year: the run skips
complete tiles and describes and hashes their files.

The tile composites keep the band names their writer gives them (`VV`,
`num_obs`, `cqm`). The items, `item_assets` and the mosaic COGs name each band
by its asset key, such as `coh12_vv`.

Checksums are `sha256` alone, multihash-encoded (`1220` followed by the hex),
because `file:checksum` requires that form and `PTL-DAT-001` reads the bytes
back to verify it. The writer computes the digest at creation and the run
manifest stores it already encoded, so the recorded and published values are
the same string.

The manifest carries no ETag. An ETag comes from a listing rather than being
written, and keeping it meaningful would force every upload to be
single-part. Verification uses size and presence from one listing
(`verify_manifest`) plus `rashid check --data`. A file whose digest is unknown
publishes no `file:checksum`, which Portolan permits and prefers: a
fabricated value is worse than an absent one.

The manifest also carries the five `STATISTICS_*` values per band, which
`PTL-DAT-009/010` require of the *composites*; a run whose files lack them
cannot be published. The annual mosaics
are written by this repository, so they carry their own: `build_mosaic_cog`
accumulates the five values over the source VRT, window by window, and passes
them to freezebase's `write_cog` as band tags, which sets them on the VRT
before the COG encode, the only point at which they can reach the file's
header.

---

## 6. Gates

`tests/run_all.py` runs every gate. It lists them by name rather than
globbing, so adding one is a deliberate edit. The conformance targets and what
each reads are in [`conformance.md`](conformance.md).

`tests/test_lint.py` runs `ruff format --check`, `ruff check` and
`pyrefly check` over `src/`, `scripts/`, `tools/` and `tests/`, with the same
rule set as s1bursts, so code moving between the two keeps its shape. Pyrefly
runs under `preset = "strict"`. A missing tool **fails** the gate: a skipped
gate would report green for code nothing checked.

The three gates vendored from `portolan-sdi/portolan-catalog-template`
(`tests/test_links.py`, `tests/test_stac_valid.py`,
`tests/test_conformance.py`) are excluded from ruff and pyrefly, so that
re-syncing them from the template does not conflict. Upstream's own pyflakes
pass covers them in CI.

`tests/test_data_format.py` covers what ruff cannot. Every tracked JSON
document must be in the canonical form the generator writes (two-space
indent, declaration order, one trailing newline, non-ASCII kept as is), so a
hand edit cannot hide a one-line change in a reindented diff. YAML must parse
under `safe_load` and not indent with tabs. `tests/test_conformance.py`
refuses a rashid outside `>=0.3.0,<0.4.0`, because a pass from a validator
with the wrong rule set proves nothing.

---

## 7. Releases and citation

Source Cooperative is not immutable, so a citation must name a version, and a
reader must be able to check their bytes against it. **Each version has a DOI
at LaRS**, and this repository marks it with a git tag and a GitHub release.
`file:checksum` on every item asset lets a reader check downloaded bytes
against a cited version.

Each release has one version string. `DATASET_VERSION` in
`src/glace_catalog/stac/items.py` is the source of truth; the STAC `version`
on both `collection.json` and `CITATION.cff` repeat it, and
`tests/test_version.py` fails if those four places disagree. Items get the
same string by construction: `build_tile_item` and `build_mosaic_item` default
their `version` argument to `DATASET_VERSION`. The tag `v{DATASET_VERSION}` is
a manual step of the release, outside the gate's view.

The release procedure is in [`AGENTS.md`](../AGENTS.md#release).

**The dataset DOIs come from LaRS**, set in `metadata/catalog.yaml`. The
version DOI (`10.48656/60er-3j55` for 1.0) names the bytes one
`DATASET_VERSION` describes, so it is the collections' `sci:doi` and the
target of their `rel: "cite-as"` link, beside `sci:citation`, and the `doi`
of `CITATION.cff`. The canonical DOI (`10.48656/d4r9-fj97`) names every
version: a `rel: "related"` link and an identifier in `CITATION.cff`. A new
version needs its DOI in `metadata/catalog.yaml` and `CITATION.cff` in the
same commit as the version bump; rendering and `tests/test_version.py` fail
without it.

**The LaRS record holds no data.** Its data URL is the product page on Source
Cooperative. It holds the root README as a PDF (`make landing`) and both
`items.parquet` of that version, zipped. The indexes list every data file with
its size and checksum, so a reader can check their bytes against the cited
version after the store has moved on to the next.
