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

`items.parquet` has one row per tile and year. The collection's `items` asset
lists every column with its type and description as `table:columns`. These
are the ones queries use:

- `id`: Item id, `{mgrs_tile}-{year}`, such as `32TMS55-2024`.
- `bbox`: Bounds of the footprint in WGS84. The GeoParquet covering column, with statistics per row group, so a bounding-box filter skips row groups.
- `assets`: One struct per asset key, with absolute https hrefs. An asset a tile lacks, such as coherence on the 2015 gap tiles, is null.
- `glace:mgrs_tile`: The 10 km MGRS square, such as `32TMS55`.
- `proj:code`: CRS of the item's rasters, the square's UTM zone, such as `EPSG:32632`.
- `glace:glacier_fraction`: Share of the square inside RGI7-C-11 glacier outlines, 0 to 1. The same every year.
- `glace:glacier_fraction_buffered`: Share of the square inside the same outlines buffered by 250 m, 0 to 1. The same every year.
- `datetime`: Midpoint of the compositing window.

`glace:mgrs_tile` is the join key: the tile grid below calls it `mgrs_tile`,
and `grid:code` gives it the grid extension's prefix, `MGRS-`.

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

Asset keys are filenames without `.tif`: `coh12_vv`, `coh12_vh`, `rtc_vv` and
`rtc_vh`, each with its QA layers `{key}_qa_num` and `{key}_qa_cqm`. The
collection's `item_assets` give each key's data type and nodata, and its
`renders` block the default display ranges and colormaps. See the
[root guide](../AGENTS.md#interpret-values) for units and decibel conversion.

## Processing and dates

GeoTIFF tags record the processing chain: `PRODUCT_TYPE`, `PIPELINE_SOFTWARE*`,
`COMPOSITING_*`, `BACKSCATTER_*` and `COHERENCE_*`. The GeoTIFF tags are the
source of truth; the STAC metadata copies them in three places:

- The collection's `item_assets` hold, per asset key, the `glace:*` fields
  that are the same on every tile and in every year, such as
  `glace:compositing_method` and `glace:coherence_window_range`. The build
  refuses a raster whose tags state otherwise.
- Each item asset holds the values that are its file's own:
  `glace:contributing_observations`, and for coherence `glace:coherence_average`
  and `glace:coherence_std`. In `items.parquet` they are fields of the asset's
  struct in the `assets` column.
- Item properties hold what applies to the whole tile: `processing:level`,
  `processing:software`, `glace:dem` and the acquisition dates below. These
  are columns of `items.parquet`, so filter on them there.

All tile items in a year share `start_datetime` and `end_datetime`, the
compositing window selected for that year's lowest-snow period. Actual
acquisition dates vary by tile and product. Read `glace:coh_first_acquisition`,
`glace:coh_last_acquisition` and the corresponding `rtc` properties. The last
coherence date is about twelve days before the last backscatter date, because
both images of a coherence pair must fall within the window.

## Reading and comparing

- A count of 0 is nodata and also means no observation contributed.
- Some browser decoders return 0 instead of NaN for nodata, because they drop
  LERC's validity mask.
- Tiles use LERC_ZSTD compression with error bounds based on each quantity's
  measurement noise.
- Pair tiles with mosaics by year. Reproject before comparing pixels: tiles
  use UTM zones; mosaics use ETRS89-LAEA.

See the [README](README.md) for cube and QGIS examples.
