# What the other repositories owe this one

**Read this if you are working in `s1bursts`, `freezebase` or
`deep-glacier-mapping` and someone pointed you at the GLACE catalog.** It is the
whole cross-repository contract, per repository, with what to change, why, and
how to know it worked. You should not need to read
[`MIGRATION.md`](MIGRATION.md) to do your part; it is the design record, this is
the work order.

Living document, like the rest of `docs/`. Update it in the same commit as the
change it describes.

**The one thing to understand first:** this catalog conforms to
[Portolan](https://www.portolan-sdi.org/), and conformance is defined by passing
[`rashid`](https://github.com/portolan-sdi/rashid), not by reading the spec. Two
of its rules are error-severity and cannot be satisfied from inside this
repository, because they are properties of bytes written elsewhere. Those are
items **S-1** and **S-2** below. Both are now written; what remains is an
s1bursts release and the restat pass over the composites that predate them.

---

## `s1bursts` — the composite writer

Two items, both in `resolution_weighter.py`, both cheap while the pixel array
is still in memory and expensive afterwards. They landed in **one commit**,
because each one alone forces a rewrite of the archive. S-3 is recorded below
only to say where it went.

### S-1 — Embedded band statistics · **blocking**

> `PTL-DAT-009` (ERROR): every COG band carries embedded min/max/mean/stddev.
> `PTL-DAT-010` (ERROR): a band with a nodata value declares its valid percent.

Portolan raises these to a MUST so that a renderer can scale any data type
without reading pixels. The statistics **must be embedded in the file** — a PAM
`.aux.xml` sidecar does not satisfy it — and **must be written at creation**, so
they land in the leading header block and arrive in a reader's first range
request.

`freezebase.raster.write_cog` already takes `band_tags`, applied inside the
staging `MemoryFile` before the COG copy, which is the only point at which they
reach `GDAL_METADATA` in the header. So this is one argument at the call site:

```python
finite = np.isfinite(arr) & (arr != nodata)   # uint16 QA-NUM: arr != 0
write_cog(
    arr, dst, profile,
    band_tags=[{
        "STATISTICS_MINIMUM": repr(float(arr[finite].min())),
        "STATISTICS_MAXIMUM": repr(float(arr[finite].max())),
        "STATISTICS_MEAN":    repr(float(arr[finite].mean())),
        "STATISTICS_STDDEV":  repr(float(arr[finite].std())),
        "STATISTICS_VALID_PERCENT": repr(100.0 * finite.sum() / finite.size),
    }],
)
```

- **Exact, not estimated.** The whole array is in hand, so do *not* set
  `STATISTICS_APPROXIMATE`; GDAL requires that flag only for sampled values.
- A pixel is valid when finite **and** unequal to `nodata`, so a stray NaN beside
  a finite nodata marker like QA-NUM's `0` cannot poison the result.
- Verify once on a written file: `rasterio.open(f).tags(1)` returns all five, and
  a 16 kB range read of the head contains `GDAL_METADATA`. If GDAL places the tag
  after the image data, the COG profile needs revisiting before 26 500 files are
  written with it.

**State:** done. `s1bursts.product_tags.band_statistics`, merged into
`ResolutionWeighter._save`; committed 2026-09-04 as `fd928ef` on
`feat/checksum-band-stats`, together with S-2. Needs an s1bursts release.

**It also settles `valid_percent`.** `_save` writes QA-NUM as `uint16` with
nodata `0`, so on that band "finite and unequal to nodata" *is* `count_nonzero`
— the QA-NUM file's own `STATISTICS_VALID_PERCENT` is the number
`valid_percent_from_qa` computes, and the publisher can take it from the header
instead of reading every QA-NUM raster's pixels.

The source stays the QA-NUM band either way, which is what
`build-manifest.py` already does — it reads `valid_percent_from_qa` only for
`file.qa == "NUM"` and keys it by the data asset. S-1 changes where that number
is read from, not which band it describes.

Worth stating for whoever writes D-4, because at write time the data band's own
`STATISTICS_VALID_PERCENT` is right there and looks like the same number. It is
not: it counts finite pixels of the *composite*. With `decibel=True`,
`_to_decibel` turns every non-positive aggregate into NaN while QA-NUM still
counts the observations that produced it, so filling `valid_percent` from the
data band would quietly publish a smaller number than the archive's convention.

### S-2 — `sha256` at write time · **blocking**

Every published asset carries `file:checksum`, which Portolan requires to be
**multihash-encoded** — `1220` followed by the sha256 hex, not a bare digest and
not a `sha256:` prefix. `rashid check --data` reads the bytes back and verifies
it, so a wrong value is a conformance failure and an absent one is merely a
missing SHOULD.

It has to happen at write time. `write_cog` hands encoded bytes straight to GDAL,
which streams them to `/vsis3/`, so they never exist anywhere hashable
afterwards; computing it later means reading every object of a year back.

**State:** done. `freezebase.write_cog` gained `checksum: bool = False`,
released in freezebase 0.7.0; `ResolutionWeighter` gained the matching
constructor argument that fills `ResolutionWeighter.checksums`, keyed by output
path, committed 2026-09-04 as `fd928ef` with S-1. s1bursts now floors
`freezebase[s3]>=0.7,<0.8` with no path override. Needs an s1bursts release.

**Changed since that was written:** the manifest no longer carries an ETag, only
`sha256`. That **removes a constraint** rather than adding one — the old design
required the upload to stay single-part so the ETag stayed an MD5, and nothing
now depends on that. Upload however is convenient.

### S-3 — `manifest.parquet` · **settled 2026-09-07: not here**

The run manifest is emitted by the composite driver in `deep-glacier-mapping`,
not by `s1bursts`. See D-4 under `deep-glacier-mapping`
for the work order. Nothing is owed here beyond the release.

Why it is not writer code: everything the manifest needs that is a property of
the bytes already lands in the right place — the statistics and provenance tags
in the COG header (S-1), the digest in `ResolutionWeighter.checksums` (S-2).
What remains is arranging rows in this catalog's vocabulary — `tile`,
`asset_key`, `product`, `qa`, parsed from the filename convention — which a
general-purpose SAR package has no second use for. And the one hard case, a
resumed run whose `_save` skipped files a previous run wrote, is a property of
the run loop, which the driver owns and the writer cannot see.

**Optional, later:** a generic per-file ledger on `ResolutionWeighter` — path
to size, digest, tags and band tags — would let the driver skip the header
reads. It carries no catalog schema, so it would be legitimate here, but it is
not on the release path.

### The pass-direction tag · **fixed here 2026-09-04**

Nothing owed upstream — recorded because the mismatch was live and silent. This
repository required `ORBIT_DIRECTION` on every pass composite
(`_REQUIRED_PASS_TAGS`) and published it as `glace:orbit_direction`. `s1bursts`
renamed that tag to `PASS_DIRECTION` to match the CEOS-ARD PFS SAR wording, so
every pass composite would have come out of `survey_tags` missing a required
tag. The values changed with it, `ASCENDING`/`DESCENDING` to
`Ascending`/`Descending`; nothing here reads them, because the direction is
taken from the `COMP_A`/`COMP_D` filename token instead.

Fixed in `stac/tags.py`: the required tag is `PASS_DIRECTION` and the published
field is `glace:pass_direction`. Not visible in the LRW assets published today —
the twelve `ASSET_KEYS` are all LRW — which is exactly why it would have gone
unnoticed until the first pass composite was published.

### The retrofit that S-1 forces

The completed Swiss 2021–2024 composites have **empty band tags** — verified. So
before anything publishes they need a `restat` pass, roughly 2 GB per year.

**Order matters: restat first, then rebuild the manifest.** A rewrite changes
both the sizes and the digests the manifest records.

---

## `freezebase` — the IO layer

### F-1 — `write_cog(checksum=True)`

See S-2. Encode into a second `MemoryFile`, hash those exact bytes, upload them,
return the multihash digest. **State: done 2026-09-04**, released in freezebase
0.7.0 and consumed by s1bursts.

### F-2 — `subprocess_s3_env` · **done 2026-09-04**

Moved here from `glace_catalog.gdal_s3`, which is now deleted. It is the
child-process counterpart to `s3_env`: same concern, same storage options, but
written as environment variables because a subprocess inherits none of a
`rasterio.Env`. It lives beside `s3_env` in `freezebase/s3.py` and shares its
endpoint handling.

Nothing about it was GDAL-specific — the module that held it also carried
`to_gdal_uri` and `gdal_s3_env`, both of which imported `osgeo` and neither of
which had a caller. See [Simplifications](#simplifications-taken).

**Needs a release** before this repository can drop its editable override.

### F-3 — The VRT helpers

`create_warped_vrt`, `build_vrt_mosaic(bounds=…)`, `create_decibel_vrt`,
`create_rgb_vrt`, `create_rgba_vrt`. **State: done**, in `freezebase/vrt.py`, and
in use.

### F-4 — Cut a release · **blocks the Alps**

An editable `[tool.uv.sources]` path is not reproducible from a checkout. Before
the Alps backfill, F-1 through F-3 need a release and this repository needs a
version floor instead of a path override. Same for `s1bursts`.

---

## `deep-glacier-mapping` — composite production and inventories

### D-1 — `glacier-fractions.parquet` stays yours

`scripts/inventories/derive-glacier-fractions.py` produces it and depends on
`datasets.py` and `geo.py`, which did not move. This catalog consumes it as a
file, not an import: it publishes it as a collection asset of the tiles
collection and **fails loudly on a missing tile** rather than computing a
fallback.

Columns: `mgrs_code`, `glacier_fraction`, `glacier_fraction_buffered`,
`rgi_version`, `buffer_m`. Pin the inventory version — the fraction is
year-independent, and recomputing it per run could silently give one tile
different values in 2021 and 2025.

### D-2 — Delete the copied pipeline once the port is verified

`scopes.py`, `stac/`, `mosaics.py`, `webmap.py`, `gdal_s3.py` and
`scripts/stac/` were **copied**, not moved, so that something kept working while
the port landed. The port is done; delete them once a real year has been
published from here. Tracked as P-17.

**Done 2026-09-05.** The copies are gone. Three scripts stayed because they need
that repository's dataset classes, not this one's pipeline —
`rasterize-labels.py`, `derive-glacier-fractions.py` and
`export-inventories.py` — and moved to `scripts/inventories/`, since all three
read glacier inventories and none touches STAC.

`layers.json` production stopped with them, which is intended: M-26 retires the
manifest, and `glace-viewer` has its migration recorded in its own `AGENTS.md`.

### D-3 — The labels are yours, and are not a catalog

**Settled 2026-09-05, reversing "belongs here eventually".** The masks are MGRS
tiles used to train models alongside GLACE, read by tile code from a known
prefix. Nothing discovers them, so a STAC catalog bought nothing and cost an
import between two repositories that should not have one.

`build-labels-catalog.py` is deleted and the label machinery is gone from here.
`rasterize-labels.py` keeps its own `IGNORE_VALUE` and rasterization table;
everything the old `LabelSource` carried beyond that described a collection that
no longer exists.

**The contract is on disk, not in an import**, the same way
`glacier-fractions.parquet` is:

```
{labels_root}/{source}/LABELS_{SOURCE}_{year}_{tile}.tif
```

`1` glacier, `0` background, `255` *ignore* — a glacier surveyed in a different
year, which must be excluded from training and evaluation rather than treated as
background. Masks land on the same MGRS tile and UTM zone as the composites
because both sides ask `freezebase.mgrs`, which is where a grid contract
belongs. Write this down in a README beside the data; nothing validates it now.

### D-4 — Emit `manifest.parquet` beside the composites · **implemented 2026-09-07, unrun at scale**

One row per written file, so the publisher reads one file instead of opening
every composite: 1 716 header reads on a Swiss year is seconds, 26 500 on an
Alpine year is not a path. Settled 2026-09-07 as driver work, not writer work —
see S-3 for why. It goes at the end of `run_composite`, where `config.yaml` is
already written, and moves to the pipeline repository with it.

**The contract is on disk**, like `glacier-fractions.parquet` and the label
masks: no import between the two repositories in either direction (M-30). This
repository's `read_manifest` checks the required columns and `survey_tags`
checks the required tags, so a manifest that drifts fails loudly at the
publisher rather than silently at the reader.

Columns, one row per file:

| column | from |
| --- | --- |
| `filename`, `tile`, `asset_key`, `product`, `qa`, `polarization`, `composite_start`, `composite_end` | the filename, by the same parse `glace_catalog.stac.naming` does. `asset_key` is the published stem, `coh12_vv_qa_num`; `product` is `COH12`; `qa` is `NUM`, `CQM` or `None` |
| `size` | a listing of the output prefix, taken **last** |
| `sha256` | `ResolutionWeighter.checksums`, already multihash-encoded, keyed by output path, for files this run wrote; the file read once and hashed otherwise. Never absent, never fabricated |
| `valid_percent` | the QA-NUM raster's own `STATISTICS_VALID_PERCENT`, keyed by the *data* asset it counts for; QA rows carry `None` (see S-1) |
| `band_name`, `band_unit`, `data_type`, `nodata`, `spatial_resolution`, `bits_per_sample`, `byte_order` | the raster header. A NaN in `nodata` **is** the declared nodata — every float composite declares NaN, and parquet cannot say "none declared" any other way |
| `stat_minimum`, `stat_maximum`, `stat_mean`, `stat_stddev`, `stat_valid_percent` | the band's own `STATISTICS_*` tags (S-1), so the publisher can fill `bands[].statistics` without a second header read |
| one column per GeoTIFF tag, SCREAMING_CASE | the raster header, every tag it carries. Do not curate the list: the publisher picks the ones it knows and ignores the rest, so an extra column costs nothing and a missing one is caught |

Parquet metadata: `manifest_version = 1`. Bump it when a column changes
meaning, not when one is added.

Three constraints:

- **The driver passes `checksum=True`.** `run_composite` constructs
  `ResolutionWeighter(fm, rasterio_profile=…)` today with no checksum argument,
  so nothing records a digest. Without it every `sha256` is `None` and the
  Alps publish no `file:checksum`.
- **A resumed run must describe the whole year, not this run.** `_save` skips
  a file that already exists, so its digest is not in memory. Load the previous
  manifest at the start, carry every row whose filename and size still match,
  header-read the files this run wrote or that carried no row, and **flush after
  every geobox** as a checkpoint that may fail without ending the run. A file
  with no previous row and no in-memory digest — the tile in flight when the
  previous process died — is read once and hashed, so a resume costs at most
  one tile's worth of full reads and every row ends up with a digest.
- **A cache, not the source of truth.** Every value the manifest lists is also
  in the file; a published COG must be self-describing. `scripts/build-manifest.py`
  here rebuilds one from the archive and is the retrofit for years that predate
  emission; with `--out` it is also the check that the run's manifest agrees
  with the bytes.

**State:** `glacier_mapping/manifest.py` and the wiring in `run_composite`,
with `tests/test_manifest.py` writing real COGs through `freezebase.write_cog`
to cover the carry, the re-read, the reconcile and the per-pass result shapes.
On this side, `manifest_raster_info` now consumes `sha256` and the `stat_*`
columns — it silently dropped both before, so no manifest could ever have put a
`file:checksum` on an item — and `tests/test_manifest_contract.py` holds the
producer's column list verbatim and round-trips a manifest in that layout.
`build-manifest.py` writes the same columns, so a retrofitted Swiss manifest
and an emitted Alpine one look alike.

Verified when: `publish-year.py --steps copy` on an Alpine year runs with no
`build-manifest.py` beforehand, reports zero mismatches, and the
`file:checksum` of a published item verifies under `rashid check --data`.

---

## Simplifications taken

Recorded so the next person does not re-derive them.

| what | why |
| --- | --- |
| **`gdal_s3.py` deleted** | Of its three functions only `subprocess_s3_env` had a caller, and it has no GDAL in it. `to_gdal_uri` and `gdal_s3_env` were dead code that imported `osgeo` — a dependency this repository does not declare and never installed, so `gdal_s3_env` could not have run. Moved the one live function to `freezebase`, dropped the rest. |
| **`layers.json` retired, in design and in code (M-26)** | M-9 moved raster *styling* to `renders`; retiring the viewer manifest was a separate decision, and its writer is now deleted. `glace-viewer` reads the published file until it migrates; see its `AGENTS.md`. |
| **The inventory overlay export moved out** | `INVENTORY_OVERLAYS`, `export_inventory`, `export_tile_grid` and `build_vector_pmtiles` were dead here — nothing in this repository called them, and `deep-glacier-mapping`'s inventory export was their only consumer. They live there now, which is also what M-30 required. |
| **ETag dropped from the manifest** | It was never writer code — it is read from a listing — it was a writer *constraint*: keep the upload single-part or it stops being an MD5. `sha256` is what gets published, so the ETag bought only a listing-based check. |
| **`build_tiles_collection` / `build_mosaics_collection` deleted** | The collection documents are hand-written source now, patched at publication rather than regenerated. Their prose was in Python string literals; it is in reviewable JSON instead. |
| **The RGBA WEBP display COGs dropped** | PMTiles serve that view at a quarter of the bytes. Removing them also took every decibel pixel out of the `data` role. |

### Still worth doing

- **`mosaics.py` imports both `xml.etree` and `defusedxml`.** Only the defused
  parser is used for reading; the `ET` import remains for element construction,
  which is fine, but the two-import shape invites someone to use the wrong one.
- **`rio-pmtiles` is declared nowhere.** It is in neither `pyproject.toml` nor
  `uv.lock`, and `webmap.py` resolves the `rio` CLI at runtime, so `--steps
  pmtiles` cannot run from a clean `uv sync`. Now that M-25 makes PMTiles the
  only display product, this is on the critical path rather than a nicety.
- **`RasterInfo.statistics` is a bare dict.** Once S-1 lands and the values are
  guaranteed, it could carry the five fields explicitly so a missing one is a
  type error rather than a `KeyError` at publication.
