# What the other repositories owe this one

**Read this if you are working in `s1bursts`, `freezebase` or
`deep-glacier-mapping` and were pointed at the GLACE catalog.** It is the whole
cross-repository contract: what to change, why, and how to know it worked. You
should not need [`DESIGN.md`](DESIGN.md), the design record, to do your part.

**First:** this catalog conforms to [Portolan](https://www.portolan-sdi.org/),
and conformance means passing [`rashid`](https://github.com/portolan-sdi/rashid).
Several of its error-severity rules check bytes written in other repositories,
so they can only be satisfied there.

**No repository here imports another** (M-30). They share files on disk: the
glacier-fraction table, the label masks, the run manifest, and MGRS tile
geometry via `freezebase`. `deep-glacier-mapping` owns three live contracts and
`s1bursts` one small one (S-4); the settled ones are listed at the end.

---

## D-1 — `glacier-fractions.parquet` stays yours

`scripts/inventories/derive-glacier-fractions.py` produces it, using that
repository's `datasets.py` and `geo.py`. This catalog consumes it as a
**file**: it publishes it as an asset of the tiles collection and **fails
loudly on a missing tile** rather than computing a fallback.

Columns: `mgrs_code`, `glacier_fraction`, `glacier_fraction_buffered`,
`rgi_version`, `buffer_m`. Pin the inventory version: the fraction is the same
for every year, and recomputing it per run could give one tile different
values in different years.

The perimeter is passed as a tile count, and the output belongs in the store
the catalog publishes:

```bash
pixi run python scripts/inventories/derive-glacier-fractions.py \
    --scope alps --perimeter-tiles 2210 \
    --out s3://s1-bursts/data/glace-alps/tiles/glacier-fractions.parquet
```

Run it from your checkout, and write the prefix in full: it is the catalog's
`GLACE_STORE_ROOT`, which your `.env` does not define.

## D-3 — The labels are yours, and are not a catalog

The masks are MGRS tiles used to train models alongside GLACE, read by tile
code from a known prefix. Nothing needs to discover them, so a STAC catalog
would add nothing but an import between two repositories that should not have
one. `rasterize-labels.py` keeps its own `IGNORE_VALUE` and rasterization
table. The contract is the path:

```
{labels_root}/{source}/LABELS_{SOURCE}_{year}_{tile}.tif
```

`1` is glacier, `0` background, `255` **ignore**: a glacier surveyed in a
different year, which must be excluded from training and evaluation rather
than treated as background. Masks share the composites' MGRS tile and UTM
zone because both sides use `freezebase.mgrs`. Document this in a README
beside the data; nothing validates it.

## D-4 — `manifest.parquet` beside the composites

One row per written file, so the publisher reads one file instead of opening
every composite: a Swiss year has about 1 700 files, an Alpine year about
26 500. `run_composite` writes it at the end, beside its `config.yaml`. This is
driver work, not writer work (M-31): the bytes' own properties are already in
the header and in `ResolutionWeighter.checksums`, while arranging rows in this
catalog's vocabulary and describing a *resumed* run belong to the run loop.

`read_manifest` checks the required columns and `survey_tags` the required
tags, so a manifest that drifts fails loudly at the publisher.
`tests/test_manifest_contract.py` holds the producer's column list verbatim.
One row per file, then one column per GeoTIFF tag in SCREAMING_CASE:

| column | from |
| --- | --- |
| `filename`, `tile`, `asset_key`, `product`, `qa`, `polarization`, `composite_start`, `composite_end` | the filename, parsed as `glace_catalog.stac.naming` does. `asset_key` is the published stem, `coh12_vv_qa_num`; `product` is `COH12`; `qa` is `NUM`, `CQM` or `None` |
| `size` | a listing of the output prefix, taken **last** |
| `sha256` | `ResolutionWeighter.checksums`, already multihash-encoded, keyed by output path, for files this run wrote; otherwise the file is read once and hashed. Never absent, never fabricated |
| `valid_percent` | the QA-NUM raster's own `STATISTICS_VALID_PERCENT`, keyed by the *data* asset it counts for; QA rows carry `None` |
| `band_name`, `band_unit`, `data_type`, `nodata`, `spatial_resolution`, `bits_per_sample`, `byte_order` | the raster header. A NaN in `nodata` **is** the declared nodata: every float composite declares NaN, and parquet has no other way to say "none declared" |
| `stat_minimum`, `stat_maximum`, `stat_mean`, `stat_stddev`, `stat_valid_percent` | the band's own `STATISTICS_*` tags, so the publisher can fill `bands[].statistics` without a second header read |

Parquet metadata: `manifest_version = 2`. Bump it when a column changes
meaning, not when one is added. In version 2, `composite_start` /
`composite_end` are the compositing window; the first and last contributing
acquisitions are the `FIRST_ACQUISITION_DATE` / `LAST_ACQUISITION_DATE` tag
columns.

Three constraints:

- **The driver passes `checksum=True`.** Without it every `sha256` is `None`
  and the published items carry no `file:checksum`.
- **A resumed run must describe the whole year, not just this run.** `_save`
  skips a file that already exists, so its digest is not in memory. Load the
  previous manifest first and carry every row whose filename and size still
  match. Header-read the files this run wrote or that have no row, and **flush
  after every geobox** as a checkpoint that may fail without ending the run. A
  file with neither a previous row nor an in-memory digest is read once and
  hashed, so a resume costs at most one tile's worth of full reads.
- **A cache, not the source of truth.** Every value in the manifest is also in
  the file, or is the digest of its bytes; a published COG must describe
  itself. To give an archive a manifest, rerun its year: the run skips every
  complete tile and describes and hashes every file.

*Verified when* `publish-year.py --steps copy` on an Alpine year reports zero
mismatches and the `file:checksum` of a published item verifies under
`rashid check --data`.

---

## S-4 — The band description is the asset key (`s1bursts`)

**Status: open.** Every raster in the catalog has one band, and the catalog
names that band by the file's asset key: `coh12_vv`, `coh12_vv_qa_num`,
`rtc_vh_qa_cqm`. The collections' `item_assets`, the items and the mosaic COGs
use these names. The tile COGs say `VV`, `num_obs` or `cqm`, the GDAL band
description the writer sets today, so a tile file names its band differently
from the item that describes it.

- **What to change.** Set the band description of every composite and QA
  raster to the published asset key, `{product}_{pol}` plus `_qa_num` /
  `_qa_cqm`, lower case, as `glace_catalog.stac.naming.CompositeFile.asset_key`
  derives it from the filename. The `LAYER_DESCRIPTION` and `QA_LAYER` tags
  stay as they are: they are prose, while the band description is an
  identifier.
- **Why.** One name per band across the file, the item and the collection, so
  that `rasterio` and a STAC client agree on what they opened.

*Verified when* a regenerated year's `coh12_vv.tif` opens in rasterio with
`descriptions == ("coh12_vv",)`, and `publish-year.py` publishes it without
the item builder overriding the name.

---

## Settled

| what | outcome |
| --- | --- |
| **S-1** embedded band statistics (`PTL-DAT-009/010`) | `s1bursts.product_tags.band_statistics` writes them at creation, inside the `MemoryFile` the COG is copied from, so the tags reach the header block. s1bursts 0.5.0, 2026-09-07 |
| **S-2** `sha256` at write time | `freezebase.write_cog(checksum=True)` hashes the encoded bytes it uploads and returns the multihash digest; `ResolutionWeighter.checksums` carries them. freezebase 0.7.0, 2026-09-04 |
| **S-3** who writes the manifest | The composite driver, not the writer (M-31); see D-4. 2026-09-07 |
| statistics for old composites | The Swiss 2021–2024 archives were **regenerated** with s1bursts 0.5.0 rather than retrofitted, because 0.4.0 had already changed median aggregation and QA-CQM. Verified 2026-09-08 |
| the pass-direction tag | `PASS_DIRECTION` (CEOS-ARD PFS SAR wording), published as `glace:pass_direction`. 2026-09-04 |
| **F-2** `subprocess_s3_env` | In `freezebase/s3.py`, beside `s3_env`. 2026-09-04 |
| **F-3** the VRT helpers | `freezebase/vrt.py`, released in freezebase 0.7.0 on 2026-09-04 and used by the mosaic chain |
| **F-4** releases instead of editable overrides | freezebase 0.7.0 and s1bursts 0.5.0 are on PyPI; this repository has no `[tool.uv.sources]`. 2026-09-08 |
| **F-5** `S3Settings`, `list_object_sizes`, `atomic_write_text`, `content_type_for`, `file_sha256(UPath)`, the profile-configured endpoint in `s3_env` | Released in freezebase 0.7.1 on 2026-09-08; this repository requires `freezebase[s3]>=0.7.1,<0.8` |
| **D-2** the pipeline copied out of `deep-glacier-mapping` | Lives only in `deep-glacier-mapping`; `rasterize-labels.py`, `derive-glacier-fractions.py` and `export-inventories.py` are under its `scripts/inventories/`. 2026-09-05 |

s1bursts is not a dependency of this repository; it writes the files this
repository reads. freezebase is a dependency, installed from PyPI.
