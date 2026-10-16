# Where the catalog stands

**As of 2026-10-16.** Update this file in the same commit as the change it
describes: the next person will trust it. The design and its reasons are in
[`DESIGN.md`](DESIGN.md).

## State

| | state |
| --- | --- |
| repository | Scaffolded from `portolan-sdi/portolan-catalog-template` (Apache-2.0). Its three conformance gates remain, attributed in `THIRD_PARTY_NOTICES.md` with the license copy in `licenses/`; `tools/sync_data.py` replaces its publishers (R-8) |
| catalog documents | `glace-alps`, version 1.0, 2015–2026, rendered from `metadata/` (R-18) and committed, gated by `tests/test_render.py`. **0 rashid errors**; two accepted `PTL-COL-005` findings ([`conformance.md`](conformance.md)). The collections carry the LaRS version DOI as `sci:doi`; it is registered once the dataset is listed |
| gates | ruff, pyrefly `strict`, JSON/YAML canonical form, the render gate, the release version, grids, styles, the manifest contract, items, the item mirror, thumbnails, the tiler, the web-map build, finalize, links, sync, stac-check, rashid |
| source composites | 2015–2026 at `/mnt/data/alps-total/{year}/s1-comp-mean`, written by s1bursts 0.6.0 with mean aggregation. Every manifest passes `load_and_verify_manifest` (2026-10-16): 2 210 tiles of 12 files a year, except 2015, whose gaps the `coverage_note` in `metadata/catalog.yaml` describes |
| glacier fractions | `tiles/glacier-fractions.parquet`, derived once from the inventories (AGENTS.md step 1), on the bucket at `s3://s1-bursts/data/glace-alps` and copied into the local store |
| store | Built locally at `/mnt/data/glace-alps` (`GLACE_STORE_ROOT`). The composites are hard links into the source archives, so the store adds only the mosaics, archives and documents |
| item mirror | Hilbert-ordered, float64 covering column, 2 048-row groups, gated by `tests/test_mirror.py` against rashid's own checks |
| thumbnails | Drawn from the latest year's mosaic and the Aletsch tile's `coh12_vv.tif` over muted OSM, 1400 × 933 WebP at quality 90 (R-9); gated by `tests/test_thumbnails.py`. Finalization needs network access to the tile service |
| publication | `tools/sync_data.py`, store to Source Cooperative, gated offline by `tests/test_sync_data.py`. `giuz/glace-alps` does not exist yet. The Swiss test catalog is still at `lqgentner/glace-ch` |

## Open

Ordered by what blocks what. Everything not listed here is done.

- **P-20 — Publish `glace-alps`.** Sync the store to `giuz/glace-alps`
  (AGENTS.md, "Publish the store"). *Verified when* `rashid check --live`
  over the mirrored store reports 0 errors.
- **P-2 — Remove the Swiss test publication.** `lqgentner/glace-ch` on Source
  Cooperative holds the old Swiss tree. Publishing never deletes, and the
  proxy deletes one object at a time (it answers the bulk `DeleteObjects` call
  with `NoSuchBucket`). *Verified when* listing that prefix returns nothing.
- **P-5 — Overview depth at Alpine size.** `PTL-DAT-011` wants overviews down
  to one tile. *Verified when* the mosaic writer asserts the level count and
  `rashid check --data` passes on an Alpine mosaic.
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
  `cd ../deep-glacier-mapping` subshell in AGENTS.md step 1. *Verified when*
  `git grep -l deep-glacier-mapping` comes back empty and that subshell runs
  from `../glace-production`.
