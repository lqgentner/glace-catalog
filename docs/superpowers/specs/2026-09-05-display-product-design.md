# The display product, and where its constants live

**Status: decided 2026-09-05.** Written to settle what serves the web map once
the annual analysis mosaic moves to LAEA 40 m. Folds into
[`MIGRATION.md`](../../MIGRATION.md) §4 and its decision log.

## The decision

**Variant A — WebMercator PMTiles for every display layer, single-band and false
colour alike; ETRS89-LAEA 40 m for analysis. No WebMercator COGs.**

Both display and analysis resample **straight from the UTM per-tile
composites**; neither is derived from the other, and LAEA is never in the display
path.

The wire measurement below did not decide it — it *removed* wire cost as a
consideration by showing the 4x was the dtype and not the container, at which
point the structural and dependency axes decided it. Recorded because the
reasoning that made the question look open is worth keeping visible.

**What this gives up, knowingly:**

- In-browser pixel readout. `cog-rgb.js` is cheap only because the float mosaic
  sits on WebMercatorQuad; once it is LAEA, an XYZ tile is no longer an integer
  window read. See Finding 3.
- A palette change means re-tiling. That cost now lands entirely on a
  display-only product, which is why it stopped mattering.

---

## What is already fixed

Three published products, each resampled **straight from the UTM per-tile
composites**. No product is derived from another.

| product | grid | role | status |
| --- | --- | --- | --- |
| per-tile composites | native UTM zone | deep learning, local analysis | unchanged |
| annual mosaics | **ETRS89-LAEA 40 m** | large-scale analysis | **decided, new** |
| web-map product | WebMercator z13 (19.109 m) | display | **variant A or B** |

**LAEA is never in the display path.** WebMercator's 19 m pixel at z13 is
resampled from UTM directly, not warped out of the LAEA mosaic. This holds for
both variants and is the reason the double-resampling argument in
`webmap.py`'s module docstring does not apply to either.

### Why LAEA for analysis

WebMercator is not equal-area. `scopes.py` already records that a nominal
19.109 m pixel is **13.2 m on the ground at 46.5° N**, and the factor varies with
latitude across the Alpine perimeter. Any glacier area or area-change statistic
computed on a WebMercator mosaic needs a correction that the catalog does not
declare anywhere. For a dataset whose headline use is glacier change, that is a
correctness problem, not a preference. EPSG:3035 is the standard European
analysis grid and removes it.

This reverses the §10 row *"branch B's LAEA 40 m grid — not taken — WebMercator
z13 serves both roles; the false-colour COG closes the display gap."* Half that
justification was already dead: M-20 dropped the false-colour COG. The surviving
half — one grid serving both roles — is what the equal-area argument overturns.

---

## What changes under **both** variants

Consequences of the LAEA decision alone. None of these depend on A versus B.

1. **`scopes.py` carries two grids per scope.** `Scope.grid` is currently a
   single WebMercator `Grid` used for three things at once: the VRT chain's warp
   target, the mosaic COG's grid, and the `proj:shape` / `proj:transform` copied
   into items. Those split into an analysis grid (LAEA 40 m) and a display grid
   (WebMercator z13). Both stay hard-committed constants for the reason the
   module docstring already gives: an extent recomputed per run is not canonical.
2. **`tests/test_grids.py` inverts.** It is currently untracked, and it gates the
   WebMercator choice directly — CRS *is* WebMercator, resolution *is*
   WebMercatorQuad-at-z13, `ALIGNED_LEVELS=2` down to z12. Under either variant
   that becomes the gate on the *display* grid, and the LAEA grid needs its own
   (simpler: no tile-alignment requirement, just a committed extent on a 40 m
   multiple). **Cheap to redirect now, expensive once the Alps extent is derived
   against it.**
3. **`tiling_chain()` dies** (`publish-year.py:250`). It exists to tile from the
   published mosaics because they are the canonical-grid step already
   materialised. Once the published mosaic is LAEA, it is the wrong grid to tile
   from, and the display product is built from the UTM chain in both variants.
   Under A that restores the memory-heavy path the function was written to avoid
   — see A's open questions. Under B there is no tiler at all.
4. **The LAEA extents must be derived and committed** for both scopes, the way
   `cache/stac-store-refactor/derive-extents.py` produced the current ones.
5. **`proj:code` on the mosaic items becomes `EPSG:3035`**, and the mosaic
   `renders` block's `resampling` semantics want re-checking against a 40 m grid.

---

## Variant A — WebMercator PMTiles

Keep the current display product. Fourteen archives per year: twelve
single-band, colour-mapped through the module's LUTs, plus `coh12_rgb` and
`rtc_rgb` false colour.

### Build path

UTM tile chain → per-band LUT → `create_rgba_vrt` (RGBA, nothing materialised)
→ `rio pmtiles` subprocess → one `.pmtiles` per layer → `upload_tree` to
`mosaics/pmtiles/{year}/`.

### Catalog shape

- Collection-level `rel: "pmtiles"` links on `mosaics` (`PORTO-FMT-011`,
  `PTL-VIZ-003`), each carrying `pmtiles:layers` naming the MapLibre style layer.
- `web-map-links` v1.3.0, already declared on the collection.
- MapLibre GL styles as `style` assets, exactly one carrying `default`
  (`PTL-VIZ-002`, `PTL-VIZ-005`).
- **14 × 11 = 154 links on one collection document.** §3 names this as a genuine
  mismatch: Portolan's visualization model assumes one derivative per
  collection, and these describe products that each belong to one item. It is
  conformant, and the collection document is doing bookkeeping the spec was not
  shaped for.
- The archives live at `mosaics/pmtiles/{year}/`, not in the item directories
  (M-22) — a *consequence* of choosing PMTiles, not a principle, since a
  collection-level registration cannot point inside an item's directory.

### How the legend is specified, and how it reaches `glace-viewer`

Two channels, and only one of them is durable.

**Durable home — `metadata.portolan:legend` in the style file.** Each of the four
committed styles under `catalog/mosaics/styles/` carries a legend block:

```json
"portolan:legend": {
  "title": "COH12 VV", "type": "ramp",
  "stops": [ {"value": 0.1, "color": "#000004", "label": "0.10"}, … ]
}
```

Three facts about this, all uncomfortable:

- **It is in no specification.** §4 records it grepped: `portolan:legend` appears
  nowhere in `portolan-spec`. It is FTW's convention, copied. MapLibre's
  `metadata` is free-form and ignored by renderers, so the file stays valid and
  nothing but code written for it reads the block. Raster styling as a whole is
  open under [portolan-spec#41](https://github.com/portolan-sdi/portolan-spec/issues/41).
- **`type: "ramp"` cannot express false colour.** A ramp is one ordered `stops`
  array. `backscatter-rgb.json` currently draws one anyway, over `-18.5 … -5` in
  greys — that is the *red channel alone*, in colours the archive does not use.
  Its prose `description` stops at `"B: VV-VH dB"` with no numbers.
- **The numbers exist in exactly one place and no consumer can reach them.**
  `RGB_STYLES` in `webmap.py:173`: coherence `(0.10, 0.80) (0.10, 0.60)
  (0.8, 2.5)`, backscatter `(-18.5, -5) (-26, -11) (4, 14)` dB. `LayerInfo`
  carries one scalar `vmin`/`vmax` pair, so the writer records `ranges[0]` and
  drops green and blue.

**Interim channel — `layers.json`.** `glace-viewer` reads a manifest beside the
catalogue, and P-18 specifies an optional `channels` key on a false-colour entry
— `[{band, vmin, vmax}]` in red, green, blue order — which publishes the three
stretches today while the style-file legend is unsettled. Deliberately *not*
inferred: red and green happen to equal the sibling VV and VH entries, nothing
enforces that, and blue equals nothing published anywhere. The viewer's own
measured copy disagreed with the build (`0.75–2.75` / `3.5–11` against
`0.8–2.5` / `4–14`) and was deleted rather than corrected, which is why the
numbers must now come from here.

> **`layers.json` is currently orphaned.** `build_viewer_manifest`,
> `write_manifest`, `_as_json` and `publish_tiles` are all present in
> `webmap.py:751-840` with **no caller anywhere in this repository**, and
> `finalize-catalog.py:14` still claims in its docstring to write one.
> `UPSTREAM.md`'s simplifications table asserts *"`layers.json` and its writer
> deleted"* — false; M-9 moved raster *styling* to `renders`, not the viewer
> manifest. **Whether `glace-viewer` is reading a stale file from a pre-port
> build, or is broken today, is unresolved** and needs checking in that repo.
> Under variant A this must be fixed, because A depends on the channel.

### Dependencies

- **`rio-pmtiles` is in neither `pyproject.toml` nor `uv.lock`, and `rio` is not
  on PATH.** `webmap.py:434-441` resolves the CLI at runtime and raises
  *"The 'rio' CLI was not found; install rasterio and rio-pmtiles"*. **The
  PMTiles step cannot run from a clean `uv sync` today.** Fixable by declaring
  it — but it is currently a committed, documented, unrunnable branch.
- A `subprocess` boundary, whose S3 configuration must be rebuilt by hand
  through `subprocess_s3_env` — that is F-2, only just moved into `freezebase`
  and **still awaiting a release** (F-4 blocks the Alps on it).
- `matplotlib` + `cmcrameri` for the LUTs, `freezebase.vrt.create_rgba_vrt` for
  the RGBA intermediate.
- `--tiler-workers`, one process per core, each decoding raster blocks. Already
  documented as the knob to lower when the machine runs out of memory, and
  false colour (two chains at once) is named as the first thing to fall over.

### Measured

**4× cheaper on the wire than COG**, measured in `glace-viewer` against real
tiles — a wire measurement, not a file-size one, because a browser GeoTIFF
reader batches range reads into fixed blocks and fetches more than the viewport
needs. Verified at **0 rashid errors** for the whole registration shape.

**Caveat that matters for the comparison:** that 4× was measured against the
*float32 LERC analysis* mosaics serving double duty. It is **not** a measurement
against variant B's purpose-built lossy display COGs, which do not exist yet.

---

## Variant B — WebMercator display COGs

Drop PMTiles. Publish display COGs beside the LAEA analysis mosaic in the same
item directory, disambiguated by filename.

### Naming

`coh12_vv_3857_viz.tif`, `coh12_rgb_3857_viz.tif`, and so on.

**This does not overturn M-23.** §3 already narrowed it and, in its closing
sentence, sanctioned exactly this case in advance:

> Qualifying a **primary** asset is redundant: the path says which collection
> and grid, and `proj:code` says it authoritatively. Qualifying a **derivative
> that sits beside its primary** is not redundant at all — it is the only thing
> separating two files that describe the same measurement in different units on
> different grids. […] **If a COG display product ever returns to an item
> directory here, it takes FTW's naming with it.**

FTW's own precedent is `prue_v1_confidence_global_uint8_3857.tif` — dtype *and*
EPSG code — against a `prue_v1_confidence_global.tif` primary. Whether `_viz`
or `_uint8` / `_webp` is the better qualifier is open; FTW encodes dtype, the
proposal here encodes role.

### Encoding

- **Single band:** LERC, with a **looser error bound** than the analysis COG.
  Measured in §4: `2.5e-3` on coherence saves **19 %**, `1.5e-4` on backscatter
  saves **27 %**. Both were previously rejected — but rejected *as a change to
  the analysis product*, where the bound is anchored to the data's own
  measurement noise. On a display derivative that anchor does not apply, so the
  rejection does not carry over.
- **False colour:** WebP, prescaled to dB, 2 %/98 % stretch. Note this
  **resurrects the product M-20 dropped**, and we have its numbers: the RGBA
  WEBP COGs measured **0.32 B/px (coh12)** and **0.44 B/px (rtc)**. §4 called
  them "a good answer to the wrong question" — the question has changed.
- A 2 %/98 % percentile stretch is *computed*, where `RGB_STYLES`' current
  ranges are *designed constants*. That is a real difference: percentiles move
  between years, so two years would not be visually comparable unless the
  stretch is computed once and pinned.

### Catalog shape

- Both products are ordinary **item assets** on the mosaic item. One asset shape,
  no collection-level link explosion, no `web-map-links` extension, no `style`
  assets, no `PTL-VIZ-002/003/005` surface at all.
- The 154-link mismatch §3 names disappears entirely — each derivative is
  registered on the item it belongs to, which is what the data actually is.
- `item_assets` on the mosaics collection grows from 12 to ~26.
- **Role discipline:** the WebP false colour is dB and lossy; it takes `visual`
  / `overview` and **never `data`**, per the standing invariant that a `visual`
  asset is never `data`. The looser-LERC single bands are the awkward case —
  they are quantitatively real but deliberately degraded, so they are `visual`
  too, and M-8's retired "only decibel pixels are in PMTiles" line needs
  rewriting either way.

### How the legend is specified

**`renders` v2.0.0** — already declared on both collections (M-9), a ratified
extension rather than a convention, carrying `rescale`, `colormap_name`,
`resampling`, `nodata` and `expression`.

**This resolves P-18 outright.** `rescale` is already an array *of pairs* —
today `"rescale": [[0.1, 0.8]]` for one band — so the three-channel stretch that
`portolan:legend`'s single `ramp` cannot express is written natively:

```json
"rtc_rgb": {
  "assets": ["rtc_rgb_3857_viz"],
  "rescale": [[-18.5, -5.0], [-26.0, -11.0], [4.0, 14.0]],
  "resampling": "average", "nodata": "nan"
}
```

The numbers stop being trapped in `RGB_STYLES`, and no `channels` key, no
`layers.json`, and no dependence on portolan-spec#41 is needed. P-18 closes as
a consequence of the variant rather than as separate work.

### Dependencies

`rasterio` + `rio-cogeo`. Both declared, both locked, both already in use.
Entirely in-process: `-of COG -co TILING_SCHEME=GoogleMapsCompatible -co
ZOOM_LEVEL=13`, which `test_grids.py:22` already documents as reproducing
`snap_to_tiles` exactly, down to the last digit of the `ch` left bound. No
subprocess, no CLI, no `subprocess_s3_env`, no tiler worker pool, no new
dependency of any kind.

---

## Side by side

| | A — PMTiles | B — display COGs |
| --- | --- | --- |
| new dependencies | `rio-pmtiles` (**undeclared, unrunnable from a clean `uv sync`**) | none here; viewer keeps its pinned esm.sh reader |
| process model | subprocess + hand-rebuilt S3 env + worker pool | in-process |
| blocked on a freezebase release | yes (F-2 / F-4) | no |
| catalog registration | 154 collection-level links + styles + 3 VIZ rules | ordinary item assets |
| §3's "genuine mismatch" | present | dissolved |
| legend home | `portolan:legend` — **in no specification** | `renders` v2.0.0 — ratified |
| false-colour stretches publishable | only via `layers.json` `channels` (viewer half **already done**) | **yes, natively, in `renders`** |
| depends on `glace-viewer`'s `layers.json` | yes — orphaned, last built 2026-08-27 | no |
| depends on portolan-spec#41 landing | yes | no |
| wire cost | baseline | **1.37× at z11** (WebP q80, like-for-like); 3.35× if LERC |
| palette change | re-tile | re-encode (same cost, honestly) |
| reverses a prior decision | no | reinstates M-20, relaxes M-7 §4's LERC row |
| filename convention | n/a | pre-sanctioned by §3 |

---

## What `glace-viewer` actually is today

Explored 2026-09-05 at `b99d140`. This resolves open question 2 and changes the
comparison.

### `layers.json` is stale, not broken

`tiles/` in the viewer is **gitignored** — "usually a symlink to a local build".
It currently holds a real directory whose `layers.json` was generated
**2026-08-27**: four layers, 2023 only, no QA rasters, no false colour, no
`channels`. It is titled "European Alps" but predates the port.

So the viewer is not reading anything this repository currently produces, and it
is not broken either — it degrades. `validateManifest` drops malformed entries
with a warning rather than failing the page, and the axes are derived from the
layers that survive.

`js/rasters.js:5` states the file is *"written by the store's finalize-catalog
step"*. That matches `finalize-catalog.py:14`'s docstring here — **and neither
is true**: the writer exists in `webmap.py` with no caller. Both repositories
document a step that does not run. That is the whole of the break.

### P-18's viewer half is already implemented

`b99d140` — *"Read the false colour's channel stretches from the manifest"*.
`falseColourChannels()` (`js/rasters.js:456`) reads `layer.channels`, validates
the three-entry `{band, vmin, vmax}` shape, and where it is absent names the
bands with no ranges — *"printing numbers the archive was not necessarily built
with is a guess dressed as a legend"*. It deliberately holds no stretch of its
own.

**The viewer is waiting on this repository.** Under variant A, publishing
`channels` is the remaining work and it is small. Under variant B the key is
unnecessary — `renders`' `rescale` carries the same numbers in a ratified place.

### The COG reader still exists and is still wired

This is the finding that matters most for variant B. `d4b7fd0` retired the
tile-source **switch**, not the machinery. `js/cog-rgb.js` is intact, its tests
are intact, and `js/map.js:63` still registers the protocol:

```js
maplibregl.addProtocol("glace-rgb", cogRgbProtocol);
```

`index.html:155` records it as deliberate: *"Nothing on the page reads a COG
today — the tile-source switch was retired in favour of the pre-styled archives
— but js/cog-rgb.js and its [tests are kept]"*.

**Variant B revives a working, tested reader rather than building one.**

### The LAEA decision does not break `cog-rgb.js`

Worth stating explicitly, because it easily could have. The module's entire
cheapness argument rests on the canonical WebMercatorQuad grid:

> Every mosaic of a scope is written on the WebMercatorQuad grid at one zoom, so
> VV and VH share a size, an origin, a blocking and an overview count: tile
> (x, y) of one covers exactly the ground of tile (x, y) of the other. […] So a
> tile here is an integer window read out of each file and a per-pixel combine:
> never a reprojection, never a resample, never an interpolation.

Measured on `glace-ch`: the image origin sits 1 088 000 by 734 720 pixels from
the WebMercator origin at z13, and halving stays integral through all seven
overview levels.

Under variant B the **display** COG stays on WebMercator z13, so every word of
that still holds. It would only break if the display product moved to LAEA —
which is exactly what your "no LAEA→3857" constraint rules out.

### A third option this opens up

`cog-rgb.js` composes false colour **client-side from two single-band float
COGs** — its `channels` recipe reads VV and VH and combines per pixel. So under
B there are two sub-variants:

- **B1** — publish a baked WebP RGB false-colour COG, as proposed. Three files
  per product, stretch fixed at build time.
- **B2** — publish only the single-band display COGs and let `cog-rgb.js`
  compose. **No false-colour file at all**, and the stretch stays client-side
  and free to change — the same property `renders` gives the single bands. The
  reader for it already exists and is already tested.

B2 removes the 2 %/98 % pinning question entirely, and removes the resurrection
of M-20's dropped product. It costs a client-side combine per tile and a
dependency, below.

### The dependency is relocated, not removed

Honest accounting for B. `js/config.js:41` pins the reader:

```js
cogReaderUrl: "https://esm.sh/@developmentseed/geotiff@0.7.0?external=lerc"
```

ESM-only with bare specifiers, no UMD build, so it cannot be a `<script>` tag —
it is dynamically imported from a CDN the first time a layer needs it, and a
deployment that never selects one never fetches it. It decodes LERC and Zstd
through its own direct dependencies.

So variant B does not delete a dependency. It moves one from **this
repository's build** (`rio-pmtiles`, undeclared, subprocess, blocks a clean
`uv sync`) to **the viewer's runtime** (esm.sh, pinned, lazily fetched, already
in use and already tested). Which of those is the better place to carry it is a
judgement, not a measurement — but a lazily-fetched pinned CDN import in a
static page is a much smaller problem than a build step that cannot run from a
checkout.

---

## The wire-cost measurement

Run 2026-09-05 against the published store. **Method:** wire bytes computed from
each file's own internal tile index — for a fixed XYZ viewport, which internal
tiles are touched and what their byte lengths are. Both readers fetch exact
ranges (`@developmentseed/geotiff` for COG tile data, the PMTiles client for
archive tiles), so this is what actually goes over the wire; it is deterministic,
needs no browser, and has no network variance.

Viewport: 6x4 tiles over Aletsch (`#10/46.51/8.03`), COH12 VV 2024 — the same
one `glace-viewer/AGENTS.md` used. Harness kept at
`/tmp/…/scratchpad/wire.py` (throwaway; ~150 lines, minimal TIFF-IFD and
PMTiles-v3 parsers, no new dependencies).

### Method validation

Reproduced against the published figures before being trusted:

| z11 | AGENTS.md | harness | Δ |
| --- | --- | --- | --- |
| PMTiles | 21 req, 354 kB | 20 req, 346.4 kB | −2.2 % |
| COG (`@developmentseed/geotiff`) | 36 req, 1417 kB | 35 req, 1385.1 kB | −2.3 % |
| **ratio** | **4.00×** | **4.00×** | — |

One request fewer in each row because the published counts include the initial
header/directory read. **The ratio reproduces exactly.**

### Finding 0 — the published mosaic is at `1e-3`, not `1e-4`

Re-encoding the published COG at `MAX_Z_ERROR=1e-3` reproduces its size to
0.002 % (109 017 789 B against 109 020 017 B). So the published 2024 COH12 VV
mosaic carries the **old** bound, consistent with `lerc-compression/PLAN.md`
recording the `1e-3 → 1e-4` tightening as *unreleased*. §4's "2.5e-3 saves 19 %"
was therefore measured against the published `1e-3` file, not against the
intended analysis product. Measured here: 18.5 %.

| bound | file | vs published |
| --- | ---: | ---: |
| lossless | 331.1 MB | +204 % |
| `1e-4` (intended analysis) | 149.4 MB | +37 % |
| **`1e-3` (published today)** | **109.0 MB** | — |
| `2.5e-3` | 88.9 MB | −18.5 % |
| `5e-3` | 74.5 MB | −31.7 % |
| `1e-2` | 59.7 MB | −45.2 % |
| `2e-2` | 45.0 MB | −58.7 % |

### Finding 1 — no LERC bound closes the gap

Wire bytes per viewport, as a multiple of the PMTiles baseline:

| LERC bound | z13 | **z11** | z9 |
| --- | ---: | ---: | ---: |
| lossless | 19.09× | 11.87× | 8.23× |
| `1e-4` | 8.25× | 5.45× | 3.92× |
| `1e-3` (published) | 6.04× | **4.00×** | 2.95× |
| `2.5e-3` | 4.82× | **3.35×** | 2.47× |
| `5e-3` | 4.00× | 2.86× | 2.14× |
| `1e-2` | 3.10× | 2.37× | 1.81× |
| `2e-2` | 3.32×→2.32× | 1.83× | 1.43× |

z11 is the row to believe — the level at which the extent fills the viewport.

**A looser LERC single-band display COG does not work.** At `2.5e-3` it still
costs 3.35× PMTiles, and even at `2e-2` — 200× looser than the intended analysis
bound, well past anything defensible — it is still 1.83×. The saving is real
(−16 % of wire bytes at `2.5e-3`) but it is nowhere near a 4× gap.

### Finding 2 — the 4× is the dtype, not the container

The decisive row. An 8-bit WebP display COG, colour-mapped through the published
`magma` ramp at the published `0.10–0.80` stretch, at **`QUALITY=80` — the exact
quality `webmap.py:452` writes PMTiles tiles with**. Same codec, same quality,
same pixels; the only difference is the container.

| zoom | PMTiles | WebP COG | ratio |
| --- | --- | --- | ---: |
| z13 | 24 reads, 234.9 kB | 24 reads, 224.7 kB | **0.96×** |
| z12 | 22 reads, 321.0 kB | 24 reads, 331.8 kB | **1.03×** |
| **z11** | 20 reads, 346.4 kB | 35 reads, 475.9 kB | **1.37×** |
| z10 | 16 reads, 240.4 kB | 30 reads, 338.2 kB | **1.41×** |
| z9 | 14 reads, 122.0 kB | 20 reads, 152.0 kB | **1.25×** |

Whole file: 25.4 MB against the archive's 23.3 MB, +9 %.

**So the container costs 0.96–1.41×, and the float payload costs 4×.** §4's
measurement conflated two variables — container *and* dtype — and attributed the
whole result to the container. Isolated, almost all of it is the dtype.

The residual 1.37× at z11 is structural, not codec: below z12 the image origin
lands on a half or quarter tile, so one XYZ tile straddles a 2x2 block and the
viewport needs 35 reads against PMTiles' 20. At z13 and z12, where the grid is
1:1, the COG is level with or cheaper than the archive.

### What this does to the variants

**Variant B's proposed encoding is half right.** The WebP false colour is sound —
wire-comparable to PMTiles. The **LERC single-band half is the expensive half**,
and it should be WebP too, which is what the existing LUT machinery already
produces for the PMTiles path. Rewritten, B is: *every* display product 8-bit
WebP, single-band and false colour alike, as item assets.

That makes the wire cost a **non-differentiator**, at 1.37× worst case, and
throws the decision back onto the structural and dependency axes — where B was
already ahead.

### Finding 3 — LAEA costs the viewer its pixel-value readout

Not about the display variants, and it needs recording against the LAEA decision
itself.

`cog-rgb.js` is *"the only path by which this page can read a pixel value at
all"*, and what makes it cheap is that the float mosaic sits on the
WebMercatorQuad grid: a tile is an integer window read, never a reprojection.
**Move the float mosaic to LAEA and that property is gone** — XYZ tiles no longer
align with image blocks, and the browser would need a reprojection per tile.

An 8-bit WebP display product cannot substitute: its pixels are display codes,
not measurements.

So if in-browser pixel readout is to survive the LAEA move, a **float WebMercator
COG has to remain published** — which is precisely the "keep additional
WebMercator COGs" option. That reframes the original question: the two are not
alternatives serving one purpose, they serve two.

| product | purpose | encoding |
| --- | --- | --- |
| float WebMercator z13 COG | in-browser pixel readout, tiler source | float32 LERC |
| WebP display product | the picture | 8-bit, PMTiles or COG |
| LAEA 40 m mosaic | large-scale analysis | float32 LERC |

Whether readout is worth a third product is a question for you — it is a viewer
feature, not a catalog requirement, and nothing in Portolan asks for it.

---

## Open questions

**Blocking a decision:**

1. ~~What does a purpose-built lossy display COG actually cost on the wire?~~
   **Answered 2026-09-05: 1.37× at z11, like-for-like.** The 4× was the dtype,
   not the container. Looser LERC does *not* work (3.35× at `2.5e-3`); 8-bit
   WebP does. See the measurement section. **Wire cost no longer separates the
   variants.**
2. ~~Is `glace-viewer`'s `layers.json` live, stale, or broken?~~ **Answered
   2026-09-05: stale.** A 2026-08-27 local build, four layers, gitignored. The
   viewer degrades rather than failing. Neither repository writes one.

**Not blocking:**

3. **B1 versus B2** — bake a WebP false-colour COG, or let `cog-rgb.js` compose
   it client-side from the two single-band COGs it already reads. B2 needs no
   third file and keeps the stretch client-side.
4. Whether the false-colour stretch stays a designed constant or becomes a
   pinned 2 %/98 % percentile. Only arises under B1: a computed percentile moves
   between years, so it must be pinned once or years stop being comparable.
5. `_viz` versus FTW's dtype-encoding `_uint8` / `_webp` as the qualifier.
6. Whether the twelve single-band display products are all needed, or whether
   the QA layers can be display-only on demand. 14 layers is what drives the
   154-link count under A and the `item_assets` growth under B. Note the viewer
   already puts QA rasters on the map (`d4b7fd0`), so it expects all fourteen.

---

## The metadata design

Settled 2026-09-05, after the variant. Answers three questions: is `layers.json`
required, where do the display constants live, and what is standard about any of
it.

### `layers.json` is not required

Every field of `LayerInfo` has a standard home:

| field | home | standard |
| --- | --- | --- |
| `url` | the `rel:"pmtiles"` link href | web-map-links v1.3.0 |
| `bounds`, `min_zoom`, `max_zoom` | **the PMTiles header** | PMTiles v3 |
| `size_bytes` | HTTP `Content-Length` | — |
| `id` | style layer id / `pmtiles:layers` | web-map-links |
| `product`, `polarization`, `year` | link path + style layer id | naming |
| `start_date`, `end_date` | mosaic item `start_datetime` / `end_datetime` | STAC core |
| `cmap`, `vmin`, `vmax`, `units`, `colors` | the style's legend metadata | below |
| `source` | dropped — build provenance, belongs in `manifest.parquet` | — |

Verified on the published `2024/pmtiles/coh12_vv.pmtiles`: the v3 header carries
`minzoom=5`, `maxzoom=13`, bounds `6.7676, 45.8288, 10.4590, 47.3388`, and tile
type 4 (WEBP). So three fields are already in the archive a client is opening
anyway.

> **One caveat.** That archive's header `center` is garbage — `57.5164,
> -100.4528`, `centerzoom 27` — a `rio-pmtiles` bug. Harmless, because MapLibre
> frames from `bounds`, but do not let anything come to depend on it.

Discovery becomes: fetch `mosaics/collection.json` to enumerate archives, fetch
the one style file for display constants. Two requests, both standard.

### The constants live in the style file, not in `renders`

`renders` is the ratified extension and the tempting home. It is the wrong one,
for a measured reason: **`renders.colormap_name` deliberately names a different
ramp than the one baked into the archives.**

| layer | baked (`DEFAULT_STYLES`) | `renders` declares |
| --- | --- | --- |
| `coh12_vv` / `coh12_vh` | `cmc.lipari` | `magma` |
| `rtc_vv` / `rtc_vh` | `cmc.navia` | `gray` |
| `*_qa_num` | `cmc.turku` | *absent* |
| `*_qa_cqm` | `cmc.glasgow` | *absent* |

M-10 made that substitution deliberately — a generic client will not have
cmcrameri registered — and it is right for the LAEA analysis COG, where a client
renders from float pixels. It is wrong as a legend source for PMTiles: it would
draw a `magma` legend under an image coloured in `cmc.lipari`. `renders` also
covers only 4 of the 14 published layer types.

Semantically too: `renders` says *how to render this asset*. A PMTiles archive
needs no rendering instruction, because the render already happened. **The
stretch is provenance of the archive, not an instruction.**

So `renders` stays where it belongs — describing the LAEA analysis COG — and the
display constants go in the MapLibre style's `metadata`, which the style spec
leaves explicitly free-form and which Portolan already mandates as a file
(`PTL-VIZ-002`, `PTL-VIZ-005`). No new artifact.

### One `default.json`, fourteen layers

The stretch is **per product, not per year** — `finalize-catalog` only ever
repoints the year in a style's `pmtiles://` URL. So the constants are 14 values,
not 154.

One `default.json` declaring 14 sources and 14 layers, one visible and thirteen
`visibility: none`, each carrying its legend under layer-level `metadata`. It
stays directly loadable by any MapLibre client, satisfies "exactly one
`default`", and costs a reader one fetch rather than fourteen. The year pinned in
its sources does not constrain a reader looking at another year, precisely
because the stretch is year-independent.

### `portolan:legend` needs a false-colour shape

`type: "ramp"` is one ordered `stops` array and cannot express three channels,
which is why `backscatter-rgb.json` currently draws the red channel's range in
greys. Add a `type: "channels"` variant carrying three `{band, vmin, vmax}`
entries, keeping FTW's `value` / `color` / `label` / `unit` vocabulary for the
ramp case so the two stay one convention rather than two.

This is the concrete case to take to
[portolan-spec#41](https://github.com/portolan-sdi/portolan-spec/issues/41): a
real raster legend the vector-shaped rules do not cover.

### One source of truth closes P-18 — **implemented 2026-09-05**

The numbers currently exist **twice** — `DEFAULT_STYLES` / `RGB_STYLES` in
`webmap.py` (used to bake) and the committed style JSON (published) — with
nothing keeping them in step. That duplication *is* P-18, and `glace-viewer`'s
deleted third copy, blue `0.75-2.75` against the build's `0.8-2.5`, was the same
bug one repository over.

**The committed style is the source of truth, and `webmap.py` reads it.**
`DEFAULT_STYLES` and `RGB_STYLES` are deleted and replaced by a reader over
`catalog/mosaics/styles/default.json`. The false colour's three ranges reach a
reader for the first time, so P-18 closes as a consequence rather than as
separate work.

**This upholds M-21** rather than reversing it. M-21 committed the style files
because they hold colour decisions that belong in review; making the reviewed
file the one the build actually consumes is that argument carried through. The
alternative — generating the legend from Python — would have left the reviewed
file downstream of an unreviewed constant.

**What the legend block therefore has to carry.** Rendered `stops` alone are not
enough to rebuild a LUT: the build needs the ramp by name and the range as
numbers. So each layer's `metadata.portolan:legend` carries `cmap`, `vmin`,
`vmax`, `units` and `decibel` declaratively, and the `stops` are *derived* from
them — kept in the file for clients that cannot resolve a `cmc.*` name, and
regenerated whenever the declarative fields change.

**The cost, stated plainly.** The build gains a dependency on a catalog file:
`publish-year.py --steps pmtiles` cannot run without a valid
`catalog/mosaics/styles/default.json`. That is a real coupling and the reason to
prefer the other direction. It is accepted because the alternative is two copies
of the same numbers, and two copies is precisely what P-18 is.
