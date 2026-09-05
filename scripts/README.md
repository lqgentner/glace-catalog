# Building a GLACE store

A **store** is a self-contained directory on object storage holding one region's
published data: the per-tile composite COGs, annual mosaics of them, web-map tiles,
and the STAC catalogue describing all of it. It is relocatable — every link inside is
relative, so moving the bucket means re-running one command, not rewriting the store.

Two regions are defined, `ch` (Switzerland) and `alps` (the European Alps). Each has a
fixed target grid and a default root, both in `src/glace_catalog/scopes.py`.

> **The mosaic grid is changing.** M-24 moves the annual analysis mosaics to
> ETRS89-LAEA 40 m; the web-map archives stay on WebMercator z13, resampled
> straight from the UTM composites. `scopes.py` currently carries one grid per
> scope and will carry two. Until that lands, what is described below is the
> single WebMercator grid.

## Layout

**A collection owns its subtree** (M-1): items live under the collection that
describes them, never under a shared year directory.

```
{root}/
├── catalog.json                     entry point
├── glacier-fractions.parquet        per-tile glacier cover
├── thumbnail.png
├── tiles/
│   ├── collection.json              one item per (tile, year)
│   ├── items.parquet                searchable index of every tile item
│   └── 2024/
│       ├── manifest.parquet         what the composite run produced
│       ├── config.yaml              how it was configured
│       └── 32TPS05/                 one directory per tile
│           ├── item.json
│           └── coh12_vv.tif  rtc_vh.tif  coh12_vv_qa_num.tif  … (12 files)
└── mosaics/
    ├── collection.json              one item per year
    ├── items.parquet                searchable index of every mosaic item
    ├── styles/default.json          MapLibre style; **source of truth** for
    │                                the display ramps and ranges (M-27)
    ├── 2024/
    │   ├── item.json
    │   └── coh12_vv.tif  …          the same 12 layers, region-wide
    └── pmtiles/2024/                14 web-map archives
```

The per-year `manifest.parquet` and `config.yaml` are **tiles-collection assets**
keyed `manifest-{year}` / `config-{year}`, not assets of the year directory that
holds them: `PTL-AST-005` forbids assets on a catalog (M-4).

Every file is named after what it holds, so nothing needs to be parsed to be
understood. The twelve layers are `{coh12,rtc}_{vv,vh}` plus a `_qa_num` (observation
count) and `_qa_cqm` (composite quality) layer for each.

Glacier label masks are **not** part of a store. They live in their own catalogue with
its own root, and are paired to composites on tile and year by whoever consumes them.

## Building one

### 1. Once per region

Compute the per-tile glacier cover. It is measured once and reused by every year, so a
tile cannot end up with different values in different years.

```bash
uv run --env-file .env python ../deep-glacier-mapping/scripts/stac/derive-glacier-fractions.py \
    --scope ch --out s3://s1-bursts/data/glace-ch/glacier-fractions.parquet
```

### 2. Once per year

Each composite run should leave a `manifest.parquet` beside its output, listing every
file it wrote. For older runs that have none, build one first:

```bash
uv run --env-file .env python scripts/build-manifest.py \
    --src s3://s1-bursts/data/alps-total/2024/s1-comp-ch
```

This refuses to overwrite an existing manifest — pass `--force` if you really mean to
replace it, or `--out` to write a copy elsewhere and compare.

Then publish the year:

```bash
uv run --env-file .env python scripts/publish-year.py 2024 \
    --src s3://s1-bursts/data/alps-total/2024/s1-comp-ch --scope ch
```

This runs five steps in order:

| step | what it does |
| --- | --- |
| `copy` | Checks the composites are complete and properly labelled, then copies them into the store. Nothing is downloaded or re-encoded — the files arrive byte-identical. |
| `vrt` | Prepares the region-wide views the next two steps read from. Takes seconds and leaves nothing behind. |
| `mosaics` | Writes the twelve annual mosaics. |
| `pmtiles` | Writes the fourteen web-map archives. |
| `items` | Writes the year's catalogue entries and its index. |

Useful options:

- `--steps copy,mosaics` — run only some steps.
- `--force-from mosaics` — rebuild from a step onward.
- `--layers coh12_vv,rtc_vv` — restrict to some layers.
- `--root` — publish somewhere other than the region's default, e.g. a scratch prefix
  for a trial run.
- `--max-workers` — threads for the copy. It is network-bound, so more than you have
  cores is fine.
- `--tiler-workers` — processes for the web-map tiler, one per core by default. Each
  one decodes raster blocks, so **this is what to lower if the machine runs out of
  memory**. It is unrelated to `--max-workers`.

Steps are safe to re-run. Each records what it was built from and skips itself only if
nothing has changed; if the source composites, the grid or the settings move, it
rebuilds rather than leaving a stale result behind.

### 3. After adding or changing any year

```bash
uv run --env-file .env python scripts/finalize-catalog.py \
    --scope ch --base-url https://<host>/glace-ch
```

This writes the catalogue's top level — the root, both collections, both search
indexes, the viewer manifest and the thumbnail — and validates the result. `--base-url`
is the address the store will be served from; it is the only absolute address anywhere
in it, so **relocating a published store is a single re-run of this command**.

Adding a year later needs only step 2 for the new year and step 3 again. Existing years
are never touched.

### 4. Publishing to Source Cooperative

**Set the published address first.** The store is relocatable, but two things in it are
absolute: the root catalogue's own address, and the asset links inside the two search
indexes. Both come from `--base-url`, so re-run step 3 pointing at where the data will
actually be served *before* copying anything:

```bash
uv run --env-file .env python scripts/finalize-catalog.py \
    --scope ch --base-url https://data.source.coop/<account>/glace-ch
```

Skipping this leaves the indexes pointing back at the source bucket, so the catalogue
resolves to files a reader may not be able to reach.

**Then copy via a local staging directory.** A direct bucket-to-bucket sync does not
currently work: Source Cooperative's S3-compatible endpoint is documented primarily as
read-only, and its proxy's header and signature handling rejects a server-side copy.
Until that is [fixed upstream](https://github.com/source-cooperative/data.source.coop),
the data has to come down and go back up.

**Retry both directions.** `sync` exits non-zero if any object failed, and only transfers
what is missing, so looping until it exits clean is the whole error handling — each pass
is shorter than the last:

```bash
STAGING=/path/to/staging
mkdir -p "$STAGING"

# Down from the source bucket
until aws s3 sync s3://s1-bursts/data/glace-ch/ "$STAGING/" \
        --profile uzh-glaciers; do
    echo "retrying download..."
done

# Up to Source Cooperative
until aws s3 sync "$STAGING/" s3://<account>/glace-ch/ \
        --profile source-coop; do
    echo "retrying upload..."
done
```

The loop is not belt-and-braces: **the upload fails on individual objects that the AWS CLI
will not retry by itself.** Source Cooperative's proxy intermittently answers `PutObject`
with HTTP 520, and the CLI only retries 500, 502, 503 and 504 — so a 520 is reported as
`upload failed:` and abandoned, while the rest of the transfer carries on. Raising
`AWS_MAX_ATTEMPTS` does not help, because it only multiplies attempts for errors already
classed as retryable.

Nothing is left corrupt when that happens: `PutObject` is atomic, so a failed upload leaves
no object at all and the next pass simply uploads it. A file that fails on *every* pass is
a different problem and worth looking at on its own.

**Check it landed complete.** The counts should match:

```bash
find "$STAGING" -type f | wc -l
aws s3 ls s3://<account>/glace-ch/ --recursive --profile source-coop | wc -l
```

Budget disk for the whole store — roughly 3.4 GB per Swiss year, so about 14 GB for
2021-2024. Adding a year later re-syncs just that year plus the top-level documents.

## Reading a store

Nothing needs a server. Open the catalogue directly:

```python
import pystac
catalog = pystac.Catalog.from_file("https://<host>/glace-ch/catalog.json")
```

Or query an index, which is far faster for anything selective:

```python
import rustac
items = rustac.search_sync(
    "https://<host>/glace-ch/tiles.parquet",
    filter='"glace:glacier_fraction" > 0.3',
    datetime="2024-01-01T00:00:00Z/2024-12-31T00:00:00Z",
)
```

Asset values are stored as measured, without display scaling — backscatter and the
quality layers are linear power, not decibels. Convert on read if you want decibels.
Missing data is `NaN`, except in the per-tile observation counts where it is `0`.

The collection `AGENTS.md` files walk through searching, previewing and reading
pixels.

## Labels

Built separately, from masks produced by `rasterize-labels.py`:

```bash
uv run --env-file .env python scripts/stac/build-labels-catalog.py \
    --labels-dir s3://s1-bursts/data/labels/sgi2023 \
    --root s3://s1-bursts/data/labels \
    --base-url https://<host>/labels
```

The mask directories must sit inside `--root`. Masks use `1` for glacier, `0` for
background and `255` for *ignore* — glaciers surveyed in a different year — which must
be excluded from training and evaluation rather than treated as background.
