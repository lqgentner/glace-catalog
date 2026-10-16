# Where the catalog stands

**As of 2026-10-16.** Update this file in the same commit as the change it
describes: the next person will trust it. The design and its reasons are in
[`DESIGN.md`](DESIGN.md).

## State

| | state |
| --- | --- |
| repository | Scaffolded from `portolan-sdi/portolan-catalog-template` (Apache-2.0). Its three conformance gates remain, attributed in `THIRD_PARTY_NOTICES.md` with the license copy in `licenses/`; its publishers are unused, and `tools/publish.py` uploads (R-8) |
| catalog documents | `glace-alps`, version 1.0, 2015–2026, rendered from `metadata/` (R-18) and committed, gated by `tests/test_render.py`. **0 rashid errors**; two accepted `PTL-COL-005` findings ([`conformance.md`](conformance.md)). The collections carry the LaRS version DOI as `sci:doi`; it is registered once the dataset is listed |
| gates | ruff, pyrefly `strict`, JSON/YAML canonical form, the render gate, the release version, grids, styles, the manifest contract, items, the item mirror, thumbnails, the tiler, the web-map build, finalize, links, publishing, stac-check, rashid |
| source composites | 2015–2026 at `/mnt/data/alps-total/{year}/s1-comp-mean`, written by s1bursts 0.6.0 with mean aggregation. Every manifest passes `load_and_verify_manifest` (2026-10-16): 2 210 tiles of 12 files a year, except 2015, whose gaps the `coverage_note` in `metadata/catalog.yaml` describes |
| glacier fractions | `tiles/glacier-fractions.parquet`, derived once from the inventories (AGENTS.md step 1), on the bucket at `s3://s1-bursts/data/glace-alps` and copied into the local store |
| store | Built and finalized locally at `/mnt/data/glace-alps` (`GLACE_STORE_ROOT`), 2026-10-16: 26 519 tile items, twelve mosaic items, 144 mosaic COGs and 144 web-map archives. The composites are hard links into the source archives, so the store adds only the mosaics, archives and documents. `make conformance` gives 0 errors and twelve `PTL-CAT-001` warnings, one per year catalog ([`conformance.md`](conformance.md)) |
| web maps | Twelve value-encoded archives a year, over the transport ranges remeasured on the mean composites (`metadata/assets.yaml`); finalization refuses archives whose recorded encoding differs. A year takes 54 min on 15 cores (AGENTS.md) |
| item mirror | Hilbert-ordered, float64 covering column, 2 048-row groups, gated by `tests/test_mirror.py` against rashid's own checks |
| thumbnails | Drawn from the latest year's mosaic and the Aletsch tile's `coh12_vv.tif` over muted OSM, 1400 × 933 WebP at quality 90 (R-9); gated by `tests/test_thumbnails.py`. Finalization needs network access to the tile service |
| publication | `tools/publish.py`, store to Source Cooperative, gated offline by `tests/test_publish.py`. `giuz/glace-alps` does not exist yet. The Swiss test catalog is still at `lqgentner/glace-ch` |

## Open

Ordered by what blocks what. Everything not listed here is done.

- **P-21 — Publish the tile grid as PMTiles; stop publishing the fractions
  table.** Plan:
  [`2026-10-16-tile-grid-pmtiles.md`](superpowers/plans/2026-10-16-tile-grid-pmtiles.md).
  The viewer then draws the grid without reading `items.parquet`, whose footer
  is most of what it downloads. Do it before P-20, so the fractions table is
  never uploaded. *Verified when* the built tree's tiles collection registers
  `grid` with a `rel: "pmtiles"` link and rashid reports 0 errors.
- **P-20 — Publish `glace-alps`.** Upload the store to `giuz/glace-alps`
  (AGENTS.md, "Publish the store"). *Verified when* `make conformance-live`
  over the mirrored store reports 0 errors.
- **P-2 — Remove the Swiss test publication.** `lqgentner/glace-ch` on Source
  Cooperative holds the old Swiss tree. Publishing never deletes, and the
  proxy deletes one object at a time (it answers the bulk `DeleteObjects` call
  with `NoSuchBucket`). *Verified when* listing that prefix returns nothing.
- **P-5 — Overview depth at Alpine size.** `PTL-DAT-011` wants overviews down
  to one tile. `rashid check --data` passes on the 2026 Alpine mosaics
  (2026-10-16); the writer does not assert the level count yet. *Verified
  when* the mosaic writer asserts the level count.
- **P-10 — Register in the Portolan registry** once published
  (`portolan:register-catalog`). *Verified when* the catalog appears in the
  registry.

Worth doing, blocking nothing:

- **P-12 — `grid:code`** (`MGRS-32TLS`) on tile items: one line, for interop
  with Planetary Computer and Earth Search.
- **P-15 — Track [portolan-spec#41](https://github.com/portolan-sdi/portolan-spec/issues/41)
  (raster styling).** This catalog is the first Portolan raster PMTiles case;
  the style files and the `renders` blocks may need changing when it settles.
- **P-22 — Check every published href after publishing.** Send a HEAD request to
  each asset href under `public_base`, as FIRMS does with
  `tests/test_links.py --remote`. `rashid check --live` tests range requests
  and CORS, not whether every object exists.
- **P-23 — Align the READMEs and agent guides with the paper's Usage Notes**
  (`../glace-paper/main.tex`, "Usage Notes"). The guides already cover linear
  backscatter and its decibel conversion, NaN nodata, the lossy PMTiles and
  the 2015 gaps. They lack three of its notes:
  - CQM: separability drops where it is below 0.5. Mask or weight those
    pixels, or use the CQM as a model input.
  - Interpreting coherence:
    - lakes reach about 0.09 with the 35 × 7 window, so values are not
      comparable with products that use larger windows, and thresholds should
      come from GLACE itself;
    - persistent snow, unstable slopes and lakes look glacier-like.
  - Validated scope: glacier mapping in Switzerland, within 250 m of the
    outlines.

  Use the paper's numbers and wording so the two stay consistent, and put the
  notes in a section on quirks that give silently wrong answers, as FIRMS
  does in `detections/AGENTS.md`.
- **P-24 — `table` extension on the `items` assets.** `table:columns` and
  `table:row_count`, generated from the code that writes the mirror, with the
  AGENTS.md column table rendered from the same source. Seen in the
  Sentinel-2 mirror.
- **P-25 — A gate that every URL in the repository names `public_base`.**
  A stale prefix keeps resolving while its old objects exist. The Sentinel-2
  mirror added `test_location.py` after it moved.
- **P-27 — Year styles as item assets.** FIRMS keeps each year's styles beside
  its archives (`pmtiles://./…`), so the collection holds only the default
  style. Check first that the Portolan browser reads a style from an item.
- **P-29 — Archive header bounds.** `rashid check --data` warns
  (`PTL-DAT-005`) on every web-map archive: its header bounds are the
  tile-aligned display grid, about 1 km wider than the mosaic item's bbox.
  *Verified when* the archives' header bounds match the item bbox and the
  warning is gone.
- **P-28 — Root and collection links.** A `rel: "preview"` link (`text/html`)
  to the viewer, `via` to the Source Cooperative page, `providers` on the
  root, and `updated` on the root and the collections at finalization.
- **P-19 — Repoint to the production repository once it has the code.** The
  composite pipeline is moving from `deep-glacier-mapping` to
  [`glace-production`](https://github.com/lqgentner/glace-production), which
  is still empty. References to `deep-glacier-mapping` stay until the code has
  moved, because they are runnable commands, such as the
  `cd ../deep-glacier-mapping` subshell in AGENTS.md step 1. *Verified when*
  `git grep -l deep-glacier-mapping` comes back empty and that subshell runs
  from `../glace-production`.
