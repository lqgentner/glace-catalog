# What the other repositories owe this one

**Read this if you are working in `s1bursts`, `freezebase` or
`deep-glacier-mapping` and someone pointed you at the GLACE catalog.** It is the
whole cross-repository contract, with what to change, why, and how to know it
worked. You should not need to read [`DESIGN.md`](DESIGN.md) to do your part;
that is the design record, this is the work order.

**The one thing to understand first:** this catalog conforms to
[Portolan](https://www.portolan-sdi.org/), and conformance is defined by passing
[`rashid`](https://github.com/portolan-sdi/rashid), not by reading the spec.
Several of its error-severity rules are properties of bytes written elsewhere and
cannot be satisfied from inside this repository.

**No repository here imports another** (M-30). What they share is on disk: the
glacier-fraction table, the label masks, the run manifest, and MGRS tile geometry
via `freezebase`. Three contracts are live, all owned by `deep-glacier-mapping`;
everything else is settled and listed at the end.

---

## D-1 — `glacier-fractions.parquet` stays yours

`scripts/inventories/derive-glacier-fractions.py` produces it and depends on that
repository's `datasets.py` and `geo.py`. This catalog consumes it as a **file**:
it publishes it as an asset of the tiles collection and **fails loudly on a
missing tile** rather than computing a fallback.

Columns: `mgrs_code`, `glacier_fraction`, `glacier_fraction_buffered`,
`rgi_version`, `buffer_m`. Pin the inventory version: the fraction is
year-independent, and recomputing it per run could silently give one tile
different values in 2021 and 2025.

The perimeter is passed as a tile count rather than imported, and the output
belongs in the store the catalog publishes:

```bash
uv run --env-file .env scripts/inventories/derive-glacier-fractions.py \
    --scope ch --perimeter-tiles 143 \
    --out s3://s1-bursts/data/glace-ch/tiles/glacier-fractions.parquet
```

Run it from your checkout, and write the prefix in full: it is the catalog's
`GLACE_STORE_ROOT`, which your `.env` does not define.

## D-3 — The labels are yours, and are not a catalog

The masks are MGRS tiles used to train models alongside GLACE, read by tile code
from a known prefix. Nothing discovers them, so a STAC catalog bought nothing and
cost an import between two repositories that should not have one. The label
machinery is gone from here; `rasterize-labels.py` keeps its own `IGNORE_VALUE`
and rasterization table. The contract is on disk:

```
{labels_root}/{source}/LABELS_{SOURCE}_{year}_{tile}.tif
```

`1` glacier, `0` background, `255` **ignore** — a glacier surveyed in a different
year, which must be excluded from training and evaluation rather than treated as
background. Masks land on the same MGRS tile and UTM zone as the composites
because both sides ask `freezebase.mgrs`, which is where a grid contract belongs.
Write this down in a README beside the data; nothing validates it.

## D-4 — `manifest.parquet` beside the composites

One row per written file, so the publisher reads one file instead of opening
every composite: 1 716 header reads on a Swiss year is seconds, 26 500 on an
Alpine year is not a path. It is written by `run_composite` at the end, beside
the `config.yaml` it already writes — driver work, not writer work (M-31),
because what is a property of the bytes already lands in the header and in
`ResolutionWeighter.checksums`, while arranging rows in this catalog's vocabulary
and describing a *resumed* run are properties of the run loop.

`read_manifest` checks the required columns here and `survey_tags` checks the
required tags, so a manifest that drifts fails loudly at the publisher rather
than silently at the reader; `tests/test_manifest_contract.py` holds the
producer's column list verbatim. Columns, one row per file, then one column per
GeoTIFF tag in SCREAMING_CASE:

| column | from |
| --- | --- |
| `filename`, `tile`, `asset_key`, `product`, `qa`, `polarization`, `composite_start`, `composite_end` | the filename, by the same parse `glace_catalog.stac.naming` does. `asset_key` is the published stem, `coh12_vv_qa_num`; `product` is `COH12`; `qa` is `NUM`, `CQM` or `None` |
| `size` | a listing of the output prefix, taken **last** |
| `sha256` | `ResolutionWeighter.checksums`, already multihash-encoded, keyed by output path, for files this run wrote; the file read once and hashed otherwise. Never absent, never fabricated |
| `valid_percent` | the QA-NUM raster's own `STATISTICS_VALID_PERCENT`, keyed by the *data* asset it counts for; QA rows carry `None` |
| `band_name`, `band_unit`, `data_type`, `nodata`, `spatial_resolution`, `bits_per_sample`, `byte_order` | the raster header. A NaN in `nodata` **is** the declared nodata — every float composite declares NaN, and parquet cannot say "none declared" any other way |
| `stat_minimum`, `stat_maximum`, `stat_mean`, `stat_stddev`, `stat_valid_percent` | the band's own `STATISTICS_*` tags, so the publisher can fill `bands[].statistics` without a second header read |

Parquet metadata: `manifest_version = 2`. Bump it when a column changes meaning,
not when one is added. Version 2 means: *`composite_start` / `composite_end` are
the compositing window, no longer the first and last contributing acquisition
(those are the `FIRST_ACQUISITION_DATE` / `LAST_ACQUISITION_DATE` tag columns).*

Three constraints:

- **The driver passes `checksum=True`.** Without it every `sha256` is `None` and
  the published items carry no `file:checksum`.
- **A resumed run must describe the whole year, not this run.** `_save` skips a
  file that already exists, so its digest is not in memory. Load the previous
  manifest first, carry every row whose filename and size still match,
  header-read the files this run wrote or that carried no row, and **flush after
  every geobox** as a checkpoint that may fail without ending the run. A file
  with no previous row and no in-memory digest is read once and hashed, so a
  resume costs at most one tile's worth of full reads.
- **A cache, not the source of truth.** Every value the manifest lists is also in
  the file, or is the digest of its bytes; a published COG must be
  self-describing. There is no retrofit tool: an archive without a manifest gets
  one by rerunning its year, which skips every complete tile and describes and
  hashes every file.

*Verified when* `publish-year.py --steps copy` on an Alpine year reports zero
mismatches and the `file:checksum` of a published item verifies under
`rashid check --data`.

---

## Settled

| what | outcome |
| --- | --- |
| **S-1** embedded band statistics (`PTL-DAT-009/010`) | `s1bursts.product_tags.band_statistics`, written at creation inside the `MemoryFile` the COG is copied from, so the tags reach the header block. Released in s1bursts 0.5.0, 2026-09-07 |
| **S-2** `sha256` at write time | `freezebase.write_cog(checksum=True)` hashes the encoded bytes it uploads and returns the multihash digest; `ResolutionWeighter.checksums` carries them. freezebase 0.7.0, 2026-09-04 |
| **S-3** who writes the manifest | The composite driver, not the writer (M-31). Settled 2026-09-07; see D-4 |
| the `restat` retrofit for old composites | Superseded: the Swiss 2021–2024 archives were **regenerated** with s1bursts 0.5.0, because 0.4.0 had already changed median aggregation and QA-CQM. Verified here 2026-09-08 |
| the pass-direction tag | `PASS_DIRECTION` (CEOS-ARD PFS SAR wording), published as `glace:pass_direction`. Fixed here 2026-09-04 |
| **F-2** `subprocess_s3_env` | Moved into `freezebase/s3.py` beside `s3_env`; `glace_catalog.gdal_s3` deleted, 2026-09-04 |
| **F-3** the VRT helpers | `freezebase/vrt.py`, released in freezebase 0.7.0 on 2026-09-04 and in use by the mosaic chain |
| **F-4** cut releases, drop the editable overrides | freezebase 0.7.0 and s1bursts 0.5.0 are on PyPI; this repository floors `freezebase[s3]>=0.7,<0.8` and has no `[tool.uv.sources]`, 2026-09-08 |
| **F-5** `S3Settings`, `list_object_sizes`, `atomic_write_text`, `content_type_for`, `file_sha256(UPath)`, the profile-configured endpoint in `s3_env` | In `freezebase` on the `feat/s3-settings-and-listing` branch, 2026-09-08, unreleased. This repository and `deep-glacier-mapping` consume it editable via `[tool.uv.sources]`; **open** until the release lands and both drop the override |
| **D-2** delete the pipeline copied out of `deep-glacier-mapping` | Done 2026-09-05. `rasterize-labels.py`, `derive-glacier-fractions.py` and `export-inventories.py` stayed there, under `scripts/inventories/` |

s1bursts is not a dependency of this repository; it is the writer whose output
this repository reads. freezebase is a dependency, consumed from PyPI once F-5
is released and from the sibling checkout until then.
