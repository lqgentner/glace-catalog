# Where the catalog stands

**As of 2026-09-22.** Update this file in the same commit as the change it
describes: the next person will trust it. The design and its reasons are in
[`DESIGN.md`](DESIGN.md).

## State

| | state |
| --- | --- |
| repository | Scaffolded from `portolan-sdi/portolan-catalog-template` (Apache-2.0). Its three conformance gates remain, attributed in `THIRD_PARTY_NOTICES.md` with the license copy in `licenses/`; `tools/sync_data.py` replaces its publishers (R-8) |
| catalog documents | Rendered from `metadata/` (R-18) and committed, gated by `tests/test_render.py`. **0 rashid errors**; two accepted `PTL-COL-005` findings ([`conformance.md`](conformance.md)). Last reviewed in the Portolan browser on 2026-09-09 |
| gates | ruff, pyrefly `strict`, JSON/YAML canonical form, the render gate, the release version, grids, styles, the manifest contract, items, the item mirror, thumbnails, the tiler, the web-map build, finalize, links, sync, stac-check, rashid |
| source composites | Swiss 2021–2024, regenerated with s1bursts 0.5.0 and verified on 2026-09-08 with `load_and_verify_manifest`: 143 tiles and 1 716 files a year, every row with a multihash sha256 and the five band statistics, `manifest_version: 2`, one compositing window per year |
| glacier fractions | `tiles/glacier-fractions.parquet` is on the bucket, derived once per region from the inventories (AGENTS.md step 1); `finalize-catalog` records its `file:size` and `file:checksum` |
| build pipeline | `publish-year.py` (copy, vrt, mosaics, pmtiles, items) and `finalize-catalog.py` (patch, mirror, thumbnails, per-year styles, asset sizes and checksums, coverage check, validation). Run end to end over Swiss 2021–2024 on 2026-09-09, and over Alpine 2023 on 2026-09-24 (timings in AGENTS.md) |
| item mirror | Hilbert-ordered, float64 covering column, 2 048-row groups, gated by `tests/test_mirror.py` against rashid's own checks |
| thumbnails | Drawn from the latest year's mosaic and the Aletsch tile's `coh12_vv.tif` over muted OSM, 1400 × 933 WebP at quality 90 (R-9); gated by `tests/test_thumbnails.py`. Finalization needs network access to the tile service |
| publication | `tools/sync_data.py`, store to Source Cooperative, gated offline by `tests/test_sync_data.py`. The four Swiss years are published at `lqgentner/glace-ch` in the item-asset layout with the R-18 metadata; `rashid check --live` over the mirrored store reported 0 errors on 2026-09-14. The last republication, on 2026-09-20, replaced the thumbnails with WebP and deleted the old `thumbnail.png` pair. **A live conformance pass after that republication is outstanding** |
| published store | The old flat tree on the UZH bucket is being deleted. The old flat publication at `lqgentner/glace-ch` on Source Cooperative (over a thousand objects) still needs clearing (P-2). `sync_data.py` only adds and replaces, so renamed or dropped objects are removed by hand: one object at a time on the Source Cooperative proxy, which answers the bulk `DeleteObjects` call with `NoSuchBucket` |
| scope | **`glace-alps`** since 2026-09-22: 2015–2025, title "GLACE Alps", no preliminary notice. The store root is `s3://s1-bursts/data/glace-alps`, and the local `.env` builds into `/mnt/data/glace-alps`; the publish target is `giuz/glace-alps`. The perimeter is the 2025 Alpine Convention edition, 2 210 MGRS tiles. The `glace-ch` store and its publication stay until retired (DESIGN.md §8) |
| Alpine composites | 2016 and 2023 are finished at `s3://s1-bursts/data/alps-total/{2016,2023}/s1-comp`, each exactly the 2 210 perimeter tiles and passing `load_and_verify_manifest` (2026-09-22). Tiles outside the 2025 perimeter were removed from both by hand (2016: `33TWM56`, `33TXP00`; 2023: `33TWM56`), with backups at `alps-total/{year}/s1-comp-backup-20260922`. No other year's manifest lists such a tile (checked 2026-09-24). Those tiles are the 2018 perimeter minus the 2025 perimeter, both at `s3://s1-bursts/data/regions/`. 2015 is still in production |

## Open

Ordered by what blocks what. Everything not listed here is done.

- **P-2 — Clear the old publication.** `lqgentner/glace-ch` on Source
  Cooperative still holds the old flat tree, and publishing never deletes.
  *Verified when* listing that prefix returns nothing.
- **P-5 — Overview depth at Alpine size.** `PTL-DAT-011` wants overviews down
  to one tile, and the Alpine mosaic needs more levels than a Swiss test COG.
  *Verified when* the mosaic writer asserts the level count and
  `rashid check --data` passes on an Alpine mosaic.
- **P-6 — The VRT chain and the tiler at scale.** A 2 210-source VRT chain on
  S3 is the most likely thing to force a design change. Alpine 2023 built end
  to end on 2026-09-24 from a local source (AGENTS.md, "2. Build each year");
  scratch use was recorded, the process-tree peak RSS was not. *Verified when*
  an Alpine year completes `--steps vrt,mosaics,pmtiles` without a rewrite,
  with its peak scratch and RSS recorded.
- **P-7 — The run manifest at scale.** Implemented on both sides and gated by
  `tests/test_manifest_contract.py`, but only ever read on Swiss years.
  *Verified when* `publish-year.py --steps copy` on an Alpine year reports zero
  mismatches.
- **P-8 — Re-measure the display ranges for the Alps.** The default stretches
  in `metadata/assets.yaml` were measured on the Swiss 2021–2024 display
  mosaics. `glace-alps` covers 2015–2025, including more years in which both
  Sentinel-1A and 1B flew. *Verified when* the ranges in
  `metadata/assets.yaml` are re-derived from published Alpine mosaics and
  `catalog/` re-rendered.
- **P-9 — First release: the DOI fields.** Enable the Zenodo integration for
  this repository on zenodo.org, cut the release (DESIGN.md §7), then add
  `sci:doi` (the concept DOI) and `sci:citation` to `catalog/catalog.json`,
  `catalog/tiles/collection.json` and `catalog/mosaics/collection.json`, with
  the scientific extension in each `stac_extensions`. *Verified when* the three
  documents carry both fields and `tests/run_all.py` is green.
- **P-10 — Register in the Portolan registry** once published
  (`portolan:register-catalog`). *Verified when* the catalog appears in the
  registry.

Worth doing, blocking nothing:

- **P-11 — `llms.txt` per collection**, linked `rel: "llms"`. FTW ships one per
  collection and per item, at 1–7 kB.
- **P-12 — `grid:code`** (`MGRS-32TLS`) on tile items: one line, for interop
  with Planetary Computer and Earth Search.
- **P-13 — Per-item thumbnails on mosaic items.** About twelve renders a year
  would make the catalog legible in any browser.
- **P-14 — Test `@developmentseed/deck.gl-geotiff`** on a LERC_ZSTD mosaic
  with NaN nodata. The Portolan browser uses it, and it depends on `lerc`
  directly. If it keeps LERC's validity mask, the viewer's workaround of
  treating 0 as nodata can go.
- **P-15 — Track [portolan-spec#41](https://github.com/portolan-sdi/portolan-spec/issues/41)
  (raster styling).** This catalog is the first Portolan raster PMTiles case;
  the style files and the `renders` blocks may need changing when it settles.
- **P-19 — Repoint to the production repository once it has the code.** The
  composite pipeline is moving from `deep-glacier-mapping` to
  [`glace-production`](https://github.com/lqgentner/glace-production), which
  is still empty. References to `deep-glacier-mapping` stay until the code has
  moved, because they are runnable commands, such as the
  `cd ../deep-glacier-mapping` subshell in AGENTS.md step 1. Both READMEs
  already link the new repository. *Verified when*
  `git grep -l deep-glacier-mapping` comes back empty and that subshell runs
  from `../glace-production`.

## Switching scope

DESIGN.md §8 lists what a scope switch changes and why. Find the remaining
Swiss references with:

```bash
grep -rn 'Switzerland\|Swiss\|glace-ch' --include='*.json' --include='*.md' \
     --include='*.yaml' --include='*.py' . | grep -v '^./.git'
```

The substantive files are `metadata/catalog.yaml` (`scope`, `region`, `years`,
`title`, the preliminary notice) and `catalog.publish.yaml`; then
`uv run scripts/render-catalog.py` rewrites `catalog/`. The rest are
constants, comments, Swiss measurements in `docs/` and the Swiss-measured
stretches in `metadata/assets.yaml` (P-8).
