# The design of the GLACE catalog

Why the store is shaped the way it is: what holds now, and why. Current state
and open work are in [`STATUS.md`](STATUS.md); what the other repositories owe
this one is in [`UPSTREAM.md`](UPSTREAM.md); the validator findings this
catalog accepts are in [`conformance.md`](conformance.md).

Reversed decisions are not kept here; git has them.

---

## 1. Identity, license and providers

Conformance means passing **rashid 0.1.8** against spec v0.2.0. Its rule
registry is the rule list, so it is not repeated here. The license is
`CC-BY-4.0` (`PTL-LIC-001`), and the data contains modified Copernicus
Sentinel data.

**UZH is both producer and host**, in one provider entry carrying every role.
Portolan compares the producer and host names to decide whether a catalog is
official or a mirror, and a mirror owes a `via` link to its source. Source
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
│   ├── thumbnail.webp  glacier-fractions.parquet
│   └── {year}/
│       ├── catalog.json  README.md  AGENTS.md
│       ├── .build-state.json  .items.parquet     ← never published
│       └── {tile}/item.json + 12 COGs
└── mosaics/
    ├── collection.json  README.md  AGENTS.md
    ├── items.parquet  thumbnail.webp
    ├── styles/{year}.json           # MapLibre GL, one per year, from the committed template
    └── {year}/item.json + 12 analysis COGs + 12 {stem}_viz.pmtiles
```

**One collection per product, not one per year.** Per-year collections (as in
Fields of the World, FTW) make each year independently publishable. Here they
would mean twelve copies of the same providers, license and prose, and a
cross-year query would become a concatenation. A dataloader wants one
collection with year sub-catalogs and one mirror covering the series.

**Year sub-catalogs only, no third grouping level.** A year holds up to 2 210
tiles, so `PTL-CAT-001` (twenty children) warns on each year. A further level does not fix this and costs three
generated files per directory; [`conformance.md`](conformance.md) gives the
full reasoning. `items.parquet` serves the browse path the rule protects.

**Filenames carry no EPSG code, year or scope.** The directory names the
collection and therefore the grid, `proj:code` states it, and the asset key is
the filename stem, so a suffix would only lengthen every href. The strongest
objection is the year: copying four years of `coh12_vv.tif` into one
directory collides. That flattening is the user's choice; `odc.stac` and
`rasterio` read by URL, and item ids disambiguate. FTW and portolan-nl qualify
fully; this catalog prefers short, predictable hrefs.

**Web-map archives sit beside the rasters they render**, as
`{stem}_viz.pmtiles` in the year's item directory. Each is an asset of that
item with the `visual` role, so it carries the year's footprint and window.
The `_viz` suffix separates the measurement from the picture in the one
listing where both appear: `coh12_vv.tif` is linear float32, while
`coh12_vv_viz.pmtiles` holds lossy 8-bit codes, in decibels for backscatter.
A client lists the archives from the year items and draws them through the
year's style, as layer `glace-{stem}-{year}`. The collection carries no
`rel: "pmtiles"` links, a recorded deviation from `PORTO-FMT-011` (M-36,
[`conformance.md`](conformance.md)). Registering the archives as *collection*
assets would put 144 cards on one page.

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

**Dotfiles are build state.** `.build-state.json` and `.items.parquet` (the
year's slice of the mirror, before `finalize-catalog` combines them) start
with a dot so publishing skips them.

---

## 3. Visualization

**Published: twelve float32 LERC analysis COGs per year on the equal-area
grid, drawn client-side through the `renders` block, plus PMTiles archives
with MapLibre styles as the web-map product.** The mosaics collection links its
twelve items directly, below the twenty-child threshold for sub-catalogs.

**PMTiles, not COGs, for the web map.** Served to the web map as COGs, the
mosaics cost about **four times the bytes** on the wire, measured in
`glace-viewer` on Swiss tiles. (A browser GeoTIFF reader fetches fixed-size
blocks, so file sizes understate the difference.) The cost comes from the
data type, not the container: at z11 an 8-bit WebP COG costs 1.4x the PMTiles
bytes and a float LERC COG 4x, and no LERC error bound closes the gap. A
single-band uint8 display COG saves too little to justify a second product:
95 % of the LERC baseline for coherence, 49 % for backscatter (M-7).

**The archives carry values, not colours.** Each tile holds an 8-bit code,
repeated in all three channels and linear over a fixed *transport range* per
layer. Code 0 is nodata, with a binary alpha band. Tiles are lossy WebP at
quality 90. The year style declares the decoding as a MapLibre `raster-dem`
source with `"encoding": "custom"` and draws it with a `color-relief` layer
over a default ramp and stretch. A client can recolour, restretch and read
values without new tiles. The item asset declares the same decoding as
`raster:scale` and `raster:offset` on its band.

Two MapLibre GL JS behaviours fix the custom encoding's channel factors.
`color-relief` packs its stops by treating the three factors as positional
digits, so none may be zero. Decoding is a dot product over the channels, so
one code step is split 65536 : 256 : 1 across a grey pixel. MapLibre also
subtracts `baseShift`, although the specification says it is added.

Lossy grey tiles beat lossless ones: on speckled SAR, lossless tiles were
twice the size of colour-mapped archives, while grey tiles at quality 90 were
smaller and added no more value error than colour mapping did. The transport
ranges are fixed across years and set in `metadata/assets.yaml`. Changing one
means re-tiling every year; changing a default ramp does not. There are no
false-colour archives: `color-relief` colours one value, and a client can
combine polarizations itself. MapLibre Native does not yet decode custom
encodings.

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
tiler reads that copy. Tiling straight from the VRT re-warps the remote UTM
composites for every tile at every zoom level; in a Swiss test that took
174 s per layer against 10 s from the local copy.

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
`scripts/render-catalog.py` writes the file (R-18). Decisions are reviewed in
the source; the rendered file is what a client downloads. `finalize-catalog`
writes one copy per year, `styles/{year}.json`, registered as `style-{year}`.
The newest also carries the `default` role, which Portolan requires on the
style a client should load. One style per year gives every archive a layer
that draws it. The encoding and default ramp are per product and fixed across
years, so the copies differ only in the year. Ranges, ramps and units are
written once, in `metadata/assets.yaml`, and reach the build, the styles, the
`renders` blocks, the item assets and the prose tables through one
vocabulary.

### How standard is any of this

| layer | status |
| --- | --- |
| MapLibre GL Style Spec v8, the file format | **De-facto standard, and mandated.** Implemented by MapLibre GL JS and Native, deck.gl, QGIS, ArcGIS, tileserver-gl, Martin, Felt. Portolan pins it by name and media type and `PTL-VIZ-005` enforces it |
| `"type": "raster-dem"` sources with `"encoding": "custom"`, `color-relief` layers | **Standard.** Style-spec v8; custom encoding since MapLibre GL JS 3.4; both verified in GL JS 6.9. MapLibre Native does not decode custom encodings yet |
| the `pmtiles://` URL scheme | **Ecosystem convention, in no specification.** A client must call `addProtocol("pmtiles", …)` first. FTW writes it the same way |
| raster PMTiles styling as a whole | **Unspecified; this catalog is the first case.** Open under [portolan-spec#41](https://github.com/portolan-sdi/portolan-spec/issues/41); every style file in FTW and portolan-nl uses a vector source |

The container is standard and required. Raster styling is not yet covered by
the specification, and this design may have to change when #41 is settled.
One reviewed style template keeps that cost bounded.

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
| `catalog/catalog.json`, both `collection.json`, `mosaics/styles/default.json` | yes, rendered | yes, patched |
| every `README.md` / `AGENTS.md` down to collection level | yes, rendered | yes |
| `catalog/_assets/` | yes | yes |
| items, year catalogs, `items.parquet`, thumbnails, `.tif`, `.pmtiles` | no | yes |
| `.build-state.json`, `.items.parquet`, any other dotfile | no | no |

**`catalog/` is rendered from `metadata/` and committed (R-18).** Every title,
description and display number is written once, in `metadata/assets.yaml` or
`metadata/catalog.yaml`. `scripts/render-catalog.py` writes the tracked
documents from them, and `tests/test_render.py` fails when the tracked tree
differs from a fresh render. The rendered tree stays in git for three
reasons: a reviewer reads the rendered diff, rashid and the Portolan browser
read a tree, and a tagged release then carries the STAC metadata.
The build uses the same vocabulary (`glace_catalog.metadata`) for every asset
title it writes, so an item and its collection's `item_assets` always name a
file the same way.

**The build writes only to the store.** `scripts/build-year.py` and
`scripts/finalize-catalog.py` read the composites from the archive that
`GLACE_SOURCE` names for the year and write to `GLACE_STORE_ROOT`: a prefix
on the private UZH gateway, or a local directory. `finalize-catalog` copies the
tracked documents into the store and *patches* those copies with what
depends on the build: year links, temporal extent, the per-year style
entries, and `file:size` and `file:checksum` on the collection assets it
wrote. It leaves prose, providers, license, `item_assets` and `renders`
alone. Rendering happens at commit time, with a reviewable diff; publication
does not rewrite prose.

**The store is the published tree.** `tools/publish.py` streams every object
under `GLACE_STORE_ROOT` to the `write_prefix` in `catalog.publish.yaml`,
documents and data alike, object to object, without staging. Both sides are
`UPath` roots. An `s3://` store comes from `freezebase.make_s3_upath`, so its
listing, reads and writes share one client configuration and retry budget
with the target. The target needs only the profile: botocore reads the Source
Cooperative endpoint from that profile's config section. Uploading the tracked
`catalog/` over the store would replace the patched documents with unpatched
ones, so nothing does.

Change detection uses one listing per side and reads no bytes. A different
size is a change. At equal sizes, two single-part ETags are two MD5s and
settle it. Otherwise the object is changed only when the source is newer than
its published copy, the `aws s3 sync` rule, which holds because the build
writes the store before publication copies it. ETags are used when both sides
list one, never required, so no writer is limited to single-part uploads. A
local store lists `mtime` and no ETags, so its equal-size objects fall to the
timestamp rule. `--force` re-uploads; dotfiles stay in the store; nothing is
deleted.

**Source Cooperative is addressed through its S3 proxy.** The `source-coop`
profile sets `endpoint_url = https://data.source.coop`. At that endpoint the
**bucket is the account** (`giuz`) and the **key starts with the product**
(`glace-alps/…`), hence `write_prefix: s3://giuz/glace-alps`. The AWS-native
bucket names reject these credentials. Public reads work over HTTPS and over
anonymous S3 at `s3://eu-central-1.opendata.source.coop/giuz/…`, the form the
`alternate` hrefs use; range requests return 206. The proxy blocks the
`Python-urllib` user agent, so read the catalog with `requests`, `httpx` or
`fsspec`, not `urllib.request`.

**Links.** Structural links are relative, so one set of files is valid in the
repository, in a preview and in production. The style sources are relative
for the same reason: a store copied to a local disk or a preview bucket draws
its own archives. Item asset hrefs are relative to the item, and the mosaics
collection's style asset hrefs are relative to the collection. The other
collection asset hrefs and the hrefs in `items.parquet` are absolute HTTPS
built from `public_base`, because a client reading one object alone has no
base to resolve against, and the Source Cooperative file listing shows asset
hrefs as download links that work only when absolute. `s3://` appears only
under the `alternate` extension. Finalization writes the root `self`
link; it is not tracked, because its correct value depends on the deployment
target.

**Generated files are not committed.** The Alps have about 2 200 items a year
over twelve years. Some 26 500 generated JSON files would weigh on every clone
and every diff. FTW does the same at 45 000 items.

---

## 5. Checksums and band statistics

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
cannot be published ([`UPSTREAM.md`](UPSTREAM.md), S-1). The annual mosaics
are written by this repository, so they carry their own: `build_mosaic_cog`
accumulates the five values over the source VRT, window by window, and writes
them onto the band before the COG copy, the only point at which they can
reach the file's header.

---

## 6. Gates

```bash
uv run tests/run_all.py                                    # every gate
uv run rashid check catalog/ --schema --no-data            # conformance, tracked tree
uv run rashid check "$GLACE_STORE_ROOT" --schema --data    # the real claim
uv run rashid check "$GLACE_STORE_ROOT" --live \
    --live-base-url https://data.source.coop/giuz/glace-alps
```

`tests/run_all.py` lists its gates by name rather than globbing, so adding one
is a deliberate edit. The gate that matters before publication is `--data`: it
reads asset bytes and verifies `PTL-DAT-009` and the published
`file:checksum`. The `--live` pass probes range requests and CORS on the host.

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
refuses a rashid outside `>=0.1.8,<0.2.0`, because a pass from a validator
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

The release procedure, run by hand:

1. Bump `DATASET_VERSION` and `CITATION.cff`, set the new version DOI in
   `metadata/catalog.yaml`, and re-render.
2. Build and finalize the store, and run `make conformance`.
3. `make publish-plan`, then `make publish`.
4. `make conformance-live`.
5. Commit `date-released` in `CITATION.cff`.
6. `git tag v{version}` and push the tag.
7. Create the GitHub release with the rashid summary in its notes.

**The dataset DOIs come from LaRS**, set in `metadata/catalog.yaml`. The
version DOI (`10.48656/60er-3j55` for 1.0) names the bytes one
`DATASET_VERSION` describes, so it is the collections' `sci:doi` and the
target of their `rel: "cite-as"` link, beside `sci:citation`, and the `doi`
of `CITATION.cff`. The canonical DOI (`10.48656/d4r9-fj97`) names every
version: a `rel: "related"` link and an identifier in `CITATION.cff`. A new
version needs its DOI in `metadata/catalog.yaml` and `CITATION.cff` in the
same commit as the version bump; rendering and `tests/test_version.py` fail
without it.

---

## 8. Scopes

The dataset is `glace-alps`: the 2 210 MGRS tiles of the Alpine Convention
perimeter (2025 edition), 2015–2026. It is the scope `metadata/catalog.yaml`
names. `src/glace_catalog/scopes.py` also defines `glace-ch`, 143 tiles over the
glacierised Swiss Alps, which is small enough to test the pipeline on; nothing
built under it is the GLACE dataset. Everything below `scopes.py` takes the
scope as an argument.

A switch of scope changes `scope`, `region`, `years`, `title` and the
preliminary notice in `metadata/catalog.yaml`, and `write_prefix` and
`public_base` in `catalog.publish.yaml`; `uv run scripts/render-catalog.py`
then rewrites `catalog/`. Collection ids and absolute hrefs follow
automatically. The default stretches and transport ranges in
`metadata/assets.yaml` are measured on the scope's own display mosaics.

---

## 9. Decisions

`M-*` are the decisions taken while the catalog was designed; `R-*` those
taken in the 2026-09-08 pre-publication pass. Superseded rows are deleted
rather than struck through; git keeps them.

| # | decision | § |
| --- | --- | --- |
| M-1 | Collection-owned subtree: `tiles/{year}/{tile}/`, `mosaics/{year}/` | 2 |
| M-2 | One collection per product with year sub-catalogs, not one collection per year | 2 |
| M-3 | Year sub-catalogs only; `PTL-CAT-001` accepted rather than adding two more directory levels | 2 |
| M-5 | Item mirror at `{collection}/items.parquet`, role `collection-mirror`, no `rel: "items"` link | 2 |
| M-7 | No single-band uint8 display COGs: at 95 % (coherence) and 49 % (backscatter) of the LERC baseline, they do not justify a second product | 3 |
| M-9 | `renders` v2.0.0 on both collections is the home of raster styling; `expression` declares the dB conversion | 3 |
| M-10 | `colormap_name` names portable matplotlib maps; the cmcrameri maps are named in `AGENTS.md` | 3 |
| M-11 | UZH is both producer and host, in one provider entry, so the catalog is official. Source Cooperative is the storage vendor, not the host | 1 |
| M-12 | License `CC-BY-4.0` | 1 |
| M-13 | Structural links, item asset hrefs and style asset hrefs relative; the other collection asset hrefs and the `items.parquet` hrefs absolute HTTPS + `alternate` s3; `self` written by finalization | 4 |
| M-14 | The generated tree is published and never committed | 4 |
| M-15 | `PTL-DAT-009` makes upstream band statistics a **blocking** requirement | 5 |
| M-16 | ESA / Copernicus is a second producer with the `licensor` role; UZH keeps producer + processor + licensor + host | 1 |
| M-17 | No ETag in the manifest; `sha256` alone, multihash-encoded | 5 |
| M-18 | Lint, type and data-format gates run in `tests/run_all.py`; files vendored from the catalog template are excluded from ruff and pyrefly and covered by upstream's pyflakes pass | 6 |
| M-19 | **PMTiles are the web-map product**, registered as `visual` item assets (M-34), with MapLibre styles as `style` assets: about 4x cheaper on the wire than COGs; 0 rashid errors | 3 |
| M-20 | No false-colour products, neither RGBA WebP COGs nor archives: PMTiles serve the web map, `color-relief` colours one value, and clients can combine polarizations themselves (M-37) | 3 |
| M-21 | The style template is committed, rendered from `metadata/assets.yaml` (R-18), where colour and legend decisions are reviewed; `finalize-catalog` only sets its year, once per published year (M-35) | 3 |
| M-23 | **No CRS, year or scope in any filename.** The path and `proj:code` carry them; the asset key is the filename stem. A rendering carries `_viz`, which says what the file is rather than where it sits (M-34) | 2 |
| M-24 | **The annual analysis mosaic is on ETRS89-LAEA at 40 m.** WebMercator is not equal-area, so any area statistic on it needs an undeclared correction | 3 |
| M-25 | **PMTiles serve every display layer**; no WebMercator COGs. Display and analysis each resample straight from the UTM composites | 3 |
| M-26 | **No `layers.json`.** Every field has a standard home: the PMTiles v3 header, the item, and the MapLibre style's `metadata` | 3 |
| M-27 | **The display constants live once, in `metadata/assets.yaml`.** The build reads the transport ranges through the vocabulary; the style, `renders` and prose carry the default ramps and stretches. No range or ramp is copied into Python | 3 |
| M-28 | QA ramps are **sequential**, `cmc.glasgow` for both QA-NUM and QA-CQM: dark red for few or poor, bright blue for many or good. CQM is a quality indicator where higher is better, not a ratio around 1.0, so its range is one-sided and measured; 0 dB has no physical meaning in the composites | 3 |
| M-29 | **One credit line**: `University of Zurich, Contains modified Copernicus Sentinel data {year}`, one constant written on each style source and into every archive's header | 3 |
| M-30 | **No imports between `glace-catalog` and `deep-glacier-mapping`, in either direction.** They share on-disk contracts: the glacier-fraction parquet, the label masks, and MGRS tile geometry, which both take from `freezebase` | 4 |
| M-31 | **The run manifest is driver work, not writer work.** `s1bursts` owns what is a property of the bytes; arranging rows in this catalog's vocabulary belongs to the composite driver | 4 |
| M-32 | **The run manifest and configuration are not published.** Both live beside the source archive; the items carry every value a reader needs | 2 |
| M-33 | **One time range per year**, from the committed compositing window; the observed span is `glace:*_first_acquisition` / `_last_acquisition` | 2 |
| M-34 | **A web-map archive is an item asset with the `visual` role**, `{stem}_viz.pmtiles` beside the COG it renders; the suffix keeps the stems apart | 2, 3 |
| M-35 | **One published style per year**, `styles/{year}.json`, written from the one committed template and registered as `style-{year}`; the latest year also carries the `default` role. One tracked file per year would copy twelve layers per year and let them drift; one file for all years would hold 144 sources with one visible | 3 |
| M-36 | **The collection carries no `rel: "pmtiles"` links**, a recorded deviation from `PORTO-FMT-011`. The archives are used like the COGs, twelve a year, and the rule has one vector archive per collection in mind; 144 links on one collection page serve nobody. The year items list the archives, the year styles draw them, and `finalize-catalog` refuses an archive its year's style does not draw | 2, 3 |
| M-37 | **The web-map archives carry values, not colours**: 8-bit codes over a fixed transport range per layer, lossy grey WebP at quality 90 with a binary alpha, decoded by the style's `raster-dem` custom encoding and the item band's `raster:scale` / `raster:offset`, drawn by `color-relief` over a default ramp. No false-colour archive. Ramps and stretches are client defaults; no build option chooses an encoding | 2, 3 |
| R-1 | `src/glace_catalog/__init__.py` exists: a real package, not a namespace package | 6 |
| R-2 | pyrefly `strict` is the type checker, configured as in s1bursts; vendored files excluded | 6 |
| R-3 | A mosaic item carries no `rel: "pmtiles"` link; `build_mosaic_item` has no `pmtiles` parameter. The archive is an asset of the year item (M-34) | 3 |
| R-4 | No `[tool.uv.sources]` overrides. `freezebase[s3]>=0.7.1,<0.8` from PyPI, which provides `S3Settings`, `list_object_sizes`, `atomic_write_text`, `content_type_for` and the profile-configured endpoint (UPSTREAM.md F-5) | 6 |
| R-6 | No staging directory. Publication streams object to object from `GLACE_STORE_ROOT`, local or on the bucket, to Source Cooperative through `tools/publish.py`, `UPath` to `UPath`, with no hand-built boto3 client. Each write is committed explicitly and discarded on failure, so a broken transfer publishes nothing | 4 |
| R-17 | The S3 settings class, the one-listing size map, the atomic write, the streamed sha256 and the content-type table live in `freezebase`, shared with `deep-glacier-mapping`; this repository keeps only the `1220` multihash prefix and the `GLACE_S3_` prefix | 8 |
| R-7 | Source Cooperative is addressed through its S3 proxy: the bucket is the account and the key starts with the product, so `write_prefix` is `s3://giuz/glace-alps` | 4 |
| R-8 | The published tree is the **store**, not the tracked `catalog/`; `tools/publish.py` is the only uploader. Uploading the tracked tree would strip `finalize-catalog`'s patches | 4 |
| R-9 | Thumbnails render the latest year's mosaic and tile `32TMS24` `coh12_vv.tif` COGs as 1400 × 933 WebP (quality 90) over grayscale OSM, with the provider's credit. 3:2 is the Portolan browser card's shape (guidance, not a rashid rule), so other aspects are letterboxed or cropped; 1400 px is twice the widest card, so a 2x display does not upscale. The figure has a fixed width and the dpi sets the pixel size, so the credit keeps its proportion. Both thumbnails show the full footprint in its projected proportions with at least 5 % margins | 3 |
| R-10 | The root catalog carries a `rel: "icon"` link to `_assets/glace-wordmark.svg` | 2 |
| R-11 | No `.portolan/metadata.yaml`, since `portolan add` is not used here. Title, license and contact live in `metadata/catalog.yaml`, the only copy (R-18) | 4 |
| R-12 | **Warp once per layer, tile locally.** The RGBA intermediate is a local GeoTIFF without overviews; the tiler is in-tree (`glace_catalog.tiler`, from rio-pmtiles, MIT) and shares one worker pool across the year's layers | 3 |
| R-19 | The item mirror satisfies `PTL-DAT-006/007/008/016` as rashid implements them: Hilbert-ordered rows, a float64 `bbox` covering column, 2 048-row groups | 2 |
| R-13 | `RasterInfo.statistics` is a typed `BandStatistics` record; a missing field is a `ValueError` at manifest read, not a `KeyError` at publication | 5 |
| R-14 | The docs are split: `DESIGN.md` for decisions, `STATUS.md` for state and open work, `UPSTREAM.md` for the on-disk contracts with other repositories | — |
| R-15 | `uv.lock` is tracked, so a fresh clone resolves to the dependency set CI ran | 6 |
| R-16 | **The catalog is versioned by git tags and GitHub releases, with a LaRS DOI per version.** One version string per release, `DATASET_VERSION`. The LaRS version DOI is `sci:doi`, with `sci:citation`, a `cite-as` link and the `CITATION.cff` DOI; the canonical DOI is a `related` link | 7 |
| R-18 | **`catalog/` is rendered from `metadata/` and committed under a regeneration gate.** `assets.yaml` names and describes every asset once and holds every range and ramp; `catalog.yaml` holds identity and prose; `templates/` the markdown. The build reads the same vocabulary for item titles, archive descriptions and link titles. Neither collection declares the projection extension: the tiles use no `proj:*` field and the mosaics' `proj:code` is on the items | 4 |
