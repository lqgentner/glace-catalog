# glace-catalog

<p align="center">
<a href="https://lqgentner.github.io/glace-viewer">
<img src="assets/glace-wordmark.svg" alt="GLACE" width="350">
</a>
</p>

Contents

- 🗺️ [Interactive web map](https://lqgentner.github.io/glace-viewer)
- 💾 [Dataset on Source Cooperative](https://source.coop/lqgentner/glace-ch)
- 🧭 [Portolan browser of the STAC catalog](https://browser.portolan-sdi.org/#/external/data.source.coop/lqgentner/glace-ch/catalog.json)
- ⚙️ Production pipeline on GitHub (coming soon)
- 🏷️ Dataset metadata on GitHub (you are here)

---

Metadata, build pipeline and publication for the **GLACE** STAC catalogs —
annual Sentinel-1 coherence and backscatter composites over Alpine glaciers,
published to [Source Cooperative](https://source.coop/lqgentner/glace-ch) as a
[Portolan](https://www.portolan-sdi.org/) catalog.

> **What is published today is `glace-ch`, a rehearsal.** It covers Switzerland
> and exists to exercise publication while the Alpine composites are produced.
> The dataset is **`glace-alps`**; every mention of Switzerland in this
> repository is placeholder text. [`docs/STATUS.md`](docs/STATUS.md) carries the
> swap checklist and what is still open.

The catalog metadata lives in git so that it has a change history, a review
step, and a way for someone who spots a wrong license or a bad description to
open a pull request instead of finding an email address. The data does not: it
is built onto object storage and referenced by URL.

```
metadata/         the source: catalog.yaml, assets.yaml, the markdown templates
catalog/          the published tree, rendered from metadata/ and committed
src/glace_catalog/ the library that builds it
scripts/          the build commands, incl. render-catalog.py
tools/            sync_data.py, which publishes the built store
tests/            the gates, incl. rashid conformance
docs/             DESIGN.md, STATUS.md, UPSTREAM.md, conformance.md
```

## Quick start

```bash
uv sync --all-groups
uv run tests/run_all.py             # every gate
uv run rashid check catalog/        # Portolan conformance alone
```

The gates need the project environment: `rashid`, `stac-check` and `cmcrameri`
are dependencies here, not system tools, and a gate whose tool is missing fails
rather than skips.

## Where things are

| what | where |
| --- | --- |
| every title, description and display stretch | `metadata/assets.yaml`; `metadata/catalog.yaml` holds the catalog's identity and prose |
| the README and AGENTS.md templates | `metadata/templates/` |
| what publishes, and to which bucket | `catalog.publish.yaml` |
| where the build writes | `GLACE_STORE_ROOT` in `.env`; the composites it reads are `publish-year.py --src` |
| the layout of a built store, and why | [`docs/DESIGN.md`](docs/DESIGN.md) §2 |
| every design decision, with its reason | [`docs/DESIGN.md`](docs/DESIGN.md) §9 |
| what is done and what is open | [`docs/STATUS.md`](docs/STATUS.md) |
| what other repos must change | [`docs/UPSTREAM.md`](docs/UPSTREAM.md) |
| accepted validator findings | [`docs/conformance.md`](docs/conformance.md) |

Data never lands in this repository. `.gitignore` refuses `.tif` and `.parquet`,
and publication reads the built store on object storage, never this checkout.

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

## Building a store

A **store** is one region's published tree on object storage: the per-tile
composite COGs, annual mosaics of them, web-map archives, and the STAC documents
describing all of it. Two regions are defined, `ch` and `alps`, each with a
fixed grid and a default root in `src/glace_catalog/scopes.py`.

Building needs credentials for the private UZH bucket and the prefix the store
is assembled under, read from `.env`:

```bash
GLACE_S3_PROFILE=        # a named profile in ~/.aws/credentials
GLACE_S3_ENDPOINT_URL=   # the UZH S3 gateway, unless the profile's config section sets endpoint_url
GLACE_STORE_ROOT=        # s3:// prefix the published tree is built under
```

Authentication is by named profile only, so no access key passes through the
environment. The variables are read by `freezebase.settings.S3Settings`, which
is what `deep-glacier-mapping` reads its own `GLACIERS_S3_*` with. Publishing needs nothing here: the Source Cooperative credentials
live in the profile named in `catalog.publish.yaml`.

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
- `--concurrent-layers` — web-map layers warped at once, four by default.
  Each layer is warped once into an RGBA raster in the cache before it is
  tiled, so **this is what to lower if the machine runs out of disk**, and
  what to raise towards fourteen where there is room: an Alpine raster is
  about 1.1 GB.

Steps are safe to re-run. Each records a fingerprint of what it was built from
and skips itself only if nothing has changed; if the source composites, the
grid or the settings move, it rebuilds rather than leaving a stale result.

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

Publishing needs the Source Cooperative profile named in `catalog.publish.yaml`.
The built store is the published tree, so one command streams it object by
object from the private bucket to that profile's prefix:

```bash
uv run --env-file .env tools/sync_data.py            # dry run, uploads nothing
uv run --env-file .env tools/sync_data.py --confirm  # the whole store
uv run --env-file .env tools/sync_data.py --prefix tiles/2024 --confirm
```

The dry run prints what would be uploaded and why; `--prefix` restricts the run
to one subtree, `--force` re-uploads objects that already match on size, and
`--workers` sizes the transfer pool. Build state (anything whose name starts with
a dot) never leaves the store.

Publishing never deletes. Removing a file from the store leaves the object in
the bucket; delete it separately if that is what you meant.

## Citing

Cite a **release**, not the bucket: Source Cooperative is not immutable, and a
release names one edition of the pixels that a reader can check their bytes
against through each asset's `file:checksum`. Releases are git tags archived on
Zenodo, and `CITATION.cff` at the repository root is the machine-readable form —
GitHub's "Cite this repository" button reads it. **The DOI arrives with the first
release**; until then, cite the version and the repository URL.

## Contributing

Wrong metadata is a pull request against `metadata/`, followed by
`uv run scripts/render-catalog.py`. Run `uv run tests/run_all.py` before
pushing — a contribution that fails CI costs a round trip.

## License

Code: see `LICENSE`. The published data is CC-BY-4.0 and contains modified
Copernicus Sentinel data.
