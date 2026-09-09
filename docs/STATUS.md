# Where the catalog stands

**As of 2026-09-09.** Update this in the same commit as the change it describes;
a stale status record is worse than none, because the next person trusts it. The
design and the reasons behind it are in [`DESIGN.md`](DESIGN.md).

## State

| | state |
| --- | --- |
| repository | scaffolded from `portolan-sdi/portolan-catalog-template`; the vendored publishers are gone (R-8) |
| catalog documents | root + both collections written, **0 rashid errors**, two accepted `PTL-COL-005` findings ([`conformance.md`](conformance.md)) |
| gates | real: ruff, pyrefly `strict`, JSON/YAML canonical form, the release version, grids, styles, the manifest contract, items, the item mirror, thumbnails, the tiler, the web-map build, finalize, links, sync, stac-check, rashid |
| source composites | Swiss 2021–2024 **regenerated with s1bursts 0.5.0** and verified 2026-09-08 through `load_and_verify_manifest`: 1 716 files across 143 tiles per year, every row carrying a multihash sha256 and the five band statistics, `manifest_version: 2`, one compositing window per year |
| build pipeline | `publish-year.py` (copy, vrt, mosaics, pmtiles, items) and `finalize-catalog.py` (patch, mirror, thumbnails, styles per year, asset stamps, coverage check, validate); run end to end over 2021–2024 on 2026-09-08, **before** the web-map archives moved into the item directories (P-16) |
| item mirror | Hilbert-ordered, float64 covering column, 2 048-row groups, gated by `tests/test_mirror.py` against rashid's own checks |
| thumbnails | rendered from the latest year's `coh12_rgb_viz.pmtiles`, gated by `tests/test_thumbnails.py` |
| publication | `tools/sync_data.py`, store → Source Cooperative, gated offline by `tests/test_sync_data.py`; the four Swiss years are published, live rashid 0 errors |
| published store | the old flat tree on the UZH bucket is being deleted; the old flat publication at `lqgentner/glace-ch` on Source Cooperative (over a thousand objects) still has to be cleared |
| scope | **`glace-ch`, a rehearsal.** The dataset is `glace-alps` (DESIGN.md §8) |

## Open

Ordered by what blocks what. Everything not listed here is done.

- **P-1 — Derive the glacier fractions.** No `glacier-fractions.parquet` exists
  on the bucket yet; the tiles collection registers it as an asset.
  *Verified when* `{GLACE_STORE_ROOT}/tiles/glacier-fractions.parquet` exists and
  `finalize-catalog` stamps its `file:size` and `file:checksum` without a
  warning.
- **P-16 — Rebuild the web-map archives in their new place.** M-34 and M-35
  moved every archive to `mosaics/{year}/{stem}_viz.pmtiles` and made the styles
  per year, so the published tree still holds the old `mosaics/pmtiles/` layout
  and a single style. Run, per year 2021–2024:

  ```bash
  uv run --env-file .env scripts/publish-year.py {year} --scope ch \
      --src … --steps pmtiles,items
  uv run --env-file .env scripts/finalize-catalog.py --scope ch \
      --base-url https://data.source.coop/lqgentner/glace-ch
  ```

  The `pmtiles` fingerprint changes with the style bytes, so all four years
  rebuild (about 5.5 min each). Then delete `mosaics/pmtiles/` by hand on the UZH
  store and on Source Cooperative — publishing never deletes — and sync.
  *Verified when* `rashid check --data` reports 0 errors, the 2024 item lists 26
  assets, and no `mosaics/pmtiles/` prefix remains on either side.
- **P-2 — Clear the old publication.** `lqgentner/glace-ch` on Source
  Cooperative still holds the old flat tree, and publishing never deletes.
  *Verified when* listing that prefix returns nothing.
- **P-3 — Build the four Swiss years end to end**, then finalize with the public
  base URL. *Done 2026-09-08*: 2021–2024 built and finalized; the
  `mirror_docs.py` + `rashid check --schema --data` pass in
  [`conformance.md`](conformance.md) reports 0 errors.
- **P-4 — Publish, then check the host.** *Done 2026-09-08*: synced with
  `--force`, and `rashid check --live --live-base-url
  https://data.source.coop/lqgentner/glace-ch` over the mirrored documents
  reports 0 errors, so `PTL-LIV-001..005` hold on Source Cooperative.
- **P-5 — Overview depth at Alpine size.** `PTL-DAT-011` wants overviews down to
  one tile; the Alps mosaic needs more levels than a Swiss test COG produced.
  *Verified when* the mosaic writer asserts the level count and
  `rashid check --data` passes on an Alpine mosaic.
- **P-6 — The VRT chain and the tiler at scale.** A 2 209-source chain on S3 is
  the most likely thing to force a design change. The web-map step now warps
  each layer once into a local raster and tiles from that (R-12), which the
  Swiss measurement puts at 5.5 min a year against 40 min before; the Alpine
  projection is 1.4-1.6 h a year, and fourteen concurrent intermediates are
  15 GB of scratch (`--concurrent-layers` bounds it). *Verified when* an
  Alpine year completes `--steps vrt,mosaics,pmtiles` without a rewrite, with
  its peak scratch and RSS recorded.
- **P-7 — The run manifest at scale.** Implemented on both sides and gated by
  `tests/test_manifest_contract.py`, but only ever read on Swiss years.
  *Verified when* `publish-year.py --steps copy` on an Alpine year reports zero
  mismatches.
- **P-8 — Re-measure the display ranges for the Alps.** Every stretch in
  `default.json` was remeasured on the warped Swiss 2021–2024 display mosaics on
  2026-09-09 (2–98 %, averaged over the years); `glace-alps` covers
  2015–2025, which holds more of the S1A+S1B era. *Verified when* the ranges in
  `catalog/mosaics/styles/default.json` are re-derived from published Alpine
  mosaics.
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

## Swapping Switzerland for the Alps

DESIGN.md §8 says what changes and why. Find every placeholder with:

```bash
grep -rn 'Switzerland\|Swiss\|glace-ch' --include='*.json' --include='*.md' \
     --include='*.yaml' --include='*.py' . | grep -v '^./.git'
```

The substantive files are the two `collection.json`, `catalog/catalog.json`,
`catalog.publish.yaml` and the six `README.md` / `AGENTS.md` under `catalog/`.
The rest are constants, comments and the measured Swiss numbers in `docs/`.
