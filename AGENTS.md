# glace-catalog — repository guide

Use this guide to change the repository. To use the published data, read
[catalog/AGENTS.md](catalog/AGENTS.md) and the collection guides.

## Edit sources and regenerate

**Edit source files, never generated catalog documents.**

| Source | Output | Generator |
| --- | --- | --- |
| `metadata/catalog.yaml`, `metadata/assets.yaml`, `metadata/templates/` | Tracked STAC documents, default style, READMEs and agent guides in `catalog/` | `scripts/render-catalog.py` |
| Composite manifests, rasters and build settings | Store items, annual mosaics, PMTiles, item indexes, year styles and updated collections | `scripts/publish-year.py`, `scripts/finalize-catalog.py` |

The rendered catalog is committed, for review and for release archives; built
store outputs live only in the store. The other hand-maintained files are
`catalog.publish.yaml` and `catalog/_assets/`. Rendering and building share
`glace_catalog.metadata`, so items and collections name assets the same way.

After editing metadata or templates:

```bash
uv run scripts/render-catalog.py
uv run tests/test_render.py
uv run tests/run_all.py
```

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

`glace-alps` is the dataset and the scope that `metadata/catalog.yaml` names;
`glace-ch` is the Swiss rehearsal. `src/glace_catalog/scopes.py` defines both
scopes and their grids. To switch scope, also update `scope`, `region`,
`years`, `title` and the preliminary notice in `metadata/catalog.yaml`. See
`docs/DESIGN.md` §8 and the checklist in `docs/STATUS.md`.

Preserve these rules:

- Collections contain their items: `tiles/{year}/{tile}/item.json`.
  Year catalogs sit below collections and only group items.
- Assets belong to collections and items only. Run manifests and
  configurations stay beside the source archive and are never published (M-32).
- Structural links, item asset hrefs and the collections' style asset hrefs are
  relative. The other collection asset hrefs and the hrefs in `items.parquet`
  are absolute HTTPS; `s3://` hrefs appear only in the `alternate` extension.
- Publication writes the `self` link, from `public_base`; tracked documents
  leave it out.
- Analysis COGs store linear values. PMTiles store 8-bit value codes: each
  layer is encoded over a fixed transport range from `metadata/assets.yaml`,
  backscatter and QA-CQM in decibels. The style decodes the codes
  (`raster-dem`, custom encoding) and colours them (`color-relief`) with a
  default ramp. Changing a ramp or stretch means re-rendering documents;
  changing a transport range means re-tiling every year.
- `{stem}_viz.pmtiles` belongs in its year's item directory, as an item asset
  with the `visual` role. Item assets are the archives' only registration, a
  recorded deviation from `PORTO-FMT-011` (`docs/conformance.md`). Annual
  MapLibre styles are collection assets under `styles/` with the `style` role.
  The `data` role is for the COGs alone.

## Build a store

A store holds a region's COGs, mosaics, web maps and STAC documents, on object
storage or a local disk. Run commands from this checkout unless stated
otherwise.

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

Run this in `deep-glacier-mapping`, which provides the inventory data and the
`glacier_mapping` package. It runs on pixi, which loads that checkout's `.env`.
Write the output prefix in full, matching this store's `GLACE_STORE_ROOT`.

```bash
( cd ../deep-glacier-mapping &&
  pixi run python scripts/inventories/derive-glacier-fractions.py \
      --scope alps --perimeter-tiles 2210 \
      --out s3://s1-bursts/data/glace-alps/tiles/glacier-fractions.parquet )
```

Every year reuses this lookup, so a tile's glacier fraction stays constant.

### 2. Build each year

The source archive must contain `manifest.parquet`, one row per file, written
by the composite run in `deep-glacier-mapping` (`docs/UPSTREAM.md` D-4). Source
manifests come only from that run: to add a missing one, rerun the upstream
year, which skips complete tiles and describes and hashes their files.

```bash
uv run --env-file .env scripts/publish-year.py 2023 \
    --src s3://s1-bursts/data/alps-total/2023/s1-comp --scope alps
```

| Step | Result |
| --- | --- |
| `copy` | Validates the manifest and supported assets, then copies composites server-side without re-encoding; on one local disk it hard-links them. |
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
| `--root` | Use another store prefix, such as a scratch location. |
| `--max-workers` | Set copy threads; transfers are network-bound. |
| `--mosaic-workers` | Set mosaic processes, one layer each; defaults to four. |
| `--tiler-workers` | Set tiler processes; defaults to one per core, each with a 256 MB GDAL cache. |
| `--concurrent-layers` | Set simultaneous layer warps; defaults to four. |

Size web-map concurrency to the available RAM and scratch disk. Each layer
writes a local RGBA raster of the whole scope: about 100 MB for Switzerland,
1.1 GB for the Alps. The defaults suit a 16 GB laptop. If memory runs out,
lower `--concurrent-layers` first, then `--tiler-workers`.

Swiss runs on 2026-09-11, with 15 cores and 60 GB RAM:

| Tiler workers | Concurrent layers | Peak process-tree RSS | PMTiles time |
| --- | --- | --- | --- |
| 15 | 4 | 11 GB | 12 min |
| 15 | 7 | Not measured | 7 min |

Alpine 2023 on 2026-09-24, same machine, default workers, source and store on
one local disk (2 210 tiles, 35 GB store):

| Step | Time |
| --- | --- |
| `copy` | 4 min |
| `vrt` | 4 min |
| `mosaics` | 48 min |
| `pmtiles` | 1 h 44 min |
| `items` | 1 min |
| Total | 2 h 41 min |

Scratch use peaked near 7 GB: about 4 GB of RGBA rasters and 2.7 GB of staged
mosaics.

### 3. Finalize after adding or changing a year

```bash
uv run --env-file .env scripts/finalize-catalog.py \
    --base-url https://data.source.coop/giuz/glace-alps
```

Finalization:

- updates the collections' year links and temporal extent;
- combines the item-index slices into `items.parquet`;
- draws the thumbnails;
- records asset sizes and checksums;
- points the default style to the latest year;
- validates the store.

Prose, providers, license and `item_assets` come from the tracked metadata.

**Use the public serving address for `--base-url` before publication.** It is
written into the root `self` link and every asset href in `items.parquet`. The
absolute collection asset hrefs come from `public_base` in
`catalog.publish.yaml` at render time, so both must name the same address. To
add a year, build it and finalize again; the other years stay as they are.

Thumbnails are drawn from the latest year's `coh12_vv.tif` COGs over an
OpenStreetMap basemap, so this step needs network access to the tile service.
`--thumbnail-basemap` takes another contextily provider or a local
georeferenced raster. `--thumbnail-attribution` overrides the credit; an empty
credit is accepted only for a local raster. `--skip-thumbnail` keeps the
thumbnails already in the store, and fails if there are none.

## Publish the store

`tools/sync_data.py` streams the built store from `settings.store_root`
(`GLACE_STORE_ROOT`) to `write_prefix` in `catalog.publish.yaml`, using that
file's named profile and its configured endpoint. Objects are copied directly,
in chunks, with their content types.

**Upload the built store. Do not sync the tracked `catalog/` over it:** that
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
uv run --env-file .env tools/sync_data.py            # dry run
uv run --env-file .env tools/sync_data.py --confirm  # upload the store
uv run --env-file .env tools/sync_data.py --prefix tiles/2024 --confirm
```

The dry run lists the proposed uploads and their reasons. `--prefix` limits
the subtree, `--force` re-uploads matching objects and `--workers` sets
concurrency. Publishing only adds and replaces objects; remove remote objects
separately.

## Related repositories

Read [docs/UPSTREAM.md](docs/UPSTREAM.md) before working in these repositories;
it defines their tasks and validation requirements.

- `../deep-glacier-mapping`: composite production, labels, ML and run manifests
  (M-31, D-4).
- `../s1bursts`: Sentinel-1 processing. Its COG writer (0.5.0 and later)
  embeds band statistics (`PTL-DAT-009`) and computes checksums. This
  repository only reads its output files.
- `../freezebase`: geospatial and IO helpers, including the VRT chain.

Use `freezebase` from PyPI at `>=0.7.1,<0.8`, the range that provides
`S3Settings`, `list_object_sizes`, `atomic_write_text`, `content_type_for` and
profile endpoints. Keep `uv.lock` tracked and do not use editable installs.
