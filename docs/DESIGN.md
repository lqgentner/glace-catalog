# The design of the GLACE catalog

Why the store is shaped the way it is. This is the standing record: what holds
now, and the reason it holds. What is done and what is still open is in
[`STATUS.md`](STATUS.md); what the other repositories owe this one is in
[`UPSTREAM.md`](UPSTREAM.md); the validator findings this catalog accepts are in
[`conformance.md`](conformance.md).

Reversed decisions are not kept here. Git holds the arguments that lost.

---

## 1. Identity, license and providers

Conformance is defined by passing **rashid 0.1.8** against spec v0.2.0, not by
reading the spec; its registry is the rule list and is not repeated here. The
license is `CC-BY-4.0` (`PTL-LIC-001`) and the data contains modified Copernicus
Sentinel data.

**Producer and host are both UZH**, in one provider entry carrying every role,
which is what makes the catalog official rather than a mirror: Portolan derives
that classification by comparing the two names, and a mirror owes a `via` link to
the source it copies. Listing Source Cooperative as `host` fired `PTL-PRO-001`
and `PTL-PRO-003` on the first run — a modelling error, because the spec says the
host is the organization operating this copy, "not the underlying cloud vendor
whose storage it happens to sit on". FTW lists Source Cooperative as host and
pays for it with a `via` link to its own catalog root. ESA / Copernicus is a
second producer with the `licensor` role, listed first: it operates Sentinel-1
and Copernicus terms govern reuse of the input.

---

## 2. The layout

Portolan requires a collection to own its items: a collection directory holds a
`collection.json` and one subdirectory per item, and a catalog may appear *below*
a collection to organize them. So the year directory is not shared by the two
collections, and the year sub-catalogs come for free.

```
{root}/
├── catalog.json  README.md  AGENTS.md  _assets/glace-wordmark.svg
├── tiles/
│   ├── collection.json  README.md  AGENTS.md
│   ├── items.parquet            ← collection-mirror, every year in one file
│   ├── thumbnail.png  glacier-fractions.parquet
│   └── {year}/
│       ├── catalog.json  README.md  AGENTS.md
│       ├── .build-state.json  .items.parquet     ← never published
│       └── {tile}/item.json + 12 COGs
└── mosaics/
    ├── collection.json  README.md  AGENTS.md
    ├── items.parquet  thumbnail.png
    ├── styles/{year}.json           # MapLibre GL, one per year, from the committed template
    └── {year}/item.json + 12 analysis COGs + 14 {stem}_viz.pmtiles
```

**One collection per product, not one per year.** FTW splits its per-tile
collection per year, which makes each year independently publishable and each
mirror small. Eleven collections here would be eleven copies of the same
providers, license and prose, and a cross-year query would become a
concatenation. One collection with year sub-catalogs and one mirror covering the
series is what a dataloader wants.

**Year sub-catalogs only — no third grouping level.** `PTL-CAT-001` warns at
twenty children and a year holds 143 tiles for Switzerland, ~2 209 for the Alps.
Grouping below the year does not help: every Swiss tile is in zone 32T, the Alps
span about three zones, and the MGRS 100 km square gives 9 Swiss groups of 1 to
56 tiles — the large ones still warn. Every level is a directory Portolan
requires to carry its own `catalog.json`, `README.md` and `AGENTS.md`. FTW does
group by zone and omits those files, which measures as four rashid errors per
sub-catalog against their published tree, invisible to their CI because the zone
catalogs are gitignored. The warning is the better trade, and the browse path it
protects is served by `items.parquet`.

**Nothing is qualified in a filename that the path already says** — no EPSG code,
no year, no scope. The directory says which collection and therefore which grid,
`proj:code` says it authoritatively, and the asset key is the filename stem, so a
suffix lengthens every href. The strong version of the objection is the year:
someone pulling four years of `coh12_vv.tif` into one directory collides on every
file. That is real and still not worth qualifying for — the collision is a
flattening the user performs, `odc.stac` and `rasterio` read by URL, and item ids
disambiguate. FTW and portolan-nl both qualify fully, so this is a real fork, and
we take short predictable hrefs.

**Web-map archives sit beside the rasters they render**, in the year's own item
directory, as `{stem}_viz.pmtiles`. Each is an asset of that item with the
`visual` role, which puts the rendering beside the year it renders, with that
year's footprint and window already on the item; the collection keeps the
`rel: "pmtiles"` link `PORTO-FMT-011` requires, because that is where
`pmtiles:layers` is defined. What needed disambiguating was never the CRS but the
units and role — `coh12_vv.tif` is linear float32 and `coh12_vv_viz.pmtiles` is a
pre-stretched 8-bit rendering, the backscatter ones in decibels — and the `_viz`
suffix says which is the measurement and which is the picture, in the one listing
where the pairing is visible. Registering 14 archives per year as *collection*
assets was the alternative: 56 cards on one page for Switzerland, about 150 for
the Alps, which defeats the purpose of having cards.

**Per-year run records are not published.** The manifest is the publisher's cache
and the configuration is pipeline state; both stay beside the source archive, and
every value a reader needs is on the items already.

**Every tile item of a year shares one time range.** The `COMPOSITE_*_DATE` tags
carry the compositing window, which s1bursts ≥ 0.5.0 takes from the caller and
the pipeline takes from a committed per-scope windows file. The publisher reads
that window from the manifest and requires one pair across the year: a file from
another window refuses the year rather than widening its extent, and
`published_time_range` is the single place that turns the inclusive days into
`start_datetime` / `end_datetime`. The observed span is published per product as
`glace:*_first_acquisition` / `_last_acquisition`.

**Dotfiles are build state.** `.build-state.json` and `.items.parquet`, the
year's slice of the mirror before `finalize-catalog` concatenates it, are named
with a leading dot so they stay out of the publish sync and out of a listing.

---

## 3. Visualization

**Published: twelve float32 LERC analysis COGs per year on the equal-area grid,
rendered client-side through the `renders` block, plus PMTiles archives with
MapLibre styles as the web-map product.** The mosaics collection has no year
catalogs: eleven items is under the threshold, so it links them directly.

Serving the mosaics to the web map as COGs costs about **four times the bytes**
of serving them as PMTiles, measured in `glace-viewer` against real Swiss tiles —
a wire measurement, not a file-size one, because a browser GeoTIFF reader batches
range reads into fixed blocks. The gap is the *dtype*, not the container: an
8-bit WebP COG at the same quality costs 1.37x at z11 and a float LERC one 4.00x,
and no LERC bound closes it. A single-band uint8 display COG is rejected on the
same Swiss numbers, 95 % of the LERC baseline for coherence and 49 % for
backscatter. The one real cost of PMTiles stands — the stretch is baked into
8-bit tiles, so changing a palette means re-tiling — which is why the analysis
COGs keep `renders`, where the ramp and range stay client-side.

**Two grids, neither derived from the other.** The annual analysis mosaics are
written on **ETRS89-LAEA (EPSG:3035) at 40 m**, because WebMercator is not
equal-area: a nominal 19.109 m pixel there is 13.2 m on the ground at 46.5° N and
the factor varies across the perimeter, so any area statistic would need a
correction the catalog does not declare. The web-map archives are tiled on
WebMercatorQuad at z13. Both are resampled straight from the 10 m UTM composites,
so LAEA never enters the display path and the tiler never pays a second warp.

**Each layer is warped once, then tiled locally.** The colour-mapped RGBA VRT
over the display chain is copied to a local, overview-free GeoTIFF before
tiling, and the tiler reads that. Tiling straight from the VRT was measured at
174 s per Swiss layer against 10 s from the local copy (2026-09-08 tiling
spike): every tile of every zoom re-warped the remote UTM composites, nine zoom
levels deep, so the object store was read nine times per archive. The tiler
itself is in-tree (`glace_catalog.tiler`, adapted from rio-pmtiles under its MIT
licence, see `THIRD_PARTY_NOTICES.md`): rio-pmtiles 1.2.1 leaks the overview
level it chose for a coarse tile into every finer tile the same worker renders
afterwards, so a source with overviews came out blurred at max zoom, and
running it as a subprocess meant one worker pool, one environment and one
`rio` executable per archive. The archive itself is written by the `pmtiles`
package's own writer. What is owned here is the warp of one tile.

**The style is committed, not generated.** `mosaics/styles/default.json` holds
every published layer of one year — colour, opacity and legend stops are design
decisions that belong in review. The only generated part is the year, and
`finalize-catalog` writes one published copy per year from that template:
`styles/{year}.json`, registered as `style-{year}`, the newest of them also
carrying the `default` role as Portolan requires of the one a client should load.
One style per year is what makes every `pmtiles:layers` id resolve — with a
single published style, 42 of the 56 Swiss ids named a layer no style held — and
the stretch is per product and fixed across years, so the copies differ only in
the year they name. Each layer's legend sits under `metadata.portolan:legend`, and
**the build reads those numbers rather than holding a copy**: the ranges, ramps
and units exist once, in a reviewed file.

### How standard is any of this

| layer | status |
| --- | --- |
| MapLibre GL Style Spec v8, the file format | **De-facto standard, and mandated.** Implemented by MapLibre GL JS and Native, deck.gl, QGIS, ArcGIS, tileserver-gl, Martin, Felt. Portolan pins it by name and media type and `PTL-VIZ-005` enforces it |
| `"type": "raster"` sources | **Standard.** Core to style-spec v8, as old as the format |
| the `pmtiles://` URL scheme | **Ecosystem convention, in no specification.** A client must call `addProtocol("pmtiles", …)` first. FTW writes it the same way |
| `metadata.portolan:legend` | **Not in the Portolan specification at all.** FTW's convention, copied. MapLibre's `metadata` is free-form and ignored by renderers, so the file stays valid, but nothing reads this block except code written for it |
| raster PMTiles styling as a whole | **Unspecified, and we are first.** Open under [portolan-spec#41](https://github.com/portolan-sdi/portolan-spec/issues/41); every style file in FTW and portolan-nl is a vector source |

So the container is standard and required of us; the raster application is
territory the specification has not covered, and this may have to change when #41
lands. One reviewed style template is a bounded cost. Keep the legend block aligned with FTW's
shape (`value`, `color`, `label` per stop, plus `unit`), so that if one of the two
conventions is standardized it is one convention and not two.

**The `renders` block.** Both collections declare `render` **v2.0.0**, the
version the Portolan profile registry pins, in which an item's `renders` moves
into `properties`. It carries per-layer `rescale`, `colormap_name`, `resampling`,
`nodata` and, for RTC and QA-CQM, `expression: "10*log10(...)"`, so the decibel
convention is *declared in the catalog* rather than only implemented in the
viewer. `colormap_name` names portable matplotlib maps (`magma`, `gray`) rather
than the designed cmcrameri maps, because a generic client will not have
cmcrameri registered; the designed maps are named in each collection's
`AGENTS.md`.

---

## 4. Repository and publication model

Metadata in git, data on object storage, one uploader.

| | tracked | published |
| --- | --- | --- |
| `catalog/catalog.json`, both `collection.json`, `_assets/` | yes | yes, patched |
| every `README.md` / `AGENTS.md` down to collection level | yes | yes |
| items, year catalogs, `items.parquet`, thumbnails, `.tif`, `.pmtiles` | no | yes |
| `.build-state.json`, `.items.parquet`, any other dotfile | no | no |

**The build never writes into `catalog/`.** `scripts/publish-year.py` and
`scripts/finalize-catalog.py` read the composites from the archive named by
`publish-year.py --src` and write to `GLACE_STORE_ROOT`, a prefix on the private
UZH gateway.
`finalize-catalog` stages the tracked documents into the store and *patches* the
copies there with what depends on the run — year links, temporal extent, PMTiles
links, `file:size` and `file:checksum` on the collection assets it wrote — and
never touches prose, providers, license, `item_assets` or `renders`. A generator
that rewrote descriptions would make every publication an unreviewable diff.

**The store is the published tree.** `tools/sync_data.py` streams every object
under `GLACE_STORE_ROOT` to the `write_prefix` in `catalog.publish.yaml`,
documents and data alike, object to object, with nothing on local disk and no
staging directory. Both sides are `UPath` roots from `freezebase.make_s3_upath`,
so the listing, the reads and the writes share one client configuration and one
retry budget, and the target needs only the profile: botocore reads the Source
Cooperative endpoint from that profile's config section. Syncing the tracked
`catalog/` over the store afterwards would put the unpatched documents over the
patched ones, which is why nothing does.
Change detection is size against size from one listing per side; `--force`
re-uploads, dotfiles never leave the store, and nothing is ever deleted.

**Source Cooperative is addressed through its S3 proxy.** The `source-coop`
profile carries `endpoint_url = https://data.source.coop`, and against that
endpoint the **bucket is the account** (`lqgentner`) and the **key starts with
the product** (`glace-ch/…`) — hence `write_prefix: s3://lqgentner/glace-ch`. The
AWS-native bucket names reject these credentials. Public reads work over https
and over anonymous S3 at `s3://eu-central-1.opendata.source.coop/lqgentner/…`,
the form the `alternate` hrefs carry, and range requests return 206. The proxy
blocks the `Python-urllib` user agent, so read the catalog with `requests`,
`httpx` or `fsspec` rather than `urllib.request`.

**Links.** Structural links stay relative, so one tree of bytes is valid in the
repository, in a preview and in production. Asset hrefs are absolute `https`
built from `public_base`: a client reading one object in isolation has no base to
resolve against, and the Source Cooperative file listing renders asset hrefs as
download links that only resolve when absolute. `s3://` appears only under the
`alternate` extension. The root `self` link is not in the tracked tree — the
publish step writes it, because a tracked file whose correct content depends on
the deployment target is diff noise and a merge conflict waiting to happen.

**Nothing generated is committed.** Switzerland is 143 items a year, the Alps
~2 209 across eleven years, and 24 000 generated JSON files would be paid for by
every clone and every diff. FTW does the same at 45 000 items.

---

## 5. Checksums and band statistics

`sha256` alone, multihash-encoded — `1220` followed by the hex — because that is
what `file:checksum` requires and what `PTL-DAT-001` reads the bytes back to
verify. The writer computes it at creation and the run manifest carries it
already encoded, so the value recorded and the value published are one string.
The manifest carries no ETag: an ETag is read from a listing rather than written,
and keeping one meaningful would have constrained every upload to stay
single-part. What it bought — verifying a whole prefix from a listing without
reading bytes — is replaced by size and presence from one listing
(`verify_manifest`) plus `rashid check --data`. A file whose digest is unknown
publishes no `file:checksum` at all, which Portolan permits and prefers; a
fabricated value is worse than an absent one.

The same manifest carries the five `STATISTICS_*` values per band, which
`PTL-DAT-009/010` make a blocking requirement of the *composites*: a run whose
files lack them cannot be published ([`UPSTREAM.md`](UPSTREAM.md), S-1). The
annual mosaics are not in the manifest because this repository writes them, so
they carry their own: `build_mosaic_cog` accumulates the five over the source
VRT window by window and writes them onto the band before the COG copy, which is
the only point at which they can reach the file's own header.

---

## 6. Gates

```bash
uv run tests/run_all.py                                    # every gate
uv run rashid check catalog/ --schema --no-data            # conformance, tracked tree
uv run rashid check "$GLACE_STORE_ROOT" --schema --data    # the real claim
uv run rashid check "$GLACE_STORE_ROOT" --live \
    --live-base-url https://data.source.coop/lqgentner/glace-ch
```

`tests/run_all.py` lists its gates by name rather than globbing, so adding one is
a deliberate edit. The gate that matters before publication is `--data`: it reads
asset bytes and verifies `PTL-DAT-009` and the published `file:checksum`. The
`--live` pass probes range requests and CORS on the host.

`tests/test_lint.py` runs `ruff format --check`, `ruff check` and `pyrefly check`
over `src/`, `scripts/`, `tools/` and `tests/`, with the rule set s1bursts
carries, so code moving between the two repositories does not change shape.
Pyrefly runs under `preset = "strict"`. A tool that is missing **fails** rather
than skips: a skipped gate reports a green run for code nothing read.

The three gates vendored from `portolan-sdi/portolan-catalog-template` —
`tests/test_links.py`, `tests/test_stac_valid.py`, `tests/test_conformance.py` —
are excluded from ruff and pyrefly, because reformatting them would turn every
future template re-sync into a conflict; upstream's own pyflakes pass covers them
in CI.

`tests/test_data_format.py` covers what ruff cannot: every tracked JSON document
must already be in the canonical form the generator writes (two-space indent,
declaration order, one trailing newline, non-ASCII kept as itself), or a hand
edit beside a regeneration produces a diff full of reindentation that hides the
one line that changed. YAML must parse under `safe_load` and not indent with
tabs. `tests/test_conformance.py` refuses a rashid outside `>=0.1.8,<0.2.0`,
because a green run from a validator with the wrong rule set proves nothing.

---

## 7. Releases and citation

Source Cooperative is not immutable, so a citation must name a release and a
reader must be able to verify their bytes against it. The catalog is versioned by
**git tags and GitHub releases, each archived on Zenodo for a DOI**. Zenodo
archives the repository snapshot — the metadata, from `CITATION.cff` — never the
data; `file:checksum` on every item asset is what lets a reader check downloaded
bytes against a cited release.

One version string per release. `DATASET_VERSION` in
`src/glace_catalog/stac/items.py` is the source of truth, the STAC `version` on
both `collection.json` repeats it and so does `CITATION.cff`, and
`tests/test_version.py` fails if those four places disagree. Items are not among
them because they cannot be: `build_tile_item` and `build_mosaic_item` default
their `version` argument to `DATASET_VERSION`, so an item carries the same string
by construction. The tag `v{DATASET_VERSION}` is a manual step of the release
below, not something the gate can see.

The release procedure, run by hand. Zenodo's GitHub bridge has to be enabled for
this repository on zenodo.org by its owner before step 8 mints anything.

1. Bump `DATASET_VERSION`, the two `collection.json` and `CITATION.cff`.
2. Build and finalize the store.
3. `uv run --env-file .env tools/sync_data.py --confirm`.
4. `uv run rashid check "$GLACE_STORE_ROOT" --live --live-base-url` the
   `public_base` from `catalog.publish.yaml`.
5. Commit `date-released` in `CITATION.cff`.
6. `git tag v{version}` and push the tag.
7. Create the GitHub release with the rashid summary in its notes.
8. Zenodo mints the version DOI.
9. If this was the first release, add `sci:doi` (the concept DOI) and
   `sci:citation` to the root catalog and both collections in the next commit.

The `sci:doi` / `sci:citation` fields wait for the first release deliberately: a
published placeholder DOI is worse than none. Until then STATUS.md carries the
exact fields as an open item.

---

## 8. `glace-ch` is a rehearsal; the dataset is `glace-alps`

This repository publishes Switzerland deliberately and temporarily: the Swiss
composites are complete, small enough to iterate on, and enough to prove that the
layout, the gates and the Source Cooperative upload work end to end while the
Alpine composites are produced. **Nothing published under `glace-ch` is the GLACE
dataset.** The dataset is `glace-alps`, over the Alpine Convention perimeter,
2015–2025. `src/glace_catalog/scopes.py` is the seam: everything below it takes
the scope as an argument, everything above it is written by hand for
Switzerland.

| what | where | how it changes |
| --- | --- | --- |
| scope constants | `src/glace_catalog/scopes.py` | already there: `SCOPES["alps"]`, both grids, 2 209 perimeter tiles, 45.4 % coverage. Nothing to write |
| collection ids | derived from the scope | `glace-ch-tiles` → `glace-alps-tiles`, automatic |
| publication target | `catalog.publish.yaml` | `write_prefix` and `public_base` both name `glace-ch` |
| absolute asset hrefs | both `collection.json` | built from `public_base`; every `alternate` `s3://` href too |
| root catalog identity | `catalog/catalog.json` | `id`, `title`, `description` |
| collection prose | both `collection.json` | `title`, `description`, `keywords`, `extent` |
| the documentation | 6 × `README.md` / `AGENTS.md` | written for Switzerland throughout: the perimeter, the coverage caveat, the tile counts, the worked examples' URLs |
| the perimeter caveat | `catalog/tiles/README.md`, `AGENTS.md` | "coverage is the glacier perimeter, not the country" is a Swiss sentence |
| the years | both `collection.json` extents | 2021–2024 → 2015–2025 |
| the measured numbers | `docs/` | the QA ranges and the byte ratios are Swiss |

STATUS.md carries the grep that finds them all.

**Whether the two ship as one catalog or two is still open**, and is worth
deciding before publication rather than after. Two catalogs would keep `glace-ch`
as a published rehearsal beside a separate `glace-alps` root: honest about their
different status, and pure duplication, since the Swiss data is a strict subset.
**One catalog, replaced, is the default** — `glace-alps` at a new prefix and
`glace-ch` retired once the Alpine years are published — unless someone is
already citing the Swiss URLs. The rehearsal was for exercising the pipeline, not
for having an audience, and two catalogs whose overlapping items carry different
ids for the same pixels look fine until someone joins them. Whichever is chosen,
`DATASET_VERSION` says which edition of the pixels a value belongs to, not which
scope it covers.

---

## 9. Decisions

`M-*` are the decisions taken while the catalog was designed; `R-*` those taken
in the 2026-09-08 pre-publication pass. Superseded rows are deleted rather than
struck through — git holds them.

| # | decision | § |
| --- | --- | --- |
| M-1 | Collection-owned subtree: `tiles/{year}/{tile}/`, `mosaics/{year}/` | 2 |
| M-2 | One collection per product with year sub-catalogs, not one collection per year | 2 |
| M-3 | Year sub-catalogs only; `PTL-CAT-001` accepted rather than adding two more directory levels | 2 |
| M-5 | Item mirror at `{collection}/items.parquet`, role `collection-mirror`, no `rel: "items"` link | 2 |
| M-7 | Single-band uint8 display COGs rejected on measurement — 95 %/49 % of the LERC baseline is not worth a second product | 3 |
| M-9 | `renders` v2.0.0 on both collections is the home of raster styling; `expression` declares the dB conversion | 3 |
| M-10 | `colormap_name` names portable matplotlib maps; the cmcrameri maps are named in `AGENTS.md` | 3 |
| M-11 | Producer and host are both UZH, one provider entry, so the catalog is official. Source Cooperative is the storage vendor, not the host | 1 |
| M-12 | License `CC-BY-4.0` | 1 |
| M-13 | Structural links relative, asset hrefs absolute https + `alternate` s3, `self` written by the publish step | 4 |
| M-14 | The generated tree is published and never committed | 4 |
| M-15 | `PTL-DAT-009` makes upstream band statistics a **blocking** requirement; it gates the Swiss rehearsal, not only the Alps | 5 |
| M-16 | ESA / Copernicus is a second producer with the `licensor` role; UZH keeps producer + processor + licensor + host | 1 |
| M-17 | No ETag in the manifest; `sha256` alone, multihash-encoded | 5 |
| M-18 | Lint, type and data-format gates run in `tests/run_all.py`; files vendored from the catalog template are excluded from ruff and pyrefly and covered by upstream's pyflakes pass instead | 6 |
| M-19 | **PMTiles are the web-map product**, registered as collection-level `rel: "pmtiles"` links, as `visual` item assets (M-34), with MapLibre styles as `style` assets. Measured 4x cheaper on the wire than COG; verified 0 rashid errors | 3 |
| M-20 | The false-colour RGBA WEBP COGs are dropped — PMTiles serve that view better and they have no other consumer | 3 |
| M-21 | Style files are committed, not generated: they hold colour and legend decisions that belong in review. `finalize-catalog` only repoints their year, once per published year (M-35) | 3 |
| M-23 | **No CRS, year or scope in any filename.** The path and `proj:code` carry them; the asset key stays the filename stem. A rendering carries `_viz`, which says what the file is rather than where it sits (M-34) | 2 |
| M-24 | **The annual analysis mosaic is on ETRS89-LAEA at 40 m.** WebMercator is not equal-area, so any area statistic on it needs an undeclared correction | 3 |
| M-25 | **PMTiles serve every display layer**, single-band and false colour; no WebMercator COGs. Display and analysis each resample straight from the UTM composites | 3 |
| M-26 | **`layers.json` is retired.** Every field has a standard home: the PMTiles v3 header, the `rel: "pmtiles"` link, the item, and the MapLibre style's `metadata` | 3 |
| M-27 | **The committed style is the single source of truth for the display constants, and the build reads it.** No stretch, ramp or range is copied into Python | 3 |
| M-28 | QA ramps are **sequential**: `cmc.turku` for QA-NUM, `cmc.glasgow` for QA-CQM. CQM is a quality indicator where higher is better, not a ratio around 1.0 | 3 |
| M-29 | **One credit line, read from the style**: `University of Zurich, Contains modified Copernicus Sentinel data {year}`, declared on each style source and read by the build into every archive's header | 3 |
| M-30 | **No imports between `glace-catalog` and `deep-glacier-mapping`, in either direction.** What they share is on-disk contracts: the glacier-fraction parquet, the label masks, and MGRS tile geometry, which both take from `freezebase` | 4 |
| M-31 | **The run manifest is driver work, not writer work.** `s1bursts` owns what is a property of the bytes; arranging rows in this catalog's vocabulary belongs to the composite driver | 4 |
| M-32 | **The run manifest and configuration are not published.** Both live beside the source archive; the items carry every value a reader needs | 2 |
| M-33 | **One time range per year**, from the committed compositing window; the observed span is `glace:*_first_acquisition` / `_last_acquisition` | 2 |
| M-34 | **A web-map archive is an item asset with the `visual` role**, `{stem}_viz.pmtiles` beside the COG it renders. The archives are no longer collection-level products, and the suffix keeps the stems apart. The collection-level `rel: "pmtiles"` link stays, because that is where `pmtiles:layers` lives | 2, 3 |
| M-35 | **One published style per year**, `styles/{year}.json` written from the one committed template and registered as `style-{year}`; the latest year also carries the `default` role. One tracked file per year would copy fourteen legend blocks per year and let them drift; one file for every year would be 154 sources with one visible | 3 |
| R-1 | `src/glace_catalog/__init__.py` exists — a real package, not a namespace package | 6 |
| R-2 | pyrefly `strict` replaces mypy, configured as in s1bursts; vendored files excluded | 6 |
| R-3 | PMTiles are **linked** at collection level only: `build_mosaic_item` has no `pmtiles` parameter and an item carries no `rel: "pmtiles"` link. The archive is an asset of the year item all the same (M-34) — the link is what stays collection-level, because that is where `pmtiles:layers` is defined | 3 |
| R-4 | No `[tool.uv.sources]` overrides. `freezebase[s3]>=0.7.1,<0.8` from PyPI; 0.7.1 carries `S3Settings`, `list_object_sizes`, `atomic_write_text`, `content_type_for` and the profile-configured endpoint (UPSTREAM.md F-5) | 6 |
| R-5 | The old UZH prefix `s3://s1-bursts/data/glace-ch` is being deleted by the user; the `ch` scope's default root stays that value and the fresh build lands there once it is empty | 4 |
| R-6 | No staging directory. Publication streams object-to-object from `GLACE_STORE_ROOT` to Source Cooperative through `tools/sync_data.py`, `UPath` to `UPath`; no boto3 client is built by hand. Each write is committed by hand and discarded on failure, so a broken transfer publishes nothing | 4 |
| R-17 | The S3 settings class, the one-listing size map, the atomic write, the streamed sha256 and the content-type table live in `freezebase`, shared with `deep-glacier-mapping`; this repository keeps only the `1220` multihash prefix and the `GLACE_S3_` prefix | 8 |
| R-7 | Source Cooperative is addressed through its S3 proxy: the bucket is the account, the key starts with the product, `write_prefix` is `s3://lqgentner/glace-ch` | 4 |
| R-8 | The published tree is the **store**, not the tracked `catalog/`. `tools/publish.py` is deleted; syncing the tracked tree would strip `finalize-catalog`'s patches | 4 |
| R-9 | Thumbnails are cut from the latest year's `coh12_rgb.pmtiles` archive, so they obey the committed style by construction: the whole scope for the mosaics, tile `32TMS24` (Aletsch) for the tiles, both framed 3:2 | 3 |
| R-10 | The root catalog carries a `rel: "icon"` link to `_assets/glace-wordmark.svg` | 2 |
| R-11 | No `.portolan/metadata.yaml`. It is the input of `portolan add`, which this repository does not use; a second copy of title, license and contact is drift waiting to happen | 4 |
| R-12 | **Warp once per layer, tile locally.** The RGBA intermediate is a local overview-free GeoTIFF; the tiler is in-tree (`glace_catalog.tiler`, from rio-pmtiles, MIT) and shares one worker pool across the year's layers; `rio-pmtiles` is no longer a dependency | 3 |
| R-12 | The item mirror satisfies `PTL-DAT-006/007/008/016` as rashid implements them: Hilbert-ordered rows, a float64 `bbox` covering column, 2 048-row groups | 2 |
| R-13 | `RasterInfo.statistics` is a typed `BandStatistics` record; a missing field is a `ValueError` at manifest read, not a `KeyError` at publication | 5 |
| R-14 | `docs/DESIGN.md` and `docs/STATUS.md` replace `MIGRATION.md`; `UPSTREAM.md` keeps only the on-disk contracts | — |
| R-15 | `uv.lock` is tracked, so a fresh clone resolves to the dependency set CI ran | 6 |
| R-16 | **The catalog is versioned by git tags and GitHub releases, each archived on Zenodo for a DOI.** One version string per release, `DATASET_VERSION`; `sci:doi` / `sci:citation` are added after the first release mints the concept DOI | 7 |
