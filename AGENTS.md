# glace-catalog — repository guide

Use this guide to change the repository. To use the published data, read
[catalog/AGENTS.md](catalog/AGENTS.md) and the collection guides.

## Edit sources and regenerate

**Edit source files, never generated catalog documents.**

| Source | Output | Generator |
| --- | --- | --- |
| `metadata/catalog.yaml`, `metadata/assets.yaml`, `metadata/templates/` | Tracked STAC documents, default style, READMEs and agent guides in `catalog/` | `scripts/render-catalog.py` |
| Composite manifests, rasters and build settings | Store items, annual mosaics, PMTiles, item indexes, year styles and updated collections | `scripts/publish-year.py`, `scripts/finalize-catalog.py` |

The rendered catalog is committed for review and release archives. Built store
outputs are not tracked. Other hand-maintained catalog files are
`catalog.publish.yaml` and `catalog/_assets/`. Rendering and building share
`glace_catalog.metadata`, keeping item and collection asset names consistent.

After editing metadata or templates:

```bash
uv run scripts/render-catalog.py
uv run tests/test_render.py
uv run tests/run_all.py
```

The render test requires the tracked catalog to match its sources. Lint runs
`ruff format --check`, `ruff check` and strict `pyrefly check`; missing tools
fail the gate.

`rashid` is the Portolan conformance gate; `stac-check` is advisory. Where they
disagree, rashid takes precedence. Do not widen `ACCEPTED` in
`tests/test_conformance.py` just to pass CI. Any new exception needs a row in
`docs/conformance.md` and a tracking issue; otherwise fix the finding.
The two existing exceptions reflect the tracked tree's lack of built items.
Validate the full built tree before publication.

## Scope and layout

`glace-alps` is the dataset and the scope `metadata/catalog.yaml` names;
`glace-ch` was the Swiss rehearsal. `src/glace_catalog/scopes.py` defines both
scopes and grids. Switching scope also requires updating
`scope`, `region`, `years`, `title` and the preliminary notice in
`metadata/catalog.yaml`.
See `docs/DESIGN.md` §8 and the checklist in `docs/STATUS.md`.

Preserve these rules:

- Collections contain their items: `tiles/{year}/{tile}/item.json`.
  Year catalogs sit below collections and only group items.
- Catalogs have no assets. Assets belong to collections or items. Run manifests
  and configurations are never published (M-32).
- Structural links, `rel: "pmtiles"` links, item asset hrefs and the
  collections' style asset hrefs are relative. The other collection asset hrefs
  and the hrefs in `items.parquet` are absolute HTTPS; `s3://` hrefs appear only
  in the `alternate` extension.
- Tracked documents have no `self` link. Publication adds it from `public_base`.
- Analysis COGs store linear values. PMTiles contain 8-bit display imagery;
  backscatter and QA-CQM displays use decibels.
- `{stem}_viz.pmtiles` belongs in its year's item directory, with the `visual`
  role and a collection `rel: "pmtiles"` link. Annual MapLibre styles are
  collection assets under `styles/` with the `style` role. Neither uses `data`.

## Build a store

A store contains a region's COGs, mosaics, web maps and STAC documents on object
storage. Run commands from this checkout except where indicated below.

### Credentials

Building reads `.env` through `freezebase.settings.S3Settings`:

```bash
GLACE_S3_PROFILE=        # named profile in ~/.aws/credentials
GLACE_S3_ENDPOINT_URL=   # UZH gateway, unless set in the profile
GLACE_STORE_ROOT=        # built store: s3:// prefix, or a local directory
```

`publish-year.py` and `finalize-catalog.py` build under `GLACE_STORE_ROOT`
unless `--root` is given. A local root works offline and publishes the same way.

Use named profiles only; do not pass access keys through the environment.
Publication uses the separate profile in `catalog.publish.yaml`.

### 1. Compute glacier fractions once per region

Run this in `deep-glacier-mapping`, which provides the inventory data and
`glacier_mapping` package. It runs on pixi, whose activation loads that
checkout's `.env`. Use a full output prefix matching this store; that checkout
does not define `GLACE_STORE_ROOT`.

```bash
( cd ../deep-glacier-mapping &&
  pixi run python scripts/inventories/derive-glacier-fractions.py \
      --scope alps --perimeter-tiles 2210 \
      --out s3://s1-bursts/data/glace-alps/tiles/glacier-fractions.parquet )
```

Every year reuses this lookup so a tile's glacier fraction stays constant.

### 2. Build each year

The source archive must contain `manifest.parquet`, one row per file, emitted
by the composite run in `deep-glacier-mapping`. See `docs/UPSTREAM.md` D-4.
If missing, rerun the upstream year: it skips complete tiles and describes and
hashes their files. This repository does not create source manifests.

```bash
uv run --env-file .env scripts/publish-year.py 2023 \
    --src s3://s1-bursts/data/alps-total/2023/s1-comp --scope alps
```

| Step | Result |
| --- | --- |
| `copy` | Validates the manifest and supported assets, then copies composites server-side without re-encoding. |
| `vrt` | Builds temporary regional VRTs for the next two steps. |
| `mosaics` | Writes twelve annual analysis mosaics. |
| `pmtiles` | Writes fourteen web-map archives. |
| `items` | Writes STAC items and the year's item-index slice. |

Steps can be rerun. Fingerprints skip unchanged work and trigger rebuilds when
source composites, grids or settings change.

| Option | Purpose |
| --- | --- |
| `--steps copy,mosaics` | Run selected steps. |
| `--force-from mosaics` | Rebuild from a step onward. |
| `--layers coh12_vv,rtc_vv` | Select layers. |
| `--root` | Use another store prefix, such as a scratch location. |
| `--max-workers` | Set copy threads; transfers are network-bound. |
| `--tiler-workers` | Set tiler processes; defaults to one per core, each with a 256 MB GDAL cache. |
| `--concurrent-layers` | Set simultaneous layer warps; defaults to four. |

Size web-map concurrency to available RAM and scratch disk. Each layer writes
an RGBA raster of the scope: about 100 MB for Switzerland or 1.1 GB for the
Alps. Defaults suit a 16 GB laptop. If memory runs out, reduce
`--concurrent-layers` first; reduce tiler workers when memory cannot support
one process per core.

Swiss measurements on 2026-09-11, with 15 cores and 60 GB RAM:

| Tiler workers | Concurrent layers | Peak process-tree RSS | PMTiles time |
| --- | --- | --- | --- |
| 15 | 4 | 11 GB | 12 min |
| 15 | 7 | Not measured | 7 min |

The old 57 GB peak came from GDAL's per-worker default cache; the bounded cache
fix replaced that behavior on 2026-09-11.

Alpine 2023 on 2026-09-24, same machine, default workers, local source and
store root on one disk (2 210 tiles, 35 GB store):

| Step | Time |
| --- | --- |
| `copy` | 4 min |
| `vrt` | 4 min |
| `mosaics` | 48 min |
| `pmtiles` | 1 h 44 min |
| `items` | 1 min |
| Total | 2 h 41 min |

About 18 GB of system memory was in use when sampled during `pmtiles`; the
process-tree peak was not measured. Scratch peaked near 4 GB of RGBA rasters
beside 2.7 GB of staged mosaics. Finalizing this one year took 11 s, including
thumbnails and validation.

### 3. Finalize after adding or changing a year

```bash
uv run --env-file .env scripts/finalize-catalog.py \
    --base-url https://data.source.coop/giuz/glace-alps
```

Finalization updates collection year links, temporal extent and PMTiles links;
combines item-index slices into `items.parquet`; creates thumbnails; records
asset sizes and checksums; points default styles to the latest year; and
validates the store. Prose, providers, license and `item_assets` remain defined
by the tracked metadata.

**Use the public serving address for `--base-url` before publication.** It is
written into the root `self` link and every asset href in `items.parquet`. The
absolute collection asset hrefs come from `public_base` in
`catalog.publish.yaml` when the catalog is rendered, so the two must name the
same address. Adding a year requires building that year and
finalizing again; existing years are unchanged.

Thumbnails are drawn from the latest year's `coh12_vv.tif` COGs over an
OpenStreetMap basemap, so this step needs network access to the tile service.
`--thumbnail-basemap` takes another contextily provider or a local georeferenced
raster, and `--thumbnail-attribution` overrides the credit — an empty string is
refused for a tile provider and is only for attribution-free local data.
`--skip-thumbnail` keeps the thumbnails already in the store and fails if they
are not there, so it can never leave a collection pointing at a missing image.

## Publish the store

`tools/sync_data.py` streams the built store from `settings.store_root`
(`GLACE_STORE_ROOT`) to `write_prefix` in `catalog.publish.yaml`, using its
named profile and configured endpoint. Objects transfer in chunks with their
content types; no intermediate staging is used.

**Upload the built store. Do not sync tracked `catalog/` over it:** that would
remove the collection updates made by finalization. Do not use `portolan push`;
its `versions.json` upload tracking is incompatible with this uploader.

Change detection lists each side once. Different sizes trigger upload;
single-part MD5 ETags decide equal-size objects when available. Otherwise,
upload when the source is newer. `--force` bypasses these checks. Dot-prefixed
build files, including `.build-state.json` and `.items.parquet`, are excluded.

### Endpoint

The `source-coop` profile uses `endpoint_url = https://data.source.coop`.
At this S3 proxy, the account is the bucket and the product starts the key:
`write_prefix = s3://giuz/glace-alps`. AWS-native bucket names reject these
credentials.

Public reads use HTTPS at `data.source.coop` or anonymous S3 at
`eu-central-1.opendata.source.coop`; the latter appears in `alternate` hrefs.

### Upload commands

```bash
uv run --env-file .env tools/sync_data.py            # dry run
uv run --env-file .env tools/sync_data.py --confirm  # upload the store
uv run --env-file .env tools/sync_data.py --prefix tiles/2024 --confirm
```

The dry run reports proposed uploads and reasons. `--prefix` limits the
subtree, `--force` reuploads matching objects and `--workers` sets concurrency.
Publishing never deletes remote objects; deletions require a separate action.

## Related repositories

Read [docs/UPSTREAM.md](docs/UPSTREAM.md) before working in these repositories;
it defines their tasks and validation requirements.

- `../deep-glacier-mapping`: composite production, labels, ML and run manifests
  (M-31, D-4).
- `../s1bursts`: Sentinel-1 processing. Its COG writer supplies embedded band
  statistics (`PTL-DAT-009`) and checksums, released in 0.5.0. This repository
  reads its output but does not depend on or import it.
- `../freezebase`: geospatial and IO helpers, including the VRT chain.

Use `freezebase` from PyPI at `>=0.7.1,<0.8`; 0.7.1 introduced `S3Settings`,
`list_object_sizes`, `atomic_write_text`, `content_type_for` and profile
endpoints. Keep `uv.lock` tracked and do not use editable installs.
