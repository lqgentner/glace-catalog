# GLACE tiles — agent guide

One item per (MGRS tile, year). Twelve single-band COGs per item, 10 m, in each
tile's own UTM zone.

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

Twelve keys, and the key is the filename stem:

```
coh12_vv        coh12_vh          measurements, float32, NaN nodata
rtc_vv          rtc_vh
coh12_vv_qa_num coh12_vv_qa_cqm   QA for each measurement
coh12_vh_qa_num coh12_vh_qa_cqm
rtc_vv_qa_num   rtc_vv_qa_cqm
rtc_vh_qa_num   rtc_vh_qa_cqm
```

Polarization comes before the QA role, so a sorted listing keeps each QA layer
directly beneath the measurement it describes.

## Units

| asset | stored as | to display |
| --- | --- | --- |
| `coh12_*` | dimensionless [0, 1] | rescale 0.10–0.80 (VV) / 0.10–0.60 (VH) |
| `rtc_*` | **linear power** | `10*log10(x)`, then −18.5…−5 dB (VV) / −26…−11 dB (VH) |
| `*_qa_cqm` | linear ratio, 1.0 = flat | `10*log10(x)`, diverging ramp ±6 dB |
| `*_qa_num` | uint16 count, 0 = nodata | 0–30 sequential |

Averaging `rtc_*` or `*_qa_cqm` must happen in the **linear** domain. A mean of
decibels is not the decibel of a mean.

## Provenance is in the file

Each GeoTIFF carries ~30 dataset tags recording the processing chain
(`PRODUCT_TYPE`, `PIPELINE_SOFTWARE*`, `COMPOSITING_*`, `BACKSCATTER_*`,
`COHERENCE_*`). The item properties mirror them under `glace:*` and
`processing:*`, so you can filter on them without opening a raster — but the
file is the source of truth and stays self-describing if separated from this
catalog.

Each year's run record is published as a collection asset: `manifest-{year}`
lists every file the run wrote with its size, checksum and band statistics, and
`config-{year}` is the configuration that produced them.

## Pairing with the mosaics

`glace-ch-mosaics` holds the same measurements warped onto one fixed
WebMercator grid, one item per year. Join on the year. Do not join on geometry:
the tiles are per-UTM-zone and the mosaics are WebMercator.

## Gotchas

- **QA-NUM 0 means absent**, not "zero observations".
- **NaN is nodata** on every float32 band. Some browser GeoTIFF decoders drop
  LERC's validity mask and hand a NaN through as `0`; that is a client defect,
  not a property of these files.
- Tiles are 1000 × 1000 px with one internal overview level, LERC_ZSTD
  compressed with a per-quantity error bound anchored to that quantity's own
  measurement noise (1e-3 for coherence, 1e-5 for linear backscatter).
- Coverage follows the glacier perimeter, so the tile set is ragged. A missing
  tile means no ice there, not a processing gap.
