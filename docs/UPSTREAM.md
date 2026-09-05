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

Three items, all in `resolution_weighter.py`, all cheap while the pixel array is
still in memory and expensive afterwards. S-1 and S-2 landed in **one commit**,
because each one alone forces a rewrite of the archive; S-3 does not touch the
pixels and is deferred, see below.

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

Worth stating for whoever writes S-3, because at write time the data band's own
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

### S-3 — Emit `manifest.parquet` beside the composites

One row per written file, so the publisher reads one file instead of 26 500
remote opens. Schema: `glace_catalog.stac.store.MANIFEST_COLUMNS` plus one column
per `glace_catalog.stac.PROVENANCE_TAGS` entry;
`glace_catalog.stac.store.build_manifest_frame` is the reference implementation.

Two constraints:

- **Written last.** Sizes come from a listing of what was actually written.
- **A cache, not the source of truth.** Every tag it lists is also written into
  the file; a published COG must be self-describing.

Add the five `stat_*` columns and `sha256`. Now that S-1 is implemented, the
statistics can be read straight off each COG's own band tags rather than
recomputed.

**Not blocking:** `scripts/build-manifest.py` here retrofits a manifest onto an
archive that predates emission, which is what the Swiss rehearsal uses. It costs
1 716 header reads per Swiss year — seconds. It is **not** viable for the Alps at
26 500 opens per year, which is the whole reason this moves upstream.

**State: deferred 2026-09-04, ownership unsettled.** Unlike S-1 and S-2 this is
not a property of the bytes, so nothing forces it into the writer, and the schema
above is this repository's vocabulary throughout: `tile`, `asset_key`
(`coh12_vv_qa_num`), `product` (`COH12`), `qa` (`NUM`). Emitting it from
`s1bursts` means that package encoding a downstream catalog's naming convention
it has no other use for, and duplicating `MANIFEST_COLUMNS` and
`PROVENANCE_TAGS` across a repository boundary with nothing to keep them in step.

Two questions to settle before writing any of it:

- **Does it belong in `s1bursts` at all**, or in the caller that drives it — the
  GLACE composite run, which already knows the scope, the year and the asset
  keys, and could assemble the manifest from `ResolutionWeighter.checksums` plus
  the metadata `_save` already wrote?
- **What covers a resumed run.** `_save` skips a file that already exists, so a
  writer-side manifest describes only this run's outputs and a resumed run emits
  one the publisher cannot use. Either the writer header-reads the skipped files
  (the retrofit cost, for a subset), or it refuses to write a partial manifest.

Whichever way it goes, the two constraints above stand.

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

`scripts/stac/derive-glacier-fractions.py` produces it and depends on
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

> **Do not delete the copies yet — one of them is still the live producer.**
> The published store's `layers.json` at
> `data.source.coop/lqgentner/glace-ch/layers.json` was generated **2026-09-02**,
> before the port commit, and carries all 56 layers. It is written by *your*
> copy of the writer, which still has a caller; this repository's copy has none.
> `glace-viewer` reads that file at runtime.
>
> So deleting the originals today stops `layers.json` production and the viewer
> degrades to whatever is cached. The safe order is: this repository lands the
> style-generation work (M-26 / M-27) and `glace-viewer` switches to reading the
> catalog, **then** the originals go. Until then both copies exist and only the
> old one runs.

`cache/stac-store-refactor/SUPERSEDED.md` already redirects readers.

### D-3 — The labels catalog is unclaimed

`rasterize-labels.py` and `build-labels-catalog.py` stayed behind. The labels are
STAC creation and belong here eventually, as a third collection or a sibling
catalog. Nothing depends on the decision yet. Tracked as P-15.

---

## Simplifications taken

Recorded so the next person does not re-derive them.

| what | why |
| --- | --- |
| **`gdal_s3.py` deleted** | Of its three functions only `subprocess_s3_env` had a caller, and it has no GDAL in it. `to_gdal_uri` and `gdal_s3_env` were dead code that imported `osgeo` — a dependency this repository does not declare and never installed, so `gdal_s3_env` could not have run. Moved the one live function to `freezebase`, dropped the rest. |
| **`layers.json` retired by design (M-26), not yet by code** | M-9 moved raster *styling* to `renders`; it did not move the viewer manifest, and `build_viewer_manifest` / `write_manifest` / `publish_tiles` are still in `webmap.py` — with no caller **here**. See the warning under D-2 before deleting anything upstream. |
| **ETag dropped from the manifest** | It was never writer code — it is read from a listing — it was a writer *constraint*: keep the upload single-part or it stops being an MD5. `sha256` is what gets published, so the ETag bought only a listing-based check. |
| **`build_tiles_collection` / `build_mosaics_collection` deleted** | The collection documents are hand-written source now, patched at publication rather than regenerated. Their prose was in Python string literals; it is in reviewable JSON instead. |
| **The RGBA WEBP display COGs dropped** | PMTiles serve that view at a quarter of the bytes. Removing them also took every decibel pixel out of the `data` role. |

### Still worth doing

- **`mosaics.py` imports both `xml.etree` and `defusedxml`.** Only the defused
  parser is used for reading; the `ET` import remains for element construction,
  which is fine, but the two-import shape invites someone to use the wrong one.
- **`webmap.py` is 1 100 lines** covering LUTs, stretch estimation, the tiler
  subprocess and inventory export. `export_inventory` in particular has nothing
  to do with web-map tiles and is the natural first split. Do **not** split on
  `build_viewer_manifest` / `write_manifest` / `publish_tiles`: they look dead
  here but are the interface `glace-viewer` still consumes — see D-2.

- **`rio-pmtiles` is declared nowhere.** It is in neither `pyproject.toml` nor
  `uv.lock`, and `webmap.py` resolves the `rio` CLI at runtime, so `--steps
  pmtiles` cannot run from a clean `uv sync`. Now that M-25 makes PMTiles the
  only display product, this is on the critical path rather than a nicety.
- **`RasterInfo.statistics` is a bare dict.** Once S-1 lands and the values are
  guaranteed, it could carry the five fields explicitly so a missing one is a
  type error rather than a `KeyError` at publication.
