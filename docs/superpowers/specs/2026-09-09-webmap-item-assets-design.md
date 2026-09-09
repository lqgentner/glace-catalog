# Web-map archives as item assets, one style per year

**Status: draft for review, 2026-09-09.** Nothing below is implemented.

## What changes, in one paragraph

Each year's fourteen PMTiles archives move from `mosaics/pmtiles/{year}/` into
the year's item directory, renamed with a `_viz` suffix, and are registered on
the year item as assets with the `visual` role. The collection keeps its
`rel: "pmtiles"` links, which the spec requires, and gains one published style
per year, generated from the single tracked style by the year rewrite
`finalize-catalog` already does for the latest year. The viewer needs no change
to draw; it needs one to read a year's style by year. In the Portolan browser
the collection page keeps three cards, and each year's item page shows twelve
COG cards and fourteen archive cards for that year.

## Why

- The archives are registered today only as collection links, which the
  browser lists under "Additional resources" as 56 entries, growing to about
  150 for eleven Alpine years. Registering them as collection assets would
  give them cards but 150 cards on one page, which defeats the purpose.
- An item asset puts each rendering beside the year it renders, with the
  year's footprint and window already on the item. This is also the shape a
  future `visual` WebMercator COG would take (asset-level `proj:*`, as
  `ftw/global-data` does), so the two containers become interchangeable at
  the catalog level.
- The style describes only the latest year, so 42 of the 56 `pmtiles:layers`
  ids name a layer no published style holds. One style per year closes that.

Spec check (portolan-spec b79516b, rashid as installed): assets may sit at
item level; PORTO-FMT-011 keeps the archive link at collection level, which
stays; PORTO-CORE-069 and FMT-014 keep styles as collection assets under
`styles/`, which stays; rashid's derivative checks (`PTL-VIZ-002/003/004`)
accept the links as the derivative; `PTL-COL-004` counts raster *data* assets
only. The `visual` role is a standard STAC role the spec names for the
rendering derivative. Nothing here needs a validator change.

## Decisions

### D-1 Naming: `_viz` suffix on key and filename

Asset key `coh12_vv_viz`, file `coh12_vv_viz.pmtiles`; likewise
`coh12_rgb_viz`, `rtc_vv_qa_cqm_viz`. Reasons:

- M-23 says no CRS, year or scope in a filename and key equals stem. `_3857`
  breaks the first half; `_viz` keeps both.
- The suffix says what the file is when it is opened outside the catalog, as
  a GIS with PMTiles support would. `-webmap` says where it is used, which is
  narrower than what it is.
- With distinct stems the two products can share a directory (D-2): a listing
  shows `coh12_vv.tif` and `coh12_vv_viz.pmtiles` next to each other, which is
  the right pairing to see.

The style layer ids stay `glace-{stem}-{year}` with the *data* stem
(`glace-coh12_vv-2024`), since the id names the quantity drawn, and the viewer
and the `pmtiles:layers` join already parse it. The style source key stays
`src-{stem}`.

**Same key for LAEA mosaics and tiles: yes, unchanged.** Both use `coh12_vv`
because both hold the same quantity in the same units on different grids. A
reader comparing a tile to the mosaic, the stem fallback in the viewer, and
the `renders` block all rely on that identity. Only renderings get a suffix.

### D-2 Location: in the item directory, flat

```
mosaics/
├── collection.json  README.md  AGENTS.md  items.parquet  thumbnail.png
├── styles/2021.json … 2024.json          # one per published year (D-4)
└── {year}/
    ├── item.json
    ├── coh12_vv.tif … (12 analysis COGs)
    └── coh12_vv_viz.pmtiles … (14 archives)
```

`mosaics/pmtiles/{year}/` goes away. M-22's two reasons both fall with this
design: the archives are no longer collection-level products, and the stems
no longer collide. A `mosaics/{year}/pmtiles/` subdirectory was considered;
it is allowed, but it separates the pairing D-1 makes visible and adds a path
segment every href, the thumbnail renderer and the layout class would carry
for no gain. Styles cannot move into the year directory: the spec puts them in
the collection's `styles/` as collection assets.

### D-3 Registration: item assets from the build state, links from finalize

The year item gains fourteen assets shaped like this:

```json
"coh12_vv_viz": {
  "href": "https://data.source.coop/lqgentner/glace-ch/mosaics/2024/coh12_vv_viz.pmtiles",
  "type": "application/vnd.pmtiles",
  "title": "coh12_vv web-map tiles",
  "description": "Pre-styled 8-bit WebP rendering of coh12_vv on WebMercatorQuad z5-z13, stretched 0.10-0.80 through cmc.lipari as styles/2024.json declares. Not for analysis: read coh12_vv.tif.",
  "roles": ["visual"],
  "proj:code": "EPSG:3857",
  "file:size": 23295975,
  "file:checksum": "1220…",
  "alternate": { "s3": { "href": "s3://…/mosaics/2024/coh12_vv_viz.pmtiles" } }
}
```

- Written by `publish-year`'s `items` step, the same way `mosaic_assets`
  reads the `mosaics` step's state. The `pmtiles` step already records size,
  ramp, range and units per archive; it adds the multihash checksum, measured
  on the staged file before upload as the COGs are. `stac.MosaicAsset` already
  carries every field above, so no new record type.
- `proj:code` at asset level is what projection v2 provides for an asset on a
  different grid than the item. `proj:shape` and `proj:transform` are not
  added: a tiled pyramid has no single one, and the header carries bounds.
- Roles: `visual` only. Not `data` (AGENTS.md rule), not `tiles` (undefined in
  the spec), not `overview` (that is a lower-resolution copy of the data, and
  this is a different quantity).
- `finalize-catalog.pmtiles_links` keeps writing the collection links, from
  the new location. `pmtiles:layers` stays on the link, where web-map-links
  defines it.

### D-4 Styles: one tracked source, one published style per year

The stretch and ramp are per product and fixed across years, which is the
whole reason a year slider compares like with like (M-27). One tracked file
per year would copy fourteen legend blocks four to eleven times and let them
drift; one published file holding every year's sources would be 154 sources
and layers for the Alps with one visible. Neither is right.

Instead: the tracked source stays `catalog/mosaics/styles/default.json`, and
`repoint_styles` writes one published copy per year instead of one for the
latest, `styles/{year}.json`, each registered as a collection asset
`style-{year}` with the `style` role; the latest year also carries `default`.
This is exactly what the function does today, looped, and it upholds M-21:
colours and legends are committed and reviewed, the year is the only
generated part. Every `pmtiles:layers` id then resolves to a layer in a
published style.

The build keeps reading the tracked file for its LUTs. The viewer reads
`style-{year}` for each year and falls back to the default for a year the
collection lacks, which is its current fallback with a narrower trigger.

### D-5 Thumbnails, mirror, viewer

- `thumbnails.render_from_pmtiles` reads `mosaic_year(latest) / "coh12_rgb_viz.pmtiles"`.
- The item mirror gains fourteen asset entries per mosaic item. Nothing in
  `tests/test_mirror.py` depends on the asset count.
- `glace-viewer` draws from the collection links and is unaffected. Its
  `store.js` gets the per-year style lookup (D-4). Its AGENTS.md store tree
  and the `ln -s …/pmtiles tiles` recipe change to the new path.

## Migration

Every archive changes path and name, so the `pmtiles` step rebuilds all four
years (about 5.5 min each; the fingerprint changes with the tiler's output
naming) and the `items` step rewrites the four items. Then `finalize-catalog`.
Publishing never deletes: remove `mosaics/pmtiles/` by hand on the UZH store
and, for 2024, on Source Cooperative, where this afternoon's sync put it.

## Files touched

| file | change |
| --- | --- |
| `src/glace_catalog/stac/store.py` | drop `StoreLayout.pmtiles`; archives resolve through `mosaic_year`; layout docstring |
| `src/glace_catalog/webmap.py` | archive filename `{layer}_viz.pmtiles`; `LayerInfo` gains `checksum` |
| `scripts/publish-year.py` | `step_pmtiles` stages into the year directory and records the checksum; `step_items` adds `visual_assets(state)` beside `mosaic_assets` |
| `scripts/finalize-catalog.py` | `pmtiles_links` reads the new location; `repoint_styles` writes one style per year; `stamp_style_assets` and the collection's asset map register `style-{year}` |
| `src/glace_catalog/thumbnails.py` | archive path |
| `catalog/mosaics/collection.json` | `style-default` becomes the generated `style-{year}` set; `item_assets` gains the fourteen `_viz` entries; prose |
| `catalog/mosaics/README.md`, `AGENTS.md`, root `AGENTS.md` | the "renderings are never data" paragraph, the tree |
| `docs/DESIGN.md` | M-22 superseded, M-23 amended (renderings carry `_viz`), new M-3x for item-level `visual` assets and per-year styles |
| `docs/STATUS.md` | the migration step above |
| tests | `test_webmap`, `test_items`, `test_thumbnails`, `test_links`, `test_styles`, `test_conformance` follow the paths and the new assets |
| `../glace-viewer` | `store.js` per-year style; AGENTS.md tree and recipe |

## Verification

1. `uv run tests/run_all.py` green.
2. `publish-year.py 2024 --scope ch`, then `finalize-catalog.py`; `rashid check
   --data` on the store: 0 errors, the two accepted findings unchanged.
3. The 2024 item lists 26 assets; every `file:checksum` matches its bytes.
4. Every `pmtiles:layers` id on the collection names a layer in
   `styles/{year}.json` for its year (new gate in `tests/test_links.py`).
5. The viewer's `store-catalog.test.js` fixture updated from the rebuilt
   collection; all fourteen archives of every year reach the map.
6. Portolan browser on the store: three cards on the collection, 26 on the
   2024 item, the archive cards titled by layer.

## Out of scope

The WebP COG alternative (spike report of 2026-09-08). This design is the
shape both containers would share; swapping the container is a later, separate
decision.
