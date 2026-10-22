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
WHERE datetime BETWEEN '2026-01-01' AND '2026-12-31'
  AND "glace:glacier_fraction_buffered" > 0.1
ORDER BY frac DESC;
```

`items.parquet` has one row per item and these columns, also listed with their
types as `table:columns` on the collection's `items` asset:

| Column | Description |
| --- | --- |
| `type` | Always `Feature`. |
| `stac_version` | STAC version of the item. |
| `stac_extensions` | Schema URIs of the STAC extensions the item uses. |
| `id` | Item id, `{mgrs_tile}-{year}`, such as `32TMS55-2024`. |
| `geometry` | Footprint of the MGRS square in WGS84 (EPSG:4326), as WKB: its four corners. |
| `bbox` | Bounds of the footprint in WGS84. The GeoParquet covering column, with statistics per row group, so a bounding-box filter skips row groups. |
| `links` | The item document's links, with absolute hrefs: root, collection, parent and the CEOS-ARD specification. |
| `assets` | One struct per asset key, with absolute https hrefs. An asset a tile lacks, such as coherence on the 2015 gap tiles, is null. |
| `collection` | Id of the collection, `glace-{scope}-tiles`. |
| `start_datetime` | First day of the year's compositing window, at 00:00 UTC. |
| `end_datetime` | The day after the window's last day, at 00:00 UTC. |
| `constellation` | Always `sentinel-1`. |
| `instruments` | Always `[c-sar]`. |
| `sar:instrument_mode` | Always `IW`, Interferometric Wide swath. |
| `sar:frequency_band` | Always `C`. |
| `sar:polarizations` | Polarizations with at least one composite, `VV` and `VH`. |
| `version` | Dataset version the item belongs to. |
| `glace:mgrs_tile` | The 10 km MGRS square, such as `32TMS55`. |
| `grid:code` | The MGRS square in the STAC grid extension's form, `MGRS-32TMS55`. |
| `mgrs:utm_zone` | UTM zone of the square. |
| `mgrs:latitude_band` | MGRS latitude band of the square. |
| `mgrs:grid_square` | MGRS 100 km grid square identifier, the two letters after the band. |
| `proj:code` | CRS of the item's rasters, the square's UTM zone, such as `EPSG:32632`. |
| `proj:shape` | Raster shape of the item's COGs, `[rows, columns]`. |
| `proj:transform` | Affine transform of the item's COGs, six coefficients in `proj:code` units. |
| `glace:coh_first_acquisition` | Date of the first acquisition in the square's coherence composites, `YYYY-MM-DD`. |
| `glace:coh_last_acquisition` | Date of the last acquisition in the square's coherence composites. About twelve days before the last backscatter date, because both images of a pair must fall in the window. |
| `glace:rtc_first_acquisition` | Date of the first acquisition in the square's backscatter composites. |
| `glace:rtc_last_acquisition` | Date of the last acquisition in the square's backscatter composites. |
| `glace:glacier_fraction` | Share of the square inside RGI7-C-11 glacier outlines, 0 to 1. The same every year. |
| `glace:glacier_fraction_buffered` | Share of the square inside the same outlines buffered by 250 m, 0 to 1. The same every year. |
| `processing:level` | Always `L3`, a composite over many acquisitions. |
| `glace:dem` | DEM used for terrain correction and geocoding. |
| `datetime` | Midpoint of the compositing window. |

## Tile grid

`tiles/grid.pmtiles` (asset `grid`, also linked as `rel: "pmtiles"`) is a
vector PMTiles of every tile of any year, layer `mgrs`, zooms 0–10. Each
polygon has `mgrs_tile`, `glacier_fraction` and `glacier_fraction_buffered`,
the share of the tile within RGI7-C-11 outlines,
plain and buffered by 250 m. The grid is the same every year.
Its style is `styles/grid.json`. Use it to draw or pick tiles on a map; use
`items.parquet` to query them. The collection's default style,
`styles/default.json`, draws the latest year's `coh12_vv` from the mosaics
collection's archive, `mosaics/{year}/coh12_vv_viz.pmtiles`, under the grid's
outlines.

## Assets and units

Asset keys are filenames without `.tif`. QA layers use the measurement's key
followed by `_qa_num` or `_qa_cqm`.

| Asset key | Title | Data type | Nodata | Display in |
| --- | --- | --- | --- | --- |
| `coh12_vv` | Coherence VV | float32 | NaN | stored units |
| `coh12_vv_qa_num` | Coherence VV – Contributing Observations | uint16 | 0 | stored units |
| `coh12_vv_qa_cqm` | Coherence VV – Composite Quality Map | float32 | NaN | dB, `10*log10(x)` |
| `coh12_vh` | Coherence VH | float32 | NaN | stored units |
| `coh12_vh_qa_num` | Coherence VH – Contributing Observations | uint16 | 0 | stored units |
| `coh12_vh_qa_cqm` | Coherence VH – Composite Quality Map | float32 | NaN | dB, `10*log10(x)` |
| `rtc_vv` | Backscatter VV | float32 | NaN | dB, `10*log10(x)` |
| `rtc_vv_qa_num` | Backscatter VV – Contributing Observations | uint16 | 0 | stored units |
| `rtc_vv_qa_cqm` | Backscatter VV – Composite Quality Map | float32 | NaN | dB, `10*log10(x)` |
| `rtc_vh` | Backscatter VH | float32 | NaN | dB, `10*log10(x)` |
| `rtc_vh_qa_num` | Backscatter VH – Contributing Observations | uint16 | 0 | stored units |
| `rtc_vh_qa_cqm` | Backscatter VH – Composite Quality Map | float32 | NaN | dB, `10*log10(x)` |

Average backscatter and composite quality maps in linear units. Apply decibel
conversion only for display. The collection's `renders` block holds the
default display ranges and colormaps.

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
