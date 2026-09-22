# GLACE Alps – agent guide

## Find data

```
catalog.json
├── tiles/collection.json      one item per (MGRS square, year), 12 COGs each
│   ├── items.parquet          searchable index for all years
│   └── {year}/catalog.json → {tile}/item.json
└── mosaics/collection.json    one item per year, 12 COGs + 14 PMTiles
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
- Read COGs for measurements. `{stem}_viz.pmtiles` contains rendered 8-bit
  imagery for web maps, stored beside the COGs as `visual` assets.

## Compare years and collections

Tiles and mosaics represent the same source composites on different grids.
Pair them by year; reproject before comparing pixels. Mosaics use a fixed
equal-area grid across years.

`glace:glacier_fraction` uses one RGI7 version and is computed once per scope.
The lookup table is the tiles collection's `glacier-fractions` asset.

Display ranges are fixed across years. The collection's `renders` block uses
portable matplotlib colormap names. The MapLibre styles contain the designed
cmcrameri ramps: `cmc.lipari` for coherence, `cmc.grayC`
for backscatter and `cmc.glasgow` for QA layers.
The [GLACE Viewer](https://lqgentner.github.io/glace-viewer) uses these styles and PMTiles.
