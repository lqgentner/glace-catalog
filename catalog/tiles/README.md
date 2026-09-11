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

The asset key is the filename stem. Dates are in the item properties and the GeoTIFF tags.

## Reading the data

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

## Units

`rtc_*` is **linear power, not decibels**. Convert with `10*log10(x)`, which
the collection's `renders` block declares.

`*_qa_cqm` is a linear ratio. It is a weighted average of the observations' local resolution
and does not carry a unit. For visualization, convert to decibels with `10*log10(x)`.

## License

**CC-BY-4.0.** Contains modified Copernicus Sentinel data.
