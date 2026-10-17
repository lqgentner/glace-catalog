# GLACE Alps – agent guide

Known gaps: In 2015, coherence is missing on 91 MGRS squares in the south-central Alps (100 km squares 32TNT, 32TNU, 32TPT, 32TPU and 32UPU) because Sentinel-1 coverage was limited that summer. Two of them lack backscatter as well: 32TNT14 has no 2015 item, and the 2015 backscatter of 32TNT24 holds no valid pixels. Every other year covers all squares.

## Find data

```
catalog.json
├── tiles/collection.json      one item per (MGRS square, year), 12 COGs each
│   ├── items.parquet          searchable index for all years
│   └── {year}/catalog.json → {tile}/item.json
└── mosaics/collection.json    one item per year, 12 COGs + 12 PMTiles
    ├── items.parquet
    ├── styles/{year}.json     MapLibre styles
    └── {year}/item.json
```

Use each collection's `items.parquet` to search multiple items. This
STAC-GeoParquet index has the `collection-mirror` role and includes `glace:*`
and `proj:*` properties as columns. Individual item JSON files are the
authoritative records.

This DuckDB query finds tiles with more than 20% glacier cover:

```sql
INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;
SELECT id, datetime, "glace:glacier_fraction"
FROM read_parquet('https://data.source.coop/giuz/glace-alps/tiles/items.parquet')
WHERE "glace:glacier_fraction" > 0.2;
```

Read COGs over HTTPS with rasterio. Use raster windows for small areas and
specify bounds in the raster's CRS. For examples and collection-specific
instructions, see [tiles](tiles/AGENTS.md) and [mosaics](mosaics/AGENTS.md).

## Interpret values

- Coherence is dimensionless and linear.
- Backscatter is linear γ⁰ power. Convert with `10*log10(x)` for decibel
  display; average values in linear units.
- `*_qa_cqm` is a dimensionless linear ratio describing local resolution.
  Higher values mean better resolution. Average in linear units; convert
  with `10*log10(x)` for decibel display.
- `*_qa_num` is an integer count on tiles and a mean count on mosaics. Each
  40 m mosaic pixel averages sixteen 10 m counts.
- Nodata is NaN for float32 rasters and 0 for uint16 counts.
- Asset keys are filenames without `.tif`. Dates are in item properties and
  GeoTIFF tags.
- Read COGs for measurements. `{stem}_viz.pmtiles` contains lossy 8-bit value
  codes for web maps, stored beside the COGs as `visual` assets.

## Compare years and collections

Tiles and mosaics represent the same source composites on different grids.
Pair them by year; reproject before comparing pixels. Mosaics use a fixed
equal-area grid across years.

`glace:glacier_fraction` and `glace:glacier_fraction_buffered` are computed once
per scope from RGI7-C-11 outlines, plain and buffered by
250 m, so a tile has the same values every year. The tiles
collection's `grid` asset, `tiles/grid.pmtiles`, carries them per tile.

Default display ramps and ranges are in the collections' `renders` blocks and
the year styles. The [GLACE Viewer](https://lqgentner.github.io/glace-viewer) draws the PMTiles with these styles.
