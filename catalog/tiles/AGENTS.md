# GLACE tiles – agent guide

Each item contains one year of data for a 10 km MGRS square:
twelve single-band COGs at 10 m in the square's UTM zone.
Items cover the MGRS squares of the Alpine Convention perimeter.

## Search tiles

Use `items.parquet` to search all years without fetching each item separately.
This DuckDB query selects tiles by year and buffered glacier fraction:

```sql
INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;
SELECT id, "glace:glacier_fraction_buffered" AS frac,
       "proj:code", bbox
FROM read_parquet('https://data.source.coop/giuz/glace-alps/tiles/items.parquet')
WHERE datetime BETWEEN '2025-01-01' AND '2025-12-31'
  AND "glace:glacier_fraction_buffered" > 0.1
ORDER BY frac DESC;
```

## Assets and units

Asset keys are filenames without `.tif`. QA layers use the measurement's key
followed by `_qa_num` or `_qa_cqm`.

| Asset key | Title | Data type | Nodata | Display |
| --- | --- | --- | --- | --- |
| `coh12_vv` | Coherence VV | float32 | NaN | 0.1 to 0.75, `cmc.lipari` |
| `coh12_vv_qa_num` | Coherence VV – Contributing Observations | uint16 | 0 | 0 to 40, `cmc.glasgow` |
| `coh12_vv_qa_cqm` | Coherence VV – Composite Quality Map | float32 | NaN | `10*log10(x)`, then -2.5 to 8 dB, `cmc.glasgow` |
| `coh12_vh` | Coherence VH | float32 | NaN | 0.1 to 0.55, `cmc.lipari` |
| `coh12_vh_qa_num` | Coherence VH – Contributing Observations | uint16 | 0 | 0 to 40, `cmc.glasgow` |
| `coh12_vh_qa_cqm` | Coherence VH – Composite Quality Map | float32 | NaN | `10*log10(x)`, then -2.5 to 8 dB, `cmc.glasgow` |
| `rtc_vv` | Backscatter VV | float32 | NaN | `10*log10(x)`, then -14.5 to -4.5 dB, `cmc.grayC` |
| `rtc_vv_qa_num` | Backscatter VV – Contributing Observations | uint16 | 0 | 0 to 40, `cmc.glasgow` |
| `rtc_vv_qa_cqm` | Backscatter VV – Composite Quality Map | float32 | NaN | `10*log10(x)`, then -2.5 to 7.5 dB, `cmc.glasgow` |
| `rtc_vh` | Backscatter VH | float32 | NaN | `10*log10(x)`, then -21 to -11 dB, `cmc.grayC` |
| `rtc_vh_qa_num` | Backscatter VH – Contributing Observations | uint16 | 0 | 0 to 40, `cmc.glasgow` |
| `rtc_vh_qa_cqm` | Backscatter VH – Composite Quality Map | float32 | NaN | `10*log10(x)`, then -2.5 to 7.5 dB, `cmc.glasgow` |

Average backscatter and composite quality maps in linear units. Apply decibel
conversion only for display. The collection's `renders` block defines the
same display ranges with portable matplotlib colormap names.

## Processing and dates

GeoTIFF tags record the processing chain: `PRODUCT_TYPE`, `PIPELINE_SOFTWARE*`,
`COMPOSITING_*`, `BACKSCATTER_*` and `COHERENCE_*`. Item properties copy these
under `glace:*` and `processing:*` for filtering. The GeoTIFF tags are the
source of truth.

All tile items in a year share `start_datetime` and `end_datetime`, the
compositing window selected for that year's lowest-snow period. Actual
acquisition dates vary by tile and product. Read `glace:coh_first_acquisition`,
`glace:coh_last_acquisition` and the corresponding `rtc` properties. The last
coherence date is about twelve days before the last backscatter date, because
both images of a coherence pair must fall within the window.

## Reading and comparing

- Counts use 0 for nodata, which also means no contributing observations.
- Float32 bands use NaN for nodata. Some browser decoders incorrectly return
  0 when they drop LERC's validity mask.
- Tiles use LERC_ZSTD compression with error bounds based on each quantity's
  measurement noise.
- Pair tiles with mosaics by year. Reproject before comparing pixels: tiles
  use UTM zones; mosaics use ETRS89-LAEA.

See the [README](README.md) for cube and QGIS examples.
