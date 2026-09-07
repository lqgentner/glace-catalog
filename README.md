# glace-catalog

Metadata, build pipeline and publication for the **GLACE** STAC catalogs —
annual Sentinel-1 coherence and backscatter composites over Alpine glaciers,
published to [Source Cooperative](https://source.coop/lqgentner) as a
[Portolan](https://www.portolan-sdi.org/) catalog.

> **What is published today is `glace-ch`, a rehearsal.** It covers Switzerland
> and exists to exercise publication while the Alpine composites are produced.
> The dataset is **`glace-alps`**; every mention of Switzerland in this
> repository is placeholder text. See [`docs/MIGRATION.md`](docs/MIGRATION.md)
> §11 for the swap checklist.

The catalog metadata lives in git so that it has a change history, a review
step, and a way for someone who spots a wrong license or a bad description to
open a pull request instead of finding an email address. The data does not: it
is built onto object storage and referenced by URL.

```
catalog/          the published tree — STAC JSON, README.md, AGENTS.md
src/glace_catalog/ the library that builds it
scripts/          the build commands
tools/            publish.py (metadata) and upload_data.py (bytes)
tests/            the gates, incl. rashid conformance
docs/             MIGRATION.md (living), UPSTREAM.md, conformance.md
```

## Quick start

```bash
uv sync --all-groups
uv run python3 tests/run_all.py     # every gate
uv run rashid check catalog/        # Portolan conformance alone
```

The gates need the project environment: `rashid`, `stac-check` and `cmcrameri`
are dependencies here, not system tools, and a gate whose tool is missing fails
rather than skips.

## Where things are

| what | where |
| --- | --- |
| what publishes, and to which bucket | `catalog.publish.yaml` |
| where the build reads and writes | `GLACE_SOURCE_ROOT`, `GLACE_STORE_ROOT` in `.env` |
| the layout of a built store, and why | [`docs/MIGRATION.md`](docs/MIGRATION.md) §3 |
| what other repos must change | [`docs/UPSTREAM.md`](docs/UPSTREAM.md) |
| the transition from the old flat store | [`docs/MIGRATION.md`](docs/MIGRATION.md) |
| accepted validator findings | [`docs/conformance.md`](docs/conformance.md) |

Data never lands in this repository. `.gitignore` refuses `.tif` and `.parquet`,
and `tools/publish.py` only ever syncs `catalog/`.

## Building a store

A **store** is one region's published tree on object storage: the per-tile
composite COGs, annual mosaics of them, web-map archives, and the STAC documents
describing all of it. Two regions are defined, `ch` and `alps`, each with a
fixed grid and a default root in `src/glace_catalog/scopes.py`.

Building needs credentials for the private source bucket, read from `.env`.

### 1. Once per region

Compute the per-tile glacier cover. It is measured once and reused by every
year, so a tile cannot end up with different values in different years. The
script lives in `deep-glacier-mapping`, which owns the inventories it reads:

```bash
uv run --env-file .env python ../deep-glacier-mapping/scripts/inventories/derive-glacier-fractions.py \
    --scope ch --out s3://s1-bursts/data/glace-ch/glacier-fractions.parquet
```

### 2. Once per year

Every year needs a `manifest.parquet` beside its composites — one row per
file, listing what the run wrote. **The composite run emits it**, in
`deep-glacier-mapping`; see [`docs/UPSTREAM.md`](docs/UPSTREAM.md) D-4 for the
contract. Nothing here writes one: an archive without a manifest gets one by
rerunning its year, which skips every complete tile and describes and hashes
every file.

Publish the year:

```bash
uv run --env-file .env python scripts/publish-year.py 2024 \
    --src s3://s1-bursts/data/alps-total/2024/s1-comp-ch --scope ch
```

Five steps run in order:

| step | what it does |
| --- | --- |
| `copy` | Verifies the manifest against the archive, refuses anything the catalog cannot describe, then copies the composites in server-side. Nothing is re-encoded. |
| `vrt` | Builds the region-wide VRT chain the next two steps read from. Seconds, leaves nothing behind. |
| `mosaics` | Writes the twelve annual analysis mosaics. |
| `pmtiles` | Writes the fourteen web-map archives. |
| `items` | Writes the year's items and its slice of the item mirror. |

Useful options:

- `--steps copy,mosaics` — run only some steps.
- `--force-from mosaics` — rebuild from a step onward.
- `--layers coh12_vv,rtc_vv` — restrict to some layers.
- `--root` — build somewhere other than the region's default, such as a
  scratch prefix for a trial run.
- `--max-workers` — threads for the copy. It is network-bound, so more than
  you have cores is fine.
- `--tiler-workers` — processes for the web-map tiler, one per core by
  default. Each decodes raster blocks, so **this is what to lower if the
  machine runs out of memory**. It is unrelated to `--max-workers`.

Steps are safe to re-run. Each records a fingerprint of what it was built from
and skips itself only if nothing has changed; if the source composites, the
grid or the settings move, it rebuilds rather than leaving a stale result.

### 3. After adding or changing any year

```bash
uv run --env-file .env python scripts/finalize-catalog.py --scope ch \
    --base-url https://data.source.coop/lqgentner/glace-ch
```

This patches the two collection documents with what depends on the run — year
links, temporal extent, PMTiles links — concatenates
the per-year slices into each collection's `items.parquet`, repoints the
MapLibre styles at the newest year, and validates the result. It never touches
the prose, providers, license or `item_assets`: those are source, edited here
and reviewed.

`--base-url` is the address the store will be served from. It is baked into
every asset href, so **run this with the public address before publishing**,
not the source bucket's. Adding a year later needs only step 2 for that year
and step 3 again; existing years are never touched.

## Publishing

Publishing needs the Source Cooperative profile named in `catalog.publish.yaml`.
Two uploaders, one bucket prefix, and they do not overlap:

```bash
python3 tools/publish.py                # dry run, uploads nothing
python3 tools/publish.py --confirm      # metadata: catalog/ only
python3 tools/upload_data.py --confirm  # the rasters and parquet, from data_dir
```

Publishing never deletes. Removing a file from `catalog/` leaves the object in
the bucket; delete it separately if that is what you meant.

## Contributing

Wrong metadata is a pull request. Run `uv run python3 tests/run_all.py` before
pushing — a contribution that fails CI costs a round trip.

## License

Code: see `LICENSE`. The published data is CC-BY-4.0 and contains modified
Copernicus Sentinel data.
