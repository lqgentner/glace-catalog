# GLACE tiles — 10 m Sentinel-1 LRW composites on the MGRS grid

One item per (MGRS tile, year), each carrying twelve single-band
Cloud-Optimized GeoTIFFs on the 10 km MGRS grid at 10 m resolution.

## The twelve assets

| asset key | what it holds | dtype | nodata |
| --- | --- | --- | --- |
| `coh12_vv`, `coh12_vh` | 12-day repeat-pass coherence, dimensionless [0, 1] | float32 | NaN |
| `rtc_vv`, `rtc_vh` | terrain-corrected gamma-nought backscatter, **linear power** | float32 | NaN |
| `{measurement}_qa_num` | contributing observation count | uint16 | 0 |
| `{measurement}_qa_cqm` | composite quality metric, a linear contributing-area ratio | float32 | NaN |

The asset key is the filename stem, so nothing in the store has to be parsed to
be understood: `coh12_vv` is `coh12_vv.tif` inside that item's directory. Dates
are not in filenames — they are in the item properties and the GeoTIFF tags.

## Reading it

Query the item mirror rather than walking the tree. `items.parquet` holds every
item of every year, so one range request replaces one HTTP fetch per tile:

```python
import duckdb

duckdb.sql("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;")
duckdb.sql("""
    SELECT id, datetime, "glace:glacier_fraction"
    FROM read_parquet('https://data.source.coop/lqgentner/glace-ch/tiles/items.parquet')
    WHERE "glace:glacier_fraction" > 0.2
    ORDER BY "glace:glacier_fraction" DESC
""").show()
```

The item JSON under each year sub-catalog stays the normative representation;
the mirror is a derived copy of it.

## Units, and the one thing that trips people up

`rtc_*` is **linear power, not decibels**. Converting is `10*log10(x)`, which
the collection's `renders` block declares. Averaging linear power is physically
meaningful; averaging decibels is not, which is why the stored pixels are
linear and the conversion happens at draw time.

`*_qa_cqm` is a ratio where 1.0 is flat terrain. Below 1 is layover, above 1 is
shadow, so a **diverging** ramp centred on 0 dB is what separates the two; a
sequential ramp hides the sign.

## Caveats

- **Glacier fractions are pinned, not recomputed.** `glace:glacier_fraction` and
  `glace:glacier_fraction_buffered` come from `glacier-fractions.parquet`,
  computed once against one RGI7 version, so the same tile cannot carry
  different values in different years.
- **Coverage is the perimeter, not the country.** The Swiss run composites
  glacier tiles only — the buffered SGI2023 outline, 143 tiles — so there is no
  data over the Mittelland.
- **QA-NUM's zero is nodata**, not "zero observations". A pixel with no
  contributing acquisition is absent, not counted.

## License

**CC-BY-4.0.** Contains modified Copernicus Sentinel data.

## Provenance

Produced at the University of Zurich, Department of Geography, from Copernicus
Sentinel-1 SLC data. Per-file processing provenance is embedded in each
GeoTIFF's tags and mirrored into the item properties.
