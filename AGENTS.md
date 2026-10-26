# glace-catalog — repository guide

Use this guide to change the repository. To use the published data, read
[catalog/AGENTS.md](catalog/AGENTS.md) and the collection guides.

## Edit sources and regenerate

**Edit source files, never generated catalog documents.**

| Source | Output | Generator |
| --- | --- | --- |
| `metadata/catalog.yaml`, `metadata/assets.yaml`, `metadata/templates/` | Tracked STAC documents, default style, READMEs and agent guides in `catalog/` | `scripts/render-catalog.py` |
| Composite manifests, rasters and build settings | Store items, annual mosaics, PMTiles, item indexes, year styles and updated collections | `scripts/build-year.py`, `scripts/finalize-catalog.py` |

The rendered catalog is committed, for review and for release archives; built
store outputs live only in the store. The other hand-maintained files are
`catalog.publish.yaml` and `catalog/_assets/`. Rendering and building share
`glace_catalog.metadata`, so items and collections name assets the same way.

After editing metadata or templates:

```bash
make render   # uv run scripts/render-catalog.py, then tests/test_render.py
make check    # uv run tests/run_all.py
```

`make` lists the targets. Settings come from `.env` and the tracked
configuration.

The render test requires the tracked catalog to match its sources. Lint runs
`ruff format --check`, `ruff check` and strict `pyrefly check`; a missing tool
fails the gate.

`rashid` is the Portolan conformance gate; `stac-check` is advisory. Where they
disagree, rashid takes precedence. Do not widen `ACCEPTED` in
`tests/test_conformance.py` just to pass CI. Any new exception needs a row in
`docs/conformance.md` and a tracking issue; otherwise fix the finding.
The two existing exceptions arise because the tracked tree holds the
collections while their items live in the store. Validate the full built tree
before publication.

## Scope and layout

`glace-alps` is the dataset and the scope that `metadata/catalog.yaml` names.
`glace-ch`, a small Swiss scope, is superseded: no store or source archive
exists for it, so do not build or test against it. Its grid stays in
`src/glace_catalog/scopes.py` as a small fixture for unit tests.
`docs/DESIGN.md` §8 lists what a switch of scope changes.

Preserve these rules:

- Collections contain their items: `tiles/{year}/{tile}/item.json`.
  Year catalogs sit below collections and only group items.
- Assets belong to collections and items only. Run manifests and
  configurations stay beside the source archive and are never published (M-32).
- Structural links, item asset hrefs and the collections' style asset hrefs are
  relative. The other collection asset hrefs and the hrefs in `items.parquet`
  are absolute HTTPS; `s3://` hrefs appear only in the `alternate` extension.
- Finalization writes the `self` link, from `public_base`; tracked documents
  leave it out.
- Analysis COGs store linear values. PMTiles store 8-bit value codes: each
  layer is encoded over a fixed transport range from `metadata/assets.yaml`,
  backscatter and QA-CQM in decibels. The style decodes the codes
  (`raster-dem`, custom encoding) and colours them (`color-relief`) with a
  default ramp. Changing a ramp or stretch means re-rendering documents;
  changing a transport range means re-tiling every year.
- `{stem}_viz.pmtiles` belongs in its year's item directory, as an item asset
  with the `visual` role. Item assets are the archives' only registration, a
  recorded deviation from `PORTO-FMT-011` (`docs/conformance.md`). Each year's
  MapLibre style, `{year}/styles/default.json`, is an asset of its mosaic item
  with the `style` and `default` roles; the collection's one style asset is the
  latest year's copy, a recorded deviation from `PORTO-CORE-069`. The `data`
  role is for the COGs alone.

## Build a store

A store holds a region's COGs, mosaics, web maps and STAC documents, on object
storage or a local disk. Run commands from this checkout unless stated
otherwise.

### Settings

Building reads `.env`, the S3 settings through `freezebase.settings.S3Settings`:

```bash
GLACE_S3_PROFILE=        # named profile in ~/.aws/credentials
GLACE_S3_ENDPOINT_URL=   # UZH gateway, unless set in the profile
GLACE_SOURCE=            # one year's composite archive, with {year}:
                         #   s3://s1-bursts/data/alps-total/{year}/s1-comp
GLACE_STORE_ROOT=        # built store: s3:// prefix, or a local directory
FREEZEBASE_CACHE=        # local scratch: VRT chains, staged mosaics, RGBA rasters
```

`build-year.py` and `finalize-catalog.py` build under `GLACE_STORE_ROOT`. A
local root works offline and publishes the same way. The scope comes from
`metadata/catalog.yaml` and the public address from `public_base` in
`catalog.publish.yaml`.

Set `FREEZEBASE_CACHE` to a volume with room for an Alpine year: about 2.5 GB
of staged mosaics, or 1.1 GB per concurrently warped web-map layer plus 1.8 GB
of staged archives. Unset, the cache falls back to `~/.cache/freezebase`, which
is usually the system disk. `build-year.py` stops before any work when the
cache has less room than its estimate, and removes each step's staged files
after the upload.

Use named profiles only; do not pass access keys through the environment.
Publication uses the separate profile in `catalog.publish.yaml`.

### 1. Compute glacier fractions once per region

Run this in `deep-glacier-mapping`, which provides the inventory data and the
`glacier_mapping` package. It runs on pixi, which loads that checkout's `.env`.
Write the output prefix in full, matching this store's `GLACE_STORE_ROOT`.

```bash
( cd ../deep-glacier-mapping &&
  pixi run python scripts/inventories/derive-glacier-fractions.py \
      --scope alps --perimeter-tiles 2210 \
      --out s3://s1-bursts/data/glace-alps/tiles/.glacier-fractions.parquet )
```

Every year reuses this lookup, so a tile's glacier fraction stays constant.
The build checks that the table names the scope's inventory and buffer
(`src/glace_catalog/scopes.py`). The leading dot keeps the table out of the
upload: the items and the tile grid carry its values.

### 2. Build each year

The source archive must contain `manifest.parquet`, one row per file, written
by the composite run in `deep-glacier-mapping` (`docs/UPSTREAM.md` D-4). Source
manifests come only from that run: to add a missing one, rerun the upstream
year, which skips complete tiles and describes and hashes their files.

```bash
make build YEAR=2023   # uv run --env-file .env scripts/build-year.py 2023
make build YEAR=2023 ARGS="--force-from pmtiles"
GLACE_STORE_ROOT=/scratch/glace make build YEAR=2023   # another store
```

| Step | Result |
| --- | --- |
| `copy` | Validates the manifest and supported assets, leaves out each measurement with no valid pixel and its QA layers, then copies composites server-side without re-encoding; on one local disk it hard-links them. |
| `vrt` | Builds temporary regional VRTs for the next two steps. |
| `mosaics` | Writes twelve annual analysis mosaics. |
| `pmtiles` | Writes twelve value-encoded web-map archives. |
| `items` | Writes STAC items and the year's item-index slice. |

Steps can be rerun. Fingerprints skip unchanged work and trigger rebuilds when
source composites, grids or settings change.

A hard-linked store shares its composites' bytes with the source archive, so
never rewrite an archive file in place: the store copy would change with it,
unseen by the fingerprints. Replace a composite by writing a new file.

| Option | Purpose |
| --- | --- |
| `--steps copy,mosaics` | Run selected steps. |
| `--force-from mosaics` | Rebuild from a step onward. |
| `--layers coh12_vv,rtc_vv` | Select layers. |
| `--max-workers` | Set copy threads; transfers are network-bound. |
| `--mosaic-workers` | Set mosaic processes, one layer each; defaults to four. |
| `--tiler-workers` | Set tiler processes; defaults to one per core, each with a 256 MB GDAL cache. |
| `--concurrent-layers` | Set simultaneous layer warps; defaults to four. |

Size web-map concurrency to the available RAM and scratch disk. Each layer
writes a local RGBA raster of the whole scope: about 100 MB for Switzerland,
1.1 GB for the Alps. The defaults suit a 16 GB laptop. If memory runs out,
lower `--concurrent-layers` first, then `--tiler-workers`.

The Alpine years 2015–2026 on 2026-10-16, with 15 cores and 60 GB RAM,
source and store on one local disk, `--mosaic-workers 12`,
`--concurrent-layers 12` and 15 tiler workers, per year:

| Step | Time | Peak process-tree RSS |
| --- | --- | --- |
| `copy` | 4 s | |
| `vrt` | 2 min | |
| `mosaics` | 10 min | 33 GB |
| `pmtiles` | 41 min | 17 GB |
| `items` | 1 min | |
| Total | 54 min | |

The six decibel layers take longest to warp, about 30 min against 18 min for
the others.

### 3. Finalize after adding or changing a year

```bash
make finalize   # uv run --env-file .env scripts/finalize-catalog.py
```

Finalization:

- refuses to start if `catalog/` differs from a fresh render of `metadata/`;
- refuses archives encoded over a transport range `metadata/assets.yaml` no
  longer has;
- updates the collections' year links and temporal extent, and bounds the
  tiles collection by its tiles;
- combines the item-index slices into `items.parquet`, and refuses an index
  whose columns differ from `mirror_columns` in `metadata/assets.yaml`;
- writes the tile grid, `tiles/grid.pmtiles`, with each tile's glacier fractions;
- draws the thumbnails;
- records asset sizes and checksums, and the item indexes' column types
  and row counts;
- sets `updated` on the root catalog and the collections;
- writes each year's style beside its archives and registers it on the
  mosaic item, and points the collection's style to the latest year;
- validates the store.

Prose, providers, license and `item_assets` come from the tracked metadata.

`public_base` in `catalog.publish.yaml` is written into the root `self` link
and every asset href in `items.parquet`, as it is into the collection asset
hrefs at render time. To add a year, build it and finalize again; the other
years stay as they are.

Before publication, check the built store's documents with `make conformance`
(rashid, without reading asset bytes).

Thumbnails are drawn from the latest year's `coh12_vv.tif` COGs over an
OpenStreetMap basemap, so this step needs network access to the tile service.
`--thumbnail-basemap` takes another contextily provider or a local
georeferenced raster. `--thumbnail-attribution` overrides the credit; an empty
credit is accepted only for a local raster. `--skip-thumbnail` keeps the
thumbnails already in the store, and fails if there are none.

## Publish the store

`tools/publish.py` streams the built store from `settings.store_root`
(`GLACE_STORE_ROOT`) to `write_prefix` in `catalog.publish.yaml`, using that
file's named profile and its configured endpoint. Objects are copied directly,
in chunks, with their content types.

**Upload the built store. Do not upload the tracked `catalog/` over it:** that
would overwrite the collection updates made by finalization. Do not use
`portolan push`; its `versions.json` upload tracking conflicts with this
uploader.

Change detection lists each side once. An object uploads when its size differs.
At equal sizes, single-part MD5 ETags decide where both sides have them;
otherwise the object uploads when the source is newer. `--force` uploads
regardless. Dot-prefixed build files, such as `.build-state.json` and
`.items.parquet`, stay in the store.

### Endpoint

The `source-coop` profile uses `endpoint_url = https://data.source.coop`.
At this S3 proxy, the account is the bucket and the product starts the key:
`write_prefix = s3://giuz/glace-alps`. The credentials work only through this
proxy.

Public reads use HTTPS at `data.source.coop` or anonymous S3 at
`eu-central-1.opendata.source.coop`; the latter appears in `alternate` hrefs.

### Upload commands

```bash
make publish-plan   # uv run --env-file .env tools/publish.py
make publish        # uv run --env-file .env tools/publish.py --confirm
uv run --env-file .env tools/publish.py --prefix tiles/2024 --confirm
```

The plan lists the proposed uploads and their reasons, and changes nothing.
`--prefix` limits the subtree, `--force` re-uploads matching objects and
`--workers` sets concurrency. Publishing only adds and replaces objects; remove
remote objects separately.

After publishing, run both checks:

```bash
make links-live         # HEAD every published href; compare sizes with the store
make conformance-live   # rashid: the catalog, its collection assets and its server
```

`make links-live` sends a HEAD request to each document in the store and to
every href under `public_base` that the documents name, about 345 000 for the
Alps. Each must answer 200 with the asset's `file:size`. With
`ARGS="--workers 64"` it takes about 4 minutes.

`make conformance-live` reads the collection assets, whose hrefs are absolute,
and probes their server for range requests and CORS. It leaves the item assets
to `make links-live`: rashid's `--live-base-url` would request them one at a
time, about ten hours for the Alps.

`tests/test_public_base.py` keeps every Source Cooperative URL in the
repository under the prefixes in `catalog.publish.yaml`. When the catalog
moves, add the old prefix to its `RETIRED_PREFIXES`.

## Related repositories

Read [docs/UPSTREAM.md](docs/UPSTREAM.md) before working in these repositories;
it defines their tasks and validation requirements.

- `../deep-glacier-mapping`: composite production, labels, ML and run manifests
  (M-31, D-4).
- `../s1bursts`: Sentinel-1 processing. Its COG writer (0.5.0 and later)
  embeds band statistics (`PTL-DAT-009`) and computes checksums. This
  repository only reads its output files.
- `../freezebase`: geospatial and IO helpers, including the VRT chain.

Use `freezebase` from PyPI at `>=0.10,<0.11`. It provides `S3Settings`,
`list_object_sizes`, `atomic_write_text`, `content_type_for` and profile
endpoints, and its VRT helpers write local sources relative to the VRT, so a
chain must not be moved. `MGRSGeoBox.from_mgrs` refuses a tile code that lies
outside its UTM zone. Keep `uv.lock` tracked and do not use editable installs.
