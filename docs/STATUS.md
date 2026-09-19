# Where the catalog stands

**As of 2026-09-19.** Update this in the same commit as the change it describes;
a stale status record is worse than none, because the next person trusts it. The
design and the reasons behind it are in [`DESIGN.md`](DESIGN.md).

## State

| | state |
| --- | --- |
| repository | scaffolded from `portolan-sdi/portolan-catalog-template` (Apache-2.0); the vendored publishers are gone (R-8), the three conformance gates stay, attributed in `THIRD_PARTY_NOTICES.md` with the license copy in `licenses/` |
| catalog documents | rendered from `metadata/` (R-18) and committed under `tests/test_render.py`; **0 rashid errors**, two accepted `PTL-COL-005` findings ([`conformance.md`](conformance.md)). Reviewed in the Portolan browser 2026-09-09: asset titles unified across collections, items, links and style; `renders` re-synced with the remeasured stretches; QA assets badged `cloud-optimized`; projection extension dropped |
| gates | real: ruff, pyrefly `strict`, JSON/YAML canonical form, the render gate, the release version, grids, styles, the manifest contract, items, the item mirror, thumbnails, the tiler, the web-map build, finalize, links, sync, stac-check, rashid |
| source composites | Swiss 2021–2024 **regenerated with s1bursts 0.5.0** and verified 2026-09-08 through `load_and_verify_manifest`: 1 716 files across 143 tiles per year, every row carrying a multihash sha256 and the five band statistics, `manifest_version: 2`, one compositing window per year |
| glacier fractions | `tiles/glacier-fractions.parquet` is on the bucket, derived once per region from the inventories (AGENTS.md step 1); `finalize-catalog` stamps its `file:size` and `file:checksum` |
| build pipeline | `publish-year.py` (copy, vrt, mosaics, pmtiles, items) and `finalize-catalog.py` (patch, mirror, thumbnails, styles per year, asset stamps, coverage check, validate); run end to end over 2021–2024 on 2026-09-09 with the archives in the item directories, one style per year and the remeasured stretches |
| item mirror | Hilbert-ordered, float64 covering column, 2 048-row groups, gated by `tests/test_mirror.py` against rashid's own checks |
| thumbnails | rendered from the latest year's `coh12_rgb_viz.pmtiles`, gated by `tests/test_thumbnails.py` |
| publication | `tools/sync_data.py`, store → Source Cooperative, gated offline by `tests/test_sync_data.py`; the four Swiss years are published in the item-asset layout (2026-09-09) and republished 2026-09-14 with the R-18 metadata (asset-key band names, vocabulary titles, remeasured `renders`); `rashid check --live` over the mirrored store reports 0 errors. Change detection is size, then single-part ETags, then which side is newer. Republished 2026-09-19: the restyled READMEs, the `via` link to `glace-production`, 17 objects |
| published store | the old flat tree on the UZH bucket is being deleted; the old flat publication at `lqgentner/glace-ch` on Source Cooperative (over a thousand objects) still has to be cleared |
| scope | **`glace-ch`, a rehearsal.** The dataset is `glace-alps` (DESIGN.md §8) |

## Open

Ordered by what blocks what. Everything not listed here is done.

- **P-2 — Clear the old publication.** `lqgentner/glace-ch` on Source
  Cooperative still holds the old flat tree, and publishing never deletes.
  *Verified when* listing that prefix returns nothing.
- **P-5 — Overview depth at Alpine size.** `PTL-DAT-011` wants overviews down to
  one tile; the Alps mosaic needs more levels than a Swiss test COG produced.
  *Verified when* the mosaic writer asserts the level count and
  `rashid check --data` passes on an Alpine mosaic.
- **P-6 — The VRT chain and the tiler at scale.** A 2 209-source chain on S3 is
  the most likely thing to force a design change. The web-map step now warps
  each layer once into a local raster and tiles from that (R-12), which the
  Swiss measurement puts at 5.5 min a year against 40 min before; the Alpine
  projection is 1.4-1.6 h a year, and fourteen concurrent intermediates are
  15 GB of scratch (`--concurrent-layers` bounds it). On 2026-09-11 the step
  was found to run a 60 GB machine to 57 GB because every tiler worker had
  GDAL's default cache of 5 % of RAM; bounded to 256 MB per worker it peaks
  at 11 GB with the defaults (AGENTS.md, "2. Once per year"). *Verified when* an
  Alpine year completes `--steps vrt,mosaics,pmtiles` without a rewrite, with
  its peak scratch and RSS recorded.
- **P-7 — The run manifest at scale.** Implemented on both sides and gated by
  `tests/test_manifest_contract.py`, but only ever read on Swiss years.
  *Verified when* `publish-year.py --steps copy` on an Alpine year reports zero
  mismatches.
- **P-8 — Re-measure the display ranges for the Alps.** Every stretch in
  `metadata/assets.yaml` was remeasured on the warped Swiss 2021–2024 display
  mosaics on 2026-09-09 (2–98 %, averaged over the years); `glace-alps` covers
  2015–2025, which holds more of the S1A+S1B era. *Verified when* the ranges in
  `metadata/assets.yaml` are re-derived from published Alpine mosaics and
  `catalog/` re-rendered.
- **P-18 — Thumbnails over a basemap.** The browser review asked for a single
  tile in front of a contextily basemap for the tiles collection, and the whole
  mosaic over one for the mosaics, both framed 3:2. `thumbnails.py` cuts from
  the archive alone today. *Verified when* both thumbnails show a basemap and
  `tests/test_thumbnails.py` is green.
- **P-9 — First release: the DOI fields.** Enable the Zenodo bridge for this
  repository on zenodo.org, cut the release (DESIGN.md §7), then add `sci:doi`
  (the concept DOI) and `sci:citation` to `catalog/catalog.json`,
  `catalog/tiles/collection.json` and `catalog/mosaics/collection.json`, with the
  scientific extension in each `stac_extensions`. *Verified when* the three
  documents carry both fields and `tests/run_all.py` is green.
- **P-10 — Register in the Portolan registry** once published
  (`portolan:register-catalog`). *Verified when* the catalog appears in the
  registry.

Worth doing, blocking nothing:

- **P-11 — `llms.txt` per collection**, linked `rel: "llms"`. FTW ships one per
  collection and per item at 1–7 kB.
- **P-12 — `grid:code`** (`MGRS-32TLS`) on tile items: one line, and it buys
  interop with Planetary Computer and Earth Search.
- **P-13 — Per-item thumbnails on mosaic items.** ~12 renders a year makes the
  catalog legible in any browser.
- **P-14 — Test `@developmentseed/deck.gl-geotiff`** against a LERC_ZSTD
  NaN-nodata mosaic before hardening the viewer's zero-sentinel workaround. The
  Portolan browser uses it, and it depends on `lerc` directly — if it preserves
  the validity mask, the workaround disappears.
- **P-15 — Track [portolan-spec#41](https://github.com/portolan-sdi/portolan-spec/issues/41)
  (raster styling).** This catalog is the first Portolan raster PMTiles case; the
  style files and the `renders` blocks may both need changing when it settles.
- **P-19 — Repoint the production repository, once there is one.** The composite
  pipeline moves out of `deep-glacier-mapping` into
  [`glace-production`](https://github.com/lqgentner/glace-production), which is
  still empty. **The old name stays here until the code is there, deliberately:**
  the references are runnable, not prose — `AGENTS.md` §1 is a
  `cd ../deep-glacier-mapping` subshell — so repointing them early sends a reader
  into a checkout that does not exist, which is worse than a name that is merely
  old. Both READMEs already link the new repository, and nobody executes a link
  row. *Verified when* `git grep -l deep-glacier-mapping` comes back empty and
  the subshell in `AGENTS.md` §1 runs from `../glace-production`.

## Swapping Switzerland for the Alps

DESIGN.md §8 says what changes and why. Find every placeholder with:

```bash
grep -rn 'Switzerland\|Swiss\|glace-ch' --include='*.json' --include='*.md' \
     --include='*.yaml' --include='*.py' . | grep -v '^./.git'
```

The substantive files are `metadata/catalog.yaml` (`scope`, `region`, `years`,
`title`, the preliminary notice) and `catalog.publish.yaml`; then
`uv run scripts/render-catalog.py` rewrites `catalog/`. The rest are constants,
comments and the measured Swiss numbers in `docs/` and `metadata/assets.yaml`.
