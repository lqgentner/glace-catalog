# GLACE → Portolan: the transition

> **`glace-ch` is a rehearsal, not the dataset.** Everything in this repository
> currently describes Switzerland, and none of it is the product that ships. It
> exists to exercise publication — the layout, the gates, the Source Cooperative
> upload — while GLACE Alps is still being produced. **The dataset is
> `glace-alps`**, over the Alpine Convention perimeter, and every mention of
> Switzerland here is placeholder text with a removal date. §11 is the checklist.

**Living document.** Status as of **2026-09-03**. Update it in the same commit
as the change it describes; a stale transition record is worse than none,
because the next person trusts it.

Moving the GLACE store from a flat, self-designed STAC tree on a private bucket
to a **Portolan-conformant, git-backed catalog** published on Source
Cooperative. The metadata lives here and is versioned; the data stays on the
private bucket or on `/mnt` staging and is uploaded separately.

**Working in `s1bursts`, `freezebase` or `deep-glacier-mapping`?** Read
[`UPSTREAM.md`](UPSTREAM.md) instead — it is the per-repository work order.
This document is the design record.

Prior art this replaces: `deep-glacier-mapping/cache/stac-store-refactor/`
(`PLAN.md`, `IMPLEMENTATION.md`, `FTW-COMPARISON.md`). Everything still true has
been carried across; §10 lists what changed and why.

---

## 1. Where things stand

| | state |
| --- | --- |
| repository | scaffolded from `portolan-sdi/portolan-catalog-template`, all gates green |
| catalog documents | root + both collections written, **0 rashid errors** |
| layout | redesigned to collection-owned, **validated against a generated fixture** |
| pipeline code | **copied** from `deep-glacier-mapping`, namespace rewritten, `StoreLayout` reworked. Originals still in place until P-3 (§7) |
| generators | **not yet updated** to the new layout — this is the main remaining work (§6) |
| published store | still the old flat tree at `s3://s1-bursts/data/glace-ch` |
| scope | **`glace-ch`, a rehearsal.** The dataset is `glace-alps`; see §11 |

```
0 error(s), 14 warning(s), 0 info(s) across 7 files      # fixture, 2-item year
0 error(s), 15 warning(s), 0 info(s) across 30 files     # fixture, 25-item year
```

Both warning sets are recorded in [`conformance.md`](conformance.md). Nothing
found so far is a design problem.

---

## 2. What Portolan actually forces

Read from the spec at v0.2.0 and from **rashid 0.1.8's rule set**, which is the
gate and therefore the operative definition. Only the requirements that changed
a GLACE decision are listed; the full rule list is in rashid's registry.

| rule | severity | what it forced |
| --- | --- | --- |
| core: collection directories | — | **the whole layout change** (§3) |
| `PTL-AST-005` | error | a catalog carries no assets — only collections and items. Killed the plan to hang each year's manifest off its year catalog |
| `PTL-AST-002` | error | absolute asset hrefs must be `https`, never `s3://`. `s3` moves to the `alternate` extension |
| `PTL-DAT-009/010` | error | **every COG band** carries embedded min/max/mean/stddev, and valid-percent where there is nodata. The single hardest requirement for GLACE (§7) |
| `PTL-DAT-013` | error | internal tiles square and ≤512 px. Already satisfied: 512 on tiles, 256 on mosaics |
| `PTL-DAT-011` | error | overviews down to one tile. The Alps mosaic at 41 984 × 69 120 needs 9 levels; assert it in the writer |
| `PTL-COL-005` | error | the item mirror does **not** replace item JSON. Every item is published as a document |
| `PTL-MIR-002` | error | the mirror is a collection asset with role `collection-mirror`, media type `application/vnd.apache.parquet`. No `rel: "items"` link |
| `PTL-DAT-006/7/8` | error | the mirror is GeoParquet under the full vector rules: spatially ordered, per-row-group statistics, ≤150 000 rows per group |
| `PTL-FIL-001/002/003` | error | `README.md` + `AGENTS.md` in **every** catalog and collection directory, linked `describedby` / `agents` |
| `PTL-PRV-001/002/003` | error | ≥1 `producer`, exactly one `host` listed last, host reachable by url or email |
| `PTL-VIZ-001` | error | every geospatial collection carries a thumbnail asset |
| `PTL-VIZ-002/003/005` | error | **the reason raster PMTiles lost** (§4) |
| `PTL-LIC-001` | error | SPDX license. `CC-BY-4.0` |

### The one that bit unexpectedly

`PTL-PRO-001` and `PTL-PRO-003` fired on the first run, demanding a `via` link to
an upstream source and an `updated` sync timestamp. Cause: providers listed
UZH as `producer` and Source Cooperative as `host`, and Portolan derives
*official vs mirror* by comparing those two names — different names means mirror,
and a mirror must link the source it copies.

**FTW hits this too, and pays for it.** Every Fields of the World collection
lists Source Cooperative as `host` against a different producer, so every one is
classified a mirror and carries the `via` link that forces. On
`predictions/confidence` that link points at `data.source.coop/ftw/global-data/`
— its own catalog root. A self-referential `via` is a workaround for the
classification, not provenance, which is the tell that the modelling is being
bent rather than expressed.

That was a modelling error, not a validator quirk. The spec says the host is
*"the organization responsible for operating and maintaining this copy of the
catalog and its data, **not the underlying cloud vendor** whose storage it
happens to sit on"* — Source Cooperative is the vendor. GLACE is the originating
authority, so producer and host are both UZH, in one provider entry carrying
every role. The catalog is official and carries no `via`.

**Correction worth flagging:** the answer to "who is the host contact" was
originally given as the Source Cooperative profile. Recorded here as UZH at
`https://www.geo.uzh.ch/en.html`; Source Cooperative is named in the provider
description as the storage, and corrections route through the repository's
`issues` link.

**ESA / Copernicus is a second producer**, roles `["producer", "licensor"]`,
listed first. It operates Sentinel-1 and distributes the SLC data these
composites derive from, and Copernicus terms govern reuse of that input. FTW
names it the same way. Adding it does not change the official classification:
Portolan asks only whether *some* producer shares the host's name, and UZH is
still both.

---

## 3. The layout, and why it changed

Portolan requires collections to own their items:

> A collection directory MUST contain a `collection.json` and holds one
> subdirectory per item. […] a catalog may also appear below a collection to
> organize its items (for example, a raster collection grouping items by year).

The old store split the *documents* (`tiles/`, `mosaics/`) from the *layout*
(`{year}/` holding both collections' files), on the reasoning that collections
need not own contiguous subtrees. Portolan closes that door. The year directory
loses its unity — which was a convenience, never a requirement — and the year
catalogs that `PTL-CAT-001` wants come for free.

```
{root}/
├── catalog.json  README.md  AGENTS.md  _assets/
├── tiles/
│   ├── collection.json  README.md  AGENTS.md
│   ├── items.parquet            ← collection-mirror, every year in one file
│   ├── thumbnail.png  glacier-fractions.parquet
│   └── {year}/
│       ├── catalog.json  README.md  AGENTS.md
│       ├── manifest.parquet  config.yaml
│       ├── .build-state.json  .items.parquet     ← never published
│       └── {tile}/item.json + 12 COGs
└── mosaics/
    ├── collection.json  README.md  AGENTS.md
    ├── items.parquet  thumbnail.png
    ├── styles/{name}.json          # MapLibre GL, committed
    ├── pmtiles/{year}/{layer}.pmtiles
    └── {year}/item.json + 12 analysis COGs
```

Decisions inside that, and the reasoning:

**One collection per product, not one per year.** FTW splits its per-tile
collection per year (`features/2024`, `features/2025`), which makes each year
independently publishable and each mirror small. Rejected: eleven collections
means eleven copies of the same providers, license, README and AGENTS prose, and
a cross-year query becomes a concatenation. One collection with year
sub-catalogs and one mirror covering the series is what a dataloader wants.

**Year sub-catalogs only — no third grouping level.** `PTL-CAT-001` warns at
twenty children, and a year holds 143 tiles for CH and ~2 209 for the Alps.

FTW does group below the year: `features/2025/collection.json` carries 60
`child` links, one per UTM zone, each zone catalog holding one child per MGRS
latitude band. It looks like the answer, and it is worth being precise about
what it costs, because the cost is invisible in their repository.

**Those sub-catalogs carry no `README.md` and no `AGENTS.md`.** Verified against
the published tree on 2026-09-03: `features/2025/32/catalog.json` has 16 links,
none of them `agents` or `describedby`, and both markdown files return 404.
`PTL-FIL-001` iterates *every* catalog and collection node and requires both
files in its directory; `PTL-FIL-002` and `PTL-FIL-003` require the links.
Reproducing FTW's exact shape in a fixture here gives **four errors per
sub-catalog** — two for the missing files, one per missing link — plus a
`PTL-SCH-001` schema failure. FTW's CI cannot see this: the zone catalogs are
generated and gitignored, so rashid only ever runs over the tracked tree. It is
the same blind spot recorded in [`conformance.md`](conformance.md), landing on a
node that the tracked tree does not contain at all.

So FTW's grouping is not a pattern to copy; it is non-conformance that their
gate cannot reach. Generating the two documents per level would fix it, and for
them that is 60 zones × ~10 bands × 2 years × 2 files.

**And the grouping would not help GLACE regardless.** FTW gets its mileage from
being global. Every Swiss tile is in zone 32T — measured, 143 of 143 — so a zone
level is one child holding all 143 items and the warning does not move. The Alps
span roughly three zones. The only level with real fan-out here is the MGRS
100 km square, which for CH gives 9 groups of 1 to 56 tiles: the large ones still
warn, and a fourth level to get under twenty would add hundreds of generated
directories for the Alps, each owing a `catalog.json`, a `README.md` and an
`AGENTS.md` whose content would restate its own path.

Taking one warning per year avoids all of that. The browse path the rule protects
is served by `items.parquet` instead, which is what every piece of documentation
here points consumers at. Revisit if browsing proves painful — and if so, group
*and* generate the markdown, rather than doing what FTW did.

**Nothing is qualified in a filename that the path already says.** No EPSG
code, no year, no scope. The question comes up because a tile COG sits in its
own UTM zone while a mosaic COG is EPSG:3857, so `coh12_vv.tif` names two
different grids in two places.

Against putting it in the name: the directory already says which collection and
therefore which grid; `proj:code` on every item says it authoritatively and
machine-readably; for tiles the code *varies per tile*, and the tile directory
(`32TLR48`) already encodes the zone, so `coh12_vv_32632.tif` would be
redundant noise; and it lengthens every href and every asset key, because rule 1
makes the key the stem.

The stronger version of the question is the year, not the CRS — someone pulling
four years of `coh12_vv.tif` into one directory to difference them collides on
every file, which no EPSG suffix would have prevented. That is real, and it is
still not worth qualifying for: the collision is a flattening a user performs,
`odc.stac` and `rasterio` read by URL and never flatten, and item ids
(`ch-mosaic-2024`) already disambiguate. Fields of the World and portolan-nl
both qualify fully (`luchtfoto-2024-19an1-rgb.tif`), so this is a real fork —
the trade is short predictable hrefs and `assets[key].href == f"{key}.tif"`
against flatten-safety, and we take the former.

What *does* need disambiguating is not the CRS but the **units and role**, which
is why the PMTiles moved rather than gained a suffix. A blanket `_db` suffix
would have been wrong anyway: only `rtc_*` and `*_qa_cqm` are decibels;
`coh12_*` are linear values under a stretch.

**The mosaics collection has no year catalogs.** Eleven items, under the
threshold, so the collection links them directly and `mosaics/{year}/` is simply
the item's own subdirectory.

**Web-map archives sit apart from the rasters they render.** `mosaics/pmtiles/
{year}/coh12_vv.pmtiles`, not `mosaics/{year}/coh12_vv.pmtiles`. Two reasons,
and the second is the one that matters: they are registered as *collection*-level
links, so an item's own directory is the wrong place for them; and beside the
COG the stems collide — `coh12_vv.tif` is linear float32 and `coh12_vv.pmtiles`
is a pre-stretched 8-bit rendering, the backscatter ones in decibels. Two files
one letter apart holding different quantities reads fine until someone compares
them.

#### Why not FTW's shape: two rasters in one item directory

FTW's confidence item does what a reader would expect here — one directory, one
item, two rasters distinguished by role and filename:

| asset | roles | file | grid | dtype |
| --- | --- | --- | --- | --- |
| `data` | `data`, `cloud-optimized` | `prue_v1_confidence_global.tif` | EPSG:4326 | float32 |
| `visual` | `visual`, `overview`, `cloud-optimized` | `prue_v1_confidence_global_uint8_3857.tif` | EPSG:3857 | uint8 |

That pattern is open to us **only while the display product is a COG**, which is
exactly the RGBA WEBP design M-20 dropped. It is not open to PMTiles: Portolan
registers those through a *collection*-level `rel: "pmtiles"` link
(`PORTO-FMT-011`), and where a PMTiles is additionally an asset it is a
collection-level asset too — FTW's vectors collection does both, both at
collection level. A per-year archive therefore cannot be an asset of the year
item, and putting the file inside that item's directory while registering it one
level up would be incoherent.

So the directory split is a consequence of choosing PMTiles, not a principle. It
is worth being clear about that, because it is the *cost* side of a decision the
measurement otherwise won outright.

**One genuine mismatch, worth naming.** Portolan's visualization model assumes
one derivative per collection. FTW fits it: their confidence collection holds a
single global item, so collection-level and item-level are nearly the same
place. Ours does not — 14 layers × 11 years is 154 collection-level links
describing products that each belong to one item. The registration is
conformant and the links carry titles, but the collection document is doing
bookkeeping the spec did not shape it for. If `PORTO-FMT-011` ever admits
item-level registration, this is the catalog that wants it.

#### A refinement on filenames

FTW disambiguates its two same-directory rasters with `_uint8_3857` — dtype
*and* EPSG code, in the filename. That is worth recording next to M-23, because
it is a real counter-example used for a real reason.

It does not overturn M-23, but it narrows it. Qualifying a **primary** asset is
redundant: the path says which collection and grid, and `proj:code` says it
authoritatively. Qualifying a **derivative that sits beside its primary** is not
redundant at all — it is the only thing separating two files that describe the
same measurement in different units on different grids. FTW needs the suffix
because they kept both in one directory; we do not, because the directory does
that work instead. If a COG display product ever returns to an item directory
here, it takes FTW's naming with it.

**Per-year run records are collection assets.** `PTL-AST-005` forbids assets on a
catalog, so the year catalog cannot own the manifest that sits beside it. The
files stay at `tiles/{year}/manifest.parquet` — under the collection whose data
they record — and register as collection assets keyed `manifest-{year}` /
`config-{year}`, role `metadata`. This is strictly better than the old plan's
placement on the mosaic item, which put a record of the tiles in the other
collection and needed a `describedby` link on all 24 000 tile items to
compensate. Those links are dropped.

**Dotfiles are build state.** `.build-state.json` and `.items.parquet` (the
year's slice of the mirror, before `finalize-catalog` concatenates and rebases
it) are named with a leading dot so they stay out of the publish sync and out of
a directory listing.

---

## 4. Visualization: the decision, and what settled it

**Published: 12 float32 LERC analysis COGs per year, rendered client-side
through the `renders` block, plus PMTiles archives with MapLibre styles as the
web-map product.**

> **This reverses an earlier decision in this document (2026-09-03).** PMTiles
> were retired on the reading that Portolan's rules for them are vector-shaped
> and that a raster archive could not satisfy them honestly. That reading was
> wrong on the decisive point, and a measurement settled the rest. Both
> corrections are below, because the reasoning that produced the wrong call is
> worth keeping visible.

### What the measurement said

Serving the mosaics to the web map as COGs costs **about four times the bytes**
of serving them as PMTiles, measured in `glace-viewer` against real tiles. That
is a wire measurement, not a file-size one: a browser GeoTIFF reader batches
range reads into fixed blocks, so it fetches more than the viewport needs, and
the gap is wider over a network than any local comparison shows.

Four times is not a margin a catalog can argue away. PMTiles is the display
product.

### Why the compliance objection was wrong

Three rules govern raster PMTiles, and rashid enforces all three. Each was read
as a blocker; none is.

- **`PTL-VIZ-003` requires a non-empty `pmtiles:layers`.** The objection was
  that a raster archive has no layers, so the array would be a placeholder. It
  is not: the web-map-links v1.3.0 schema leaves the field optional and untyped
  beyond "array of strings", and a MapLibre style over a raster source **does**
  have layers. `glace-viewer` already names them — `glace-pmtiles-coh12_vv_2023`
  — so the array names the style layer a client switches on. That is the field
  used as intended.
- **`PTL-VIZ-002` and `PTL-VIZ-005` require a MapLibre GL style asset.** Read as
  a tax. It is the opposite: `js/map.js` already builds these layer definitions
  at runtime, so publishing them externalises the viewer's own configuration
  into a standard file that something other than `glace-viewer` can load. It is
  the same argument that moved raster styling out of `layers.json` and into
  `renders`, one product over.
- **Collection-level registration.** 14 layers × 11 years is 154 links on one
  document. Verbose, and legal.

Verified rather than argued: a fixture registering raster PMTiles this way —
`rel: "pmtiles"` links carrying `pmtiles:layers`, web-map-links v1.3.0 declared,
MapLibre styles as `style` assets with exactly one `default` — returns **0
errors** from rashid.

The one real cost stands: the stretch is baked into 8-bit tiles, so changing a
palette means re-tiling. That is why the analysis COGs keep `renders`, where the
ramp and range stay client-side and cost nothing to change.

### What this retires

The false-colour RGBA WEBP COGs (`coh12_rgb`, `rtc_rgb`) are dropped from
`item_assets` and from `renders`. They existed only to serve false colour once
PMTiles were retired; PMTiles serve the same view at a quarter of the bytes, in
a format built for it, so the derivative has no consumer left. Measured at
0.32 B/px (coh12) and 0.44 B/px (rtc), they were a good answer to the wrong
question.

That also removes the dB-versus-linear wrinkle from the `data` role entirely:
every COG in the catalog is linear, and the only decibel pixels in the store are
in PMTiles archives that are labelled renderings.

### What the measurements ruled out, and still do

A single-band uint8 display COG remains rejected, on the same numbers as before:
95 % of the LERC baseline for coherence and 49 % for backscatter is not worth a
second product when the float32 original serves the same view through `renders`.
Lossier LERC is also still rejected — 2.5e-3 on coherence saves 19 %, 1.5e-4 on
backscatter saves 27 %, neither worth loosening a bound anchored to the data's
own measurement noise.

### The styles

Four MapLibre GL style files under `mosaics/styles/`, committed rather than
generated: they encode colour, opacity and legend stops, which are design
decisions that belong in review. `default.json` carries both `style` and
`default` roles, as Portolan requires when there is more than one.

The only generated part is the year each style's `pmtiles://` source names;
`finalize-catalog` repoints them at the most recent published year.

#### How standard is any of this

Worth being precise, because the layers differ and only some of it is settled.

| layer | status |
| --- | --- |
| MapLibre GL Style Spec v8, the file format | **De-facto standard, and mandated.** Forked from the Mapbox GL Style Spec; implemented by MapLibre GL JS and Native, deck.gl, QGIS, ArcGIS, tileserver-gl, Martin, Felt. Portolan pins it by name and media type (`formats.md`), and `PTL-VIZ-005` enforces the media type. Not our choice to make |
| `"type": "raster"` sources | **Standard.** Raster sources are core to style-spec v8, as old as the format |
| the `pmtiles://` URL scheme | **Ecosystem convention, in no specification.** It is the `pmtiles.js` protocol handler, so a client must call `addProtocol("pmtiles", …)` before it can load these files. FTW writes it the same way (`pmtiles://./Unknown.pmtiles`) |
| `metadata.portolan:legend` | **Not in the Portolan specification at all** — grepped, it appears nowhere in `portolan-spec`. It is FTW's convention and we copied it. MapLibre's `metadata` is explicitly free-form and ignored by renderers, so the file stays valid, but nothing reads this block except code written for it |
| raster PMTiles styling as a whole | **Unspecified, and we are first.** Portolan's raster styling is open under [portolan-spec#41](https://github.com/portolan-sdi/portolan-spec/issues/41), and its style requirements are written for vector. Every style file in FTW and portolan-nl is a vector source; there is no raster precedent to follow |

So the container is standard and required of us; the raster application of it is
territory the specification has not covered yet. Two consequences worth holding:

- **This may have to change when #41 lands.** It is a small, generated-adjacent
  surface — four files — so the cost of being early is bounded, and being early
  with a real raster case is more useful to that discussion than waiting.
- **The legend block earns its place only by convention.** Keep it aligned with
  FTW's shape (`value`, `color`, `label` per stop, plus `unit`) so that if one of
  the two conventions is standardized, it is one convention and not two.

The honest test of whether these files are more than a viewer config: would
anything else load them? Yes — any MapLibre GL JS or Native client, the Portolan
browser, tileserver-gl — but **only after registering the pmtiles protocol**.
That is a real limit, and it is the same limit every PMTiles publisher has.

Each style carries its legend under `metadata.portolan:legend`, which is where
the `LEGEND_STOPS` plumbing goes — out of `layers.json` and into a file MapLibre
loads directly.

### What the `renders` block does

Both collections declare `render` **v2.0.0** (the version the Portolan profile
registry pins; FTW still uses v1.0.0, and in v2.0.0 an item's `renders` moves
into `properties`). It carries per-layer `rescale`, `colormap_name`,
`resampling`, `nodata`, and — for RTC and QA-CQM — `expression:
"10*log10(...)"`, so the decibel convention is *declared in the catalog* rather
than only implemented in the viewer.

`colormap_name` names portable matplotlib maps (`magma`, `gray`, `viridis`,
`rdbu`) rather than the designed cmcrameri maps (`cmc.lipari`, `cmc.navia`),
because a generic client will not have cmcrameri registered. The designed maps
are named in each collection's `AGENTS.md`.

---

## 5. Repository model

Metadata in git, data in object storage, two uploaders that do not overlap.

| | tracked | published |
| --- | --- | --- |
| `catalog/catalog.json`, both `collection.json` | yes | yes |
| every `README.md` / `AGENTS.md` down to collection level | yes | yes |
| `tiles/{year}/**`, `mosaics/{year}/**`, `items.parquet`, thumbnails | **no** | yes |
| `.tif`, run manifests, `.build-state.json` | no | data only |

The generated tree is written into `catalog/` by the build and uploaded from
there, never committed: CH is 143 items a year, the Alps ~2 209 across eleven
years, and 24 000 generated JSON files would be paid for by every clone and
every diff. FTW does the same at 45 000 items.

**Links.** Structural links stay relative, so one tree of bytes is valid in the
repository, in a preview and in production. Asset hrefs are absolute `https`
built from `public_base` — a client reading one object in isolation has no base
to resolve against, and the Source Cooperative file listing renders asset hrefs
as download links that only resolve when absolute. `s3://` appears only under
the `alternate` extension. The root `self` link is **not** in the tracked tree;
the publish step writes it, because a tracked file whose correct content depends
on the deployment target is diff noise and a merge conflict waiting to happen.

**Where the data is.** `GLACE_STORE_ROOT` decides: an `s3://` prefix on the
private UZH bucket, or a staging directory under `/mnt`. Nothing else in the
pipeline changes between them — a staging build is the same bytes in a cheaper
place. `catalog.publish.yaml`'s `data_dir` repeats it for `tools/upload_data.py`,
which does not import the config module; keep the two in agreement.

---

## 6. Remaining work

Ordered by what blocks what.

### Blocking publication

- [ ] **P-1 — Embedded band statistics.** `PTL-DAT-009` and `PTL-DAT-010` are
      errors, and the completed Swiss 2021–2024 composites have **empty band
      tags**. Implemented upstream (`s1bursts.product_tags.band_statistics`,
      `ResolutionWeighter._save`) but uncommitted and unreleased. The existing
      archive needs a restat pass — ~2 GB per Swiss year, cheap; impossible for
      the Alps at 31.5 GB × 11, which is why it moved upstream. **Order matters:
      restat first, then rebuild the manifest**, because a rewrite changes both
      the sizes and the digests the manifest records. This is the single largest
      gate between here and a conformant publication.
- [ ] **P-2 — Item mirror under the GeoParquet rules.** `PTL-DAT-006/7/8` apply
      to an item mirror exactly as to a vector dataset: spatially ordered rows, a
      `bbox` covering column with per-row-group statistics, ≤150 000 rows per
      group. The current `export_geoparquet` does none of the three.
- [x] **P-3 — Generators ported.** Done 2026-09-03. Verified by driving the
      generators over synthetic items and validating the output: **0 rashid
      errors across 8 objects**, with every structural link resolved by hand at
      both depths. What changed:

      - **The collection documents became source, not output.**
        `build_tiles_collection` and `build_mosaics_collection` are deleted;
        `finalize-catalog` now *patches* the tracked documents, adding only what
        depends on the run (year links, per-year manifest assets, temporal
        extent, PMTiles links) and never touching the prose, providers, license,
        `item_assets`, `renders` or style registrations. A generator that
        rewrote the descriptions would make every publication an unreviewable
        diff and put the dataset's prose in Python string literals.
      - Item documents at two depths, with different parents: a tile item's
        parent is its **year sub-catalog**, a mosaic item's is the collection.
      - Year sub-catalogs generated with their `README.md` and `AGENTS.md`,
        which `PTL-FIL-001` requires of every catalog directory.
      - The `describedby` link to the manifest is gone from all 24 000 tile
        items (M-4), and the manifest and configuration are off the mosaic item.
      - `file:checksum` and full band statistics plumbed through `RasterInfo`.
      - Asset roles aligned with the committed `item_assets`: `["data",
        "cloud-optimized"]` and `["quality"]`, not `["data", "backscatter"]` and
        `["metadata", "quality"]`.
      - `layers.json` and its writer deleted — `renders` and the MapLibre styles
        replaced it.
- [ ] **P-4 — Thumbnails.** `PTL-VIZ-001` is an error and both collections need
      one, generated from the default styling.
- [x] **P-5 — PMTiles registration.** `finalize-catalog` now writes the
      per-year `rel: "pmtiles"` links and repoints the four style files at the
      most recent published year. The tiler itself was never deleted. Still
      unrun at scale (P-8).
- [ ] **P-5b — Tiler output path.** `publish-year --steps pmtiles` uploads to
      `layout.pmtiles(year)`, which is now `mosaics/pmtiles/{year}/`; unverified
      against a real run.
- [ ] ~~**P-5 — PMTiles step and style generation.**~~ The tiler in `webmap.py`
      was never deleted, so this is mostly restoring what M-6 had marked for
      removal: `build_pmtiles`, `run_rio_pmtiles`, `create_rgba_vrt` and the
      `rio-pmtiles` dependency all stay. New work is the per-year
      `rel: "pmtiles"` links on the mosaics collection, and `finalize-catalog`
      repointing the four style files at the most recent published year.

### Before the Alps

- [ ] **P-6 — Release freezebase and s1bursts, drop the editable overrides.** An
      editable path in `[tool.uv.sources]` is not reproducible from a checkout.
      Now also carries `subprocess_s3_env` (F-2). See [`UPSTREAM.md`](UPSTREAM.md).
- [ ] **P-7 — Overview-depth assertion.** `PTL-DAT-011` wants overviews down to
      one tile; the Alps mosaic at 41 984 × 69 120 needs 9 levels and the CH test
      COG stopped at 7.
- [ ] **P-8 — Spikes S3 and S5 at scale.** S3 (a 2 209-source VRT chain on S3)
      is still the most likely thing to force a design change. S5 (`rio pmtiles`
      reading `/vsis3` with `AWS_PROFILE` only) is back in scope now that
      PMTiles are, though `subprocess_s3_env` already implements it.

### Worth doing, not blocking

- [ ] **P-9 — `llms.txt` per collection**, linked `rel: "llms"`. FTW ships one
      per collection *and* per item at 1–7 kB.
- [ ] **P-10 — `grid:code`** (`MGRS-32TLS`) on tile items — one line, buys
      interop with Planetary Computer and Earth Search.
- [ ] **P-11 — Per-item thumbnails on mosaic items.** ~12 renders a year makes
      the catalog legible in any browser.
- [ ] **P-12 — Catalog logo** in `_assets/`, linked `rel: "icon"`. Registries
      list catalogs side by side.
- [ ] **P-17b — Track [portolan-spec#41](https://github.com/portolan-sdi/portolan-spec/issues/41)
      (raster styling).** This catalog is the first Portolan raster PMTiles case;
      the four style files and the `renders` blocks are both candidate inputs to
      that discussion, and both may need changing when it settles.
- [ ] **P-13 — Register in the Portolan registry** once published
      (`portolan:register-catalog`).
- [ ] **P-14 — Test `@developmentseed/deck.gl-geotiff`** against a LERC_ZSTD
      NaN-nodata mosaic before hardening the viewer's zero-sentinel workaround.
      The Portolan browser uses it, not geotiff.js, and it depends on `lerc`
      directly — if it preserves the validity mask the workaround disappears.
- [ ] **P-15 — Labels catalog.** Left in `deep-glacier-mapping` for now
      (`build-labels-catalog.py`, `rasterize-labels.py`). It is STAC creation and
      belongs here eventually, as a third collection or a sibling catalog.
- [ ] **P-16 — ~~Rename `webmap.py`~~.** Dropped: it builds web maps after all.
- [ ] **P-17 — Delete the originals from `deep-glacier-mapping`** once P-3 lands
      and this repository can build a store: `scopes.py`, `stac/`, `mosaics.py`,
      `webmap.py`, `gdal_s3.py`'s subprocess helper, and `scripts/stac/`. Until
      then both copies exist and only the old one works (§7).

### Checksums: sha256 only

The old plan carried **both** an ETag and a sha256 per file, for two different
jobs (D-25): the ETag because it is what an S3 *listing* returns, so a whole
prefix could be verified without reading a byte; the sha256 because that is what
`file:checksum` publishes.

Only sha256 survives. The reasoning:

- **`file:checksum` requires it and rashid verifies it.** Portolan mandates
  multihash encoding — `1220` followed by the sha256 hex — and `PTL-DAT-001`
  reads the bytes back and hashes them. So sha256 is not optional, and it is
  what makes a *content* claim.
- **The ETag was never a freezebase change; it was a freezebase constraint.**
  It is read from a listing after upload, so no writer code produces it. What it
  imposed was "keep the upload single-part, so the ETag stays the MD5" — because
  a multipart ETag is an MD5 of MD5s and compares to nothing computable locally.
  Dropping it frees the writer to upload however it likes. That is the
  simplification.
- **It was gateway-specific.** Ceph RGW happens to preserve the ETag through a
  server-side `CopyObject`, verified once on 1 716 objects. That is a property of
  one gateway, not of S3.

What is lost, stated plainly: verifying a whole prefix from one listing without
reading bytes. `verify_manifest` is now a **presence and size check** and its
docstring says so. Nothing that mattered goes with it:

| what the ETag guarded | what guards it now |
| --- | --- |
| the source archive matching its manifest | size and presence from one listing; content by `rashid check --data` against the published `file:checksum` |
| per-file copy idempotency | size. The copy is a server-side `CopyObject`, byte-exact by construction, so a destination of the right size came from this same copy |
| a source replaced behind our back | the build-state fingerprint, which now keys on filenames, sizes and digests, and reruns the whole step when any move |

`sha256` is stored in the manifest **already multihash-encoded**, not as bare
hex, so the value written and the value published are the same string. It is
`None` for an archive written before the writer emitted digests; those assets
then publish no `file:checksum` at all, which Portolan permits and prefers — a
fabricated value is worse than an absent one.

---

## 7. What moved, and what did not

**The move is staged, not finished.** The code below is copied into this
repository with its namespace rewritten, and the originals are still in
`deep-glacier-mapping`. They stay there until P-3 lands, because until the
generators write the new layout this repository cannot build a store and that
one still can. Deleting the working copy first would leave nothing functional in
either place. Remove them in the same commit that closes P-3.

Copied to `glace-catalog`, namespace rewritten `glacier_mapping` → `glace_catalog`:

```
src/glace_catalog/scopes.py        canonical grids, extents, collection ids
src/glace_catalog/stac/            naming, tags, items, store, save
src/glace_catalog/mosaics.py       VRT chain + mosaic COGs
src/glace_catalog/webmap.py        LUT machinery (P-16 renames it)
src/glace_catalog/gdal_s3.py       subprocess S3 environment
src/glace_catalog/config.py        rewritten for this repo's three locations
scripts/publish-year.py  finalize-catalog.py  build-manifest.py
```

Stayed in `deep-glacier-mapping`, because they depend on `datasets.py` and
`geo.py` and sit on the inventory side rather than the store side:
`derive-glacier-fractions.py`, `rasterize-labels.py`, `export-inventories.py`,
`build-labels-catalog.py`. The first produces `glacier-fractions.parquet`, which
this catalog consumes as a collection asset — a file, not an import. See P-15.

`config.py` was rewritten rather than copied. It reads `GLACE_SOURCE_ROOT` and
`GLACE_STORE_ROOT` and knows nothing about Source Cooperative: the publish
target lives in `catalog.publish.yaml` and is read only by `tools/publish.py`,
so a credential for the public bucket cannot be picked up by a build step that
only meant to read a composite.

---

## 8. How to check your work

```bash
python3 tests/run_all.py                              # every gate
python3 tests/test_lint.py                            # ruff format, ruff check, mypy
python3 tests/test_data_format.py                     # JSON and YAML well-formedness
uv run rashid check catalog/ --schema --no-data       # conformance, tracked tree
uv run rashid check "$GLACE_STORE_ROOT" --schema --data   # the real claim
uv run rashid check "$GLACE_STORE_ROOT" --live --live-base-url "$PUBLIC_BASE"
```

The gate that matters is `--data`: it reads asset bytes and is what verifies
`PTL-DAT-009` and the published `file:checksum`. The `--live` pass probes range
requests and CORS on the host.

### Lint, types and data formats

`tests/test_lint.py` runs `ruff format --check`, `ruff check` and `mypy` over
`src/`, `scripts/`, `tools/` and `tests/`, with the same rule set
deep-glacier-mapping uses, so code moving between the two repositories does not
change shape on the way. One deliberate difference: `DOC` is not selected here,
because ruff implements those rules only under `preview` and selecting it
otherwise does nothing but print a warning on every run.

A tool that is missing **fails** rather than skips. A skipped gate reports a
green run for code that nothing read.

The files vendored from the catalog template — both publishers and the four
gates that came with them — are excluded from ruff and mypy. Reformatting them
would turn every future template re-sync into a conflict, and enumerating
per-rule ignores for code we do not own is a list that grows on someone else's
schedule. They stay covered by upstream's own pyflakes pass in CI and by
`test_publish.py` / `test_upload_data.py`, which assert their behaviour directly.

`tests/test_data_format.py` covers what ruff cannot. There is no `ruff format`
for JSON or YAML worth a dependency, so it checks directly:

- **JSON** — every tracked document must already be in the canonical form the
  generator writes (two-space indent, declaration order, one trailing newline,
  non-ASCII kept as itself). Most of these are machine-written and the rest are
  hand-edited beside them; without a canonical form, a hand edit and a
  regeneration produce a diff full of reindentation that hides the one line that
  actually changed.
- **YAML** — must parse under `safe_load`, and must not indent with tabs, which
  is the most common YAML error and reports far from its cause.

Both run offline against git-tracked files only, so generated output is skipped.
`yamllint` and `check-jsonschema` are the off-the-shelf alternatives; neither
was worth a dependency for two YAML files and three JSON documents, and neither
enforces the canonical-form property, which is the one that actually protects
review.

Do not read a green run as proof a validator read the catalog until you have
seen the version it reports. `tests/test_conformance.py` refuses a rashid
outside `>=0.1.8,<0.2.0` for exactly this reason: 0.1.4 has no `PTL-LNK-007`
through `PTL-LNK-009` or `PTL-AST-006`, and 0.1.5–0.1.7 report errors for things
spec v0.2.0 allows.

---

## 9. Decisions

Superseding the `PLAN.md` decision log where they conflict.

| # | decision | § |
| --- | --- | --- |
| M-1 | Collection-owned subtree: `tiles/{year}/{tile}/`, `mosaics/{year}/`. Supersedes D-3's flat `{year}/` | 3 |
| M-2 | One collection per product with year sub-catalogs, not one collection per year | 3 |
| M-3 | Year sub-catalogs only; `PTL-CAT-001` accepted rather than adding two more directory levels | 3 |
| M-4 | Per-year manifest and config are **tiles-collection** assets keyed `manifest-{year}`; the mosaic-item placement and the 24 000 `describedby` links are dropped. Supersedes D-18 | 3 |
| M-5 | Item mirror at `{collection}/items.parquet`, role `collection-mirror`, no `rel: "items"` link. Supersedes D-6's root-level `tiles.parquet` with role `stac-items` | 3 |
| M-6 | ~~Raster PMTiles retired~~ **superseded by M-19** | 4 |
| M-7 | Single-band uint8 display COGs rejected on measurement — 95 %/49 % of the LERC baseline is not worth a second product | 4 |
| M-8 | ~~`rtc_rgb` is in dB~~ **moot under M-19**: the only decibel pixels left are inside PMTiles archives | 4 |
| M-9 | `renders` v2.0.0 on both collections replaces `DEFAULT_STYLES` and `layers.json` as the home of raster styling; `expression` declares the dB conversion | 4 |
| M-10 | `colormap_name` names portable matplotlib maps; the cmcrameri maps are named in `AGENTS.md` | 4 |
| M-11 | Producer and host are both UZH, one provider entry, so the catalog is official. Source Cooperative is the storage vendor, not the host | 2 |
| M-12 | License `CC-BY-4.0` | 2 |
| M-13 | Structural links relative, asset hrefs absolute https + `alternate` s3, `self` written by the publish step | 5 |
| M-14 | Generated item tree is published but never committed | 5 |
| M-15 | `PTL-DAT-009` makes upstream band statistics a **blocking** requirement, not the optional nicety D-24 made it. It gates the CH rehearsal, not just the Alps | 6 |
| M-16 | ESA / Copernicus is a second producer with the `licensor` role; UZH keeps producer + processor + licensor + host | 2 |
| M-17 | **ETag is dropped from the manifest**; `sha256` alone, multihash-encoded. Supersedes D-25's "both, for different jobs" | 6 |
| M-19 | **PMTiles are the web-map product**, registered as collection-level `rel: "pmtiles"` links with MapLibre styles as `style` assets. Measured 4x cheaper on the wire than COG; verified 0 rashid errors. Reverses M-6 | 4 |
| M-20 | The false-colour RGBA WEBP COGs are dropped — PMTiles serve that view better and they have no other consumer | 4 |
| M-22 | Web-map archives live at `mosaics/pmtiles/{year}/`, not beside the COGs they render: they are collection-level products, and the stems would otherwise collide across two different quantities | 3 |
| M-23 | **No CRS, year or scope in any filename.** The path and `proj:code` carry them; the asset key stays the filename stem | 3 |
| M-21 | Style files are committed, not generated: they hold colour and legend decisions that belong in review. `finalize-catalog` only repoints their year | 4 |
| M-18 | Lint, type and data-format gates run in `tests/run_all.py`; files vendored from the catalog template are excluded from ruff and mypy and covered by upstream's pyflakes pass instead | 8 |

---

## 10. What changed from the old plan

For anyone holding the `stac-store-refactor` documents.

| old | now | why |
| --- | --- | --- |
| `{year}/{tile}/` shared by both collections | `tiles/{year}/{tile}/` | Portolan requires a collection to own its items |
| `tiles.parquet` at root, role `stac-items` | `tiles/items.parquet`, role `collection-mirror` | `PTL-MIR-002`; `stac-items` is a Planetary Computer habit |
| PMTiles as `rel: "pmtiles"` links | kept, and now with MapLibre styles | the rules are satisfiable and PMTiles is 4x cheaper on the wire; §4 |
| `layers.json` + `DEFAULT_STYLES` | `renders` v2.0.0 on both collections | a standard home has existed since 2023 |
| band statistics gate the Alps only (D-24) | gate everything | `PTL-DAT-009` is an error |
| manifest as mosaic-item asset + `describedby` on every tile item (D-18) | tiles-collection asset keyed by year | `PTL-AST-005`; simpler and semantically right |
| no prose in the store | README + AGENTS at every level | `PTL-FIL-001/002/003` |
| relative asset hrefs throughout | absolute https + `alternate` s3 | `PTL-AST-002`, and the Source Cooperative listing |
| §14's COG-vs-PMTiles benchmark still open | settled by measurement | §4 |
| branch B's LAEA 40 m grid | not taken | WebMercator z13 serves both roles; the false-colour COG closes the display gap |


---

## 11. `glace-ch` is a rehearsal; the dataset is `glace-alps`

This repository publishes Switzerland. That is deliberate and temporary: the
Swiss composites are complete, small enough to iterate on, and enough to prove
that the layout, the gates and the Source Cooperative upload work end to end
while the Alpine composites are still being produced.

**Nothing published under `glace-ch` is the GLACE dataset.** The dataset is
`glace-alps`, over the Alpine Convention perimeter, 2015–2025. Every mention of
Switzerland in this repository is placeholder text, and the pipeline was built
so that swapping them is a scope change and not a rewrite: `scopes.py` already
carries both scopes as constants, with the Alpine grid, extent, perimeter tile
count and coverage fraction already committed and already derived.

### What actually has to change

`scopes.py` is the seam. Everything below it takes the scope as an argument.
Everything *above* it — the published prose and the publication target — is
written by hand for Switzerland and has to be rewritten.

| what | where | how it changes |
| --- | --- | --- |
| scope constants | `src/glace_catalog/scopes.py` | already there: `SCOPES["alps"]`, grid, extent, 2 209 perimeter tiles, 45.4 % coverage. Nothing to write |
| collection ids | derived from the scope | `glace-ch-tiles` → `glace-alps-tiles`, automatic |
| publication target | `catalog.publish.yaml` | `write_prefix`, `public_base` and `data_dir` all name `glace-ch` |
| absolute asset hrefs | both `collection.json` | built from `public_base`; every `alternate` `s3://` href too |
| root catalog identity | `catalog/catalog.json` | `id`, `title`, `description` |
| collection prose | both `collection.json` | `title`, `description`, `keywords`, `extent` |
| the documentation | 6 × `README.md` / `AGENTS.md` | written for Switzerland throughout: the perimeter, the coverage caveat, the tile counts, the worked examples' URLs |
| the perimeter caveat | `catalog/tiles/README.md`, `AGENTS.md` | "coverage is the glacier perimeter, not the country … no data over the Mittelland" is a Swiss sentence and becomes an Alpine one |
| the years | both `collection.json` extents | 2021–2024 → 2015–2025 |
| this document | `docs/MIGRATION.md`, `docs/conformance.md` | the measured numbers are Swiss |

Find them all with:

```bash
grep -rn 'Switzerland\|Swiss\|glace-ch' --include='*.json' --include='*.md' \
     --include='*.yaml' --include='*.py' . | grep -v '^./.git'
```

Nineteen files as of 2026-09-03. The two collection documents and the six
markdown files are the substantive ones; the rest are constants and comments.

### Whether the two ship as one catalog or two

Open, and worth deciding before publication rather than after. Two readings:

- **Two catalogs.** `glace-ch` stays as a published rehearsal and `glace-alps`
  is a separate root. Honest about their different status, and the Swiss data is
  a strict subset of the Alpine data, so it is also pure duplication.
- **One catalog, replaced.** `glace-alps` supersedes `glace-ch` at a new prefix,
  and `glace-ch` is retired once the Alpine years are published. The rehearsal
  was for exercising the pipeline, not for having an audience.

The second is the default unless someone is already citing the Swiss URLs. It
also avoids publishing two catalogs whose overlapping items carry different ids
for the same pixels (`32TLR48-2024` in both, under different collections), which
is the kind of thing that looks fine until someone joins them.

Whichever is chosen, `DATASET_VERSION` in `stac/items.py` and the `version`
field on both collections are the STAC version extension's, not the scope's:
they say which edition of the pixels a value belongs to, and Switzerland and the
Alps are not different editions of one thing.