# glace-catalog — repository guide

This is the **repository**, not the published catalog. The published catalog's
own agent guides are `catalog/AGENTS.md` and one per collection; read those to
*use* the data. Read this one to *change* it.

## The one rule

**Edit the source, not the generated output.** Two generators, two outputs:

- `scripts/render-catalog.py` renders everything under `catalog/` (the root
  catalog, both `collection.json`, `mosaics/styles/default.json`, every
  `README.md` / `AGENTS.md` down to the collection level) from `metadata/`:
  `catalog.yaml` for identity and prose, `assets.yaml` for every asset title,
  description and display stretch, `templates/` for the markdown. `catalog/` is
  committed so the rendered tree is reviewable and archived with a release, and
  `tests/test_render.py` fails when it and a fresh render differ.
- `scripts/publish-year.py` and `scripts/finalize-catalog.py` write the store:
  everything under `catalog/tiles/{year}/`, `catalog/mosaics/{year}/`, every
  `items.parquet`, the per-year styles and the patched collection documents.
  None of that is in git.

The hand-maintained files are `metadata/*`, `catalog.publish.yaml` and
`catalog/_assets/`. The build reads the same vocabulary the render does
(`glace_catalog.metadata`), so an item's asset title is its collection's
`item_assets` title by construction.

## Scope

`glace-ch` is a rehearsal over Switzerland. The dataset is `glace-alps`.
`src/glace_catalog/scopes.py` is the seam: it already carries both scopes as
committed constants, so the swap is a scope change below it and a few lines in
`metadata/catalog.yaml` above it: `scope`, `region`, `years`, `title` and the
preliminary notice. The prose below the root title is already written for the
Alpine Convention perimeter. `docs/DESIGN.md` §8 says what changes;
`docs/STATUS.md` carries the checklist.

## Layout invariants worth not breaking

- **Collections own their subtrees.** `tiles/{year}/{tile}/item.json`, not
  `{year}/tiles/...`. Portolan requires a collection directory to hold its items,
  and a catalog may only appear *below* a collection to organize them.
- **A catalog carries no assets.** Only collections and items may. The year
  catalogs group items and hold nothing else; the run's manifest and
  configuration are not published at all (M-32).
- **Structural links are relative; asset hrefs are absolute https.** The first
  keeps the tree valid in the repo, in a preview and in production. The second
  is what makes assets resolve for a client reading one object in isolation, and
  what makes them clickable in the Source Cooperative file listing. `s3://`
  appears only under the `alternate` extension, never as a primary href.
- **The `self` link is not in the tracked tree.** The publish step writes it
  from `public_base`. A tracked file whose correct content depends on the
  deployment target is diff noise and a merge conflict waiting to happen.
- **Renderings are never `data`.** Every COG in the catalog is linear; the only
  display pixels are inside the PMTiles archives, which are 8-bit codes under a
  fixed stretch and, for backscatter and QA-CQM, in decibels. Each archive is
  `{stem}_viz.pmtiles` in its year's item directory, an asset of that item with
  the `visual` role and a collection-level `rel: "pmtiles"` link; the MapLibre
  style of each year is a collection asset under `styles/` with the `style` role.
  None of them is ever a `data` asset: marking one would let an analysis pipeline
  read a picture.

## Editing the metadata

`catalog/` is generated output that is committed, so that the rendered tree is
what a reviewer diffs, what rashid checks in CI, and what a release archives on
Zenodo. Edit the source and render:

```bash
$EDITOR metadata/assets.yaml metadata/catalog.yaml metadata/templates/...
uv run scripts/render-catalog.py            # writes catalog/
uv run tests/test_render.py                 # the gate CI runs: catalog/ matches its source
```

A hand edit under `catalog/` fails that gate. The same vocabulary names every
asset the build writes, so an item and its collection cannot disagree.

### Gates

```bash
uv run tests/run_all.py
```

`tests/test_lint.py` runs `ruff format --check`, `ruff check` and `pyrefly
check` under `preset = "strict"`; a missing tool fails rather than skips.
`rashid` is the Portolan conformance gate; `stac-check` is advisory. Where they
disagree, rashid wins. Do not widen the `ACCEPTED` set in
`tests/test_conformance.py` to make CI pass — add a row to
`docs/conformance.md` with a tracking issue, or fix the finding. Both, or
neither.

The gates run against the tracked tree, which has no generated items in it. Two
findings are structural consequences of that and are recorded in
`docs/conformance.md`; the full gate runs against a built tree before publication.

## Building a store

A **store** is one region's published tree on object storage: the per-tile
composite COGs, annual mosaics of them, web-map archives, and the STAC documents
describing all of it. Two regions are defined, `ch` and `alps`, each with a
fixed grid and a default root in `src/glace_catalog/scopes.py`.

### Credentials

Building needs credentials for the private UZH bucket and the prefix the store
is assembled under, read from `.env`:

```bash
GLACE_S3_PROFILE=        # a named profile in ~/.aws/credentials
GLACE_S3_ENDPOINT_URL=   # the UZH S3 gateway, unless the profile's config section sets endpoint_url
GLACE_STORE_ROOT=        # s3:// prefix the published tree is built under
```

Authentication is by named profile only, so no access key passes through the
environment. The variables are read by `freezebase.settings.S3Settings`, which
is what `deep-glacier-mapping` reads its own `GLACIERS_S3_*` with. Publishing
needs nothing here: the Source Cooperative credentials live in the profile named
in `catalog.publish.yaml`.

### 1. Once per region

Compute the per-tile glacier cover. It is measured once and reused by every
year, so a tile cannot end up with different values in different years. The
script lives in `deep-glacier-mapping`, which owns the inventories it reads, and
**runs from that checkout** — it imports `glacier_mapping`, which this project's
environment does not have:

```bash
( cd ../deep-glacier-mapping &&
  uv run --env-file .env scripts/inventories/derive-glacier-fractions.py \
      --scope ch --perimeter-tiles 143 \
      --out s3://s1-bursts/data/glace-ch/tiles/glacier-fractions.parquet )
```

The subshell is the point: every later step runs from this checkout. Write the
`--out` prefix in full, too — that checkout's `.env` does not define
`GLACE_STORE_ROOT`, and the value has to match this repository's.

### 2. Once per year

Every year needs a `manifest.parquet` beside its composites — one row per
file, listing what the run wrote. **The composite run emits it**, in
`deep-glacier-mapping`; see [`docs/UPSTREAM.md`](docs/UPSTREAM.md) D-4 for the
contract. Nothing here writes one: an archive without a manifest gets one by
rerunning its year, which skips every complete tile and describes and hashes
every file.

Publish the year:

```bash
uv run --env-file .env scripts/publish-year.py 2024 \
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

Steps are safe to re-run. Each records a fingerprint of what it was built from
and skips itself only if nothing has changed; if the source composites, the
grid or the settings move, it rebuilds rather than leaving a stale result.

#### Build options

- `--steps copy,mosaics` — run only some steps.
- `--force-from mosaics` — rebuild from a step onward.
- `--layers coh12_vv,rtc_vv` — restrict to some layers.
- `--root` — build somewhere other than the region's default, such as a
  scratch prefix for a trial run.
- `--max-workers` — threads for the copy. It is network-bound, so more than
  you have cores is fine.
- `--tiler-workers` — processes for the web-map tiler, one per core by
  default. Each starts with a bounded GDAL cache (256 MB) and holds the
  source it tiles open, so a worker costs well under a gigabyte. It is
  unrelated to `--max-workers`.
- `--concurrent-layers` — web-map layers warped at once, four by default.
  Each warp writes one RGBA raster of the whole scope into the cache
  directory before it is tiled, so this is what disk and memory scale with:
  about 100 MB on disk per layer for Switzerland, 1.1 GB for the Alps.

#### Web-map resource sizing

**The web-map step is sized to the machine, not to a rule.** The defaults are
safe on a 16 GB laptop; whether to raise `--concurrent-layers` towards
fourteen depends on the RAM and scratch disk of the machine running it, and
the wall time follows the warps, which are serial per layer. Measured on the
Swiss scope, 2026-09-11, on 15 cores and 60 GB:

| `--tiler-workers` | `--concurrent-layers` | peak RSS, whole process tree | PMTiles step |
| --- | --- | --- | --- |
| 15 (default) | 4 (default) | 11 GB | 12 min |
| 15 (default) | 7 | not sampled | 7 min |

Before 2026-09-11 every worker ran with GDAL's default cache of 5 % of RAM,
so fifteen workers claimed about 45 GB between them and the same step peaked
at 57 GB; that is fixed in the tiler, not something to work around with fewer
workers. If the step is killed for memory, lower `--concurrent-layers` first;
`--tiler-workers` only matters when the machine has fewer cores than memory
for them.

### 3. After adding or changing any year

```bash
uv run --env-file .env scripts/finalize-catalog.py --scope ch \
    --base-url https://data.source.coop/lqgentner/glace-ch
```

This patches the two collection documents with what depends on the run — year
links, temporal extent, PMTiles links — concatenates the per-year slices into
each collection's `items.parquet`, renders both collection thumbnails, stamps
`file:size` and `file:checksum` on the assets it wrote, repoints the MapLibre
styles at the newest year, and validates the result. It never touches the prose,
providers, license or `item_assets`: those are rendered from `metadata/` and
reviewed in git.

`--base-url` is the address the store will be served from. It is baked into
every asset href, so **run this with the public address before publishing**,
not the source bucket's. Adding a year later needs only step 2 for that year
and step 3 again; existing years are never touched.

## Publishing

### Publication model

One uploader, one bucket prefix. `tools/sync_data.py` streams every object
under `GLACE_STORE_ROOT` to the `write_prefix` in `catalog.publish.yaml`,
documents and data alike: the store *is* the published tree, and its collection
documents are the rendered, tracked ones that `finalize-catalog` patched on the
way in. Syncing the tracked `catalog/` over them would strip those patches,
which is why nothing does. Nothing is staged anywhere in between: both sides are `UPath`
roots, and each object is read from one and written to the other in chunks,
with its content type on the write. The source root is `settings.store_root`;
the target is the `write_prefix` opened with the `profile` alone, whose config
section carries the endpoint. Change detection is three steps from one listing
per side, reading no bytes: a different size is a change; two single-part MD5
ETags decide an equal size either way; what they leave is changed only when
the source is newer than its published copy, the `aws s3 sync` rule, which
holds because the build writes before publication copies. `--force` re-uploads
regardless. Build state (`.build-state.json`, `.items.parquet`, anything whose
name starts with a dot) never leaves the store.

`portolan push` is **not** used here and must not be mixed in — it tracks
uploads in `versions.json` keyed on sha256, which `sync_data.py` knows nothing
about.

### Source Cooperative endpoint

**Source Cooperative is addressed through its S3 proxy.** The `source-coop`
profile carries `endpoint_url = https://data.source.coop`, and against that
endpoint the bucket is the *account* (`lqgentner`) and the key starts with the
*product* (`glace-ch/…`) — which is why `write_prefix` is
`s3://lqgentner/glace-ch` and not one of the AWS-native bucket names, which
reject these credentials. Public reads use `https://data.source.coop/...` or
anonymous S3 against `eu-central-1.opendata.source.coop`, which is the form the
`alternate` hrefs carry.

### Upload commands

One command streams the store object by object from the private bucket to the
prefix of the profile named in `catalog.publish.yaml`:

```bash
uv run --env-file .env tools/sync_data.py            # dry run, uploads nothing
uv run --env-file .env tools/sync_data.py --confirm  # the whole store
uv run --env-file .env tools/sync_data.py --prefix tiles/2024 --confirm
```

The dry run prints what would be uploaded and why; `--prefix` restricts the run
to one subtree, `--force` re-uploads objects that already match on size, and
`--workers` sizes the transfer pool.

Publishing never deletes. Removing a file from the store leaves the object in
the bucket; delete it separately if that is what you meant.

## Related repositories

**If you are working in one of them, read [`docs/UPSTREAM.md`](docs/UPSTREAM.md),
not this file.** It is the per-repository work order: what to change, why, and
how to know it worked.

- `../deep-glacier-mapping` — composite production, labels, ML. Produces what
  this catalog publishes, including the run manifest (M-31, `docs/UPSTREAM.md`
  D-4).
- `../s1bursts` — the Sentinel-1 pipeline. Its COG writer emits the embedded band
  statistics `PTL-DAT-009` needs and the digest `file:checksum` publishes, both
  released in 0.5.0.
- `../freezebase` — geo and IO helpers, including the VRT chain the mosaics use.

**s1bursts is not a dependency of this repository**: it is the writer whose
output this repository reads, and nothing here imports it. `freezebase` is a
dependency and is consumed from PyPI, floored at `>=0.7.1,<0.8` because 0.7.1
is where `freezebase.settings.S3Settings`, `list_object_sizes`,
`atomic_write_text`, `content_type_for` and the profile-configured endpoint
arrived; there are no editable installs and `uv.lock` is tracked.
