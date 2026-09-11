# GLACE tiles – agent guide

One item per (MGRS square, year). Twelve single-band COGs per item,
10 m, in each square's own UTM zone.

## Query the mirror, do not walk the tree

`items.parquet` holds every item of every year. Walking `{year}/{tile}/item.json`
is one HTTP request per tile and there are hundreds per year.

```sql
INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;

-- glacier-heavy tiles in one year, ready to feed a sampler
SELECT id, "glace:glacier_fraction_buffered" AS frac,
       "proj:code", bbox
FROM read_parquet('https://data.source.coop/lqgentner/glace-ch/tiles/items.parquet')
WHERE datetime BETWEEN '2024-01-01' AND '2024-12-31'
  AND "glace:glacier_fraction_buffered" > 0.1
ORDER BY frac DESC;
```

## Asset vocabulary

Twelve keys, and the key is the filename stem. Polarization comes before the
QA role, so a sorted listing keeps each QA layer directly beneath the
measurement it describes.

| asset key | title | stored as | nodata | to display |
| --- | --- | --- | --- | --- |
| `coh12_vv` | Coherence VV | float32 | NaN | 0.1 to 0.75, `cmc.lipari` |
| `coh12_vv_qa_num` | Coherence VV – Contributing Observations | uint16 | 0 | 0 to 90, `cmc.glasgow` |
| `coh12_vv_qa_cqm` | Coherence VV – Composite Quality Map | float32 | NaN | `10*log10(x)`, then -3 to 10 dB, `cmc.glasgow` |
| `coh12_vh` | Coherence VH | float32 | NaN | 0.1 to 0.55, `cmc.lipari` |
| `coh12_vh_qa_num` | Coherence VH – Contributing Observations | uint16 | 0 | 0 to 90, `cmc.glasgow` |
| `coh12_vh_qa_cqm` | Coherence VH – Composite Quality Map | float32 | NaN | `10*log10(x)`, then -3 to 10 dB, `cmc.glasgow` |
| `rtc_vv` | Backscatter VV | float32 | NaN | `10*log10(x)`, then -16.5 to -4 dB, `cmc.grayC` |
| `rtc_vv_qa_num` | Backscatter VV – Contributing Observations | uint16 | 0 | 0 to 90, `cmc.glasgow` |
| `rtc_vv_qa_cqm` | Backscatter VV – Composite Quality Map | float32 | NaN | `10*log10(x)`, then -4 to 8 dB, `cmc.glasgow` |
| `rtc_vh` | Backscatter VH | float32 | NaN | `10*log10(x)`, then -23.5 to -11 dB, `cmc.grayC` |
| `rtc_vh_qa_num` | Backscatter VH – Contributing Observations | uint16 | 0 | 0 to 90, `cmc.glasgow` |
| `rtc_vh_qa_cqm` | Backscatter VH – Composite Quality Map | float32 | NaN | `10*log10(x)`, then -4 to 8 dB, `cmc.glasgow` |

Averaging `rtc_*` or `*_qa_cqm` must happen in the **linear** domain. A mean of
decibels is not the decibel of a mean. The `renders` block on the collection
carries the same stretches with portable matplotlib colormap names.

## Provenance is in the file

Each GeoTIFF carries the dataset tags recording the processing chain
(`PRODUCT_TYPE`, `PIPELINE_SOFTWARE*`, `COMPOSITING_*`, `BACKSCATTER_*`,
`COHERENCE_*`). The item properties mirror them under `glace:*` and
`processing:*`, so you can filter on them without opening a raster, but the
file is the source of truth and stays self-describing if separated from this
catalog.

Every tile item of a year carries the same `start_datetime` and `end_datetime`:
the compositing window, chosen once per year from the lowest-snow period. Which
acquisitions actually contributed varies per tile and product and is on the item
as `glace:coh_first_acquisition` / `glace:coh_last_acquisition` and the `rtc`
pair; coherence ends about twelve days before backscatter because a pair needs
both dates inside the window.

## Pairing with the mosaics

The mosaics collection holds the same measurements resampled onto one fixed
equal-area grid, one item per year. Join on the year. Do not join on geometry:
the tiles are per-UTM-zone and the mosaics are ETRS89-LAEA.

## Gotchas

- **QA-NUM's nodata value is 0**, which coincides with zero contributing
  observations.
- **NaN is nodata** on every float32 band. Some browser GeoTIFF decoders drop
  LERC's validity mask and hand a NaN through as `0`; that is a client defect,
  not a property of these files.
- Tiles are LERC_ZSTD compressed with a per-quantity error bound anchored to
  that quantity's own measurement noise.
- Coverage is the Alpine Convention perimeter; a square outside it has no item.
