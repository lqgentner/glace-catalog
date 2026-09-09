# GLACE tiles – Sentinel-1 Composites on MGRS squares (10 m)

One item per (MGRS square, year), each carrying twelve single-band
Cloud-Optimized GeoTIFFs on the 10 km MGRS grid at 10 m grid spacing, in that
square's own UTM zone. Agents: read [`AGENTS.md`](AGENTS.md).

## The twelve assets

| asset key | title | dtype | nodata |
| --- | --- | --- | --- |
| `coh12_vv` | Coherence VV | float32 | NaN |
| `coh12_vv_qa_num` | Coherence VV – Contributing Observations | uint16 | 0 |
| `coh12_vv_qa_cqm` | Coherence VV – Composite Quality Map | float32 | NaN |
| `coh12_vh` | Coherence VH | float32 | NaN |
| `coh12_vh_qa_num` | Coherence VH – Contributing Observations | uint16 | 0 |
| `coh12_vh_qa_cqm` | Coherence VH – Composite Quality Map | float32 | NaN |
| `rtc_vv` | Backscatter VV | float32 | NaN |
| `rtc_vv_qa_num` | Backscatter VV – Contributing Observations | uint16 | 0 |
| `rtc_vv_qa_cqm` | Backscatter VV – Composite Quality Map | float32 | NaN |
| `rtc_vh` | Backscatter VH | float32 | NaN |
| `rtc_vh_qa_num` | Backscatter VH – Contributing Observations | uint16 | 0 |
| `rtc_vh_qa_cqm` | Backscatter VH – Composite Quality Map | float32 | NaN |

The asset key is the filename stem, so nothing in the store has to be parsed to
be understood: `coh12_vv` is `coh12_vv.tif` inside that item's directory. Dates
are not in filenames; they are in the item properties and the GeoTIFF tags.

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

`*_qa_cqm` is a linear ratio where 1.0 is flat terrain and higher is better
resolved; it is drawn in decibels over a fixed, measured range.

## Caveats

- **Coverage follows the glacier perimeter, not an administrative boundary.**
  Squares are composited where there is ice to observe, so the footprint is
  ragged and the gaps between glacierized regions are not data gaps.
- **Glacier fractions are pinned, not recomputed.** `glace:glacier_fraction` and
  `glace:glacier_fraction_buffered` come from `glacier-fractions.parquet`,
  computed once against one RGI7 version, so the same square cannot carry
  different values in different years.
- **QA-NUM's zero is nodata**, not "zero observations". A pixel with no
  contributing acquisition is absent, not counted.

## License

**CC-BY-4.0.** Contains modified Copernicus Sentinel data.

## Provenance

Produced at the University of Zurich, Department of Geography, from Copernicus
Sentinel-1 SLC data. Per-file processing provenance is embedded in each
GeoTIFF's tags and mirrored into the item properties.
