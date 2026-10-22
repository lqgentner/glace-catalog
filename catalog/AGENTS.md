# GLACE Alps – agent guide

## Find data

```
catalog.json
├── tiles/collection.json      one item per (MGRS square, year), 12 COGs each
│   ├── items.parquet          searchable index for all years
│   └── {year}/catalog.json → {tile}/item.json
└── mosaics/collection.json    one item per year, 12 COGs + 12 PMTiles
    ├── items.parquet
    ├── styles/default.json    MapLibre style of the latest year
    └── {year}/item.json → styles/default.json, that year's style
```

Use each collection's `items.parquet` to search multiple items. This
STAC-GeoParquet index has the `collection-mirror` role and holds the item
properties as columns; each collection's guide lists them. Individual item
JSON files are the authoritative records.

This DuckDB query finds the 2026 tiles with more than 20% glacier
cover. The index holds one row per tile and year, so filter by year:

```sql
INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;
SELECT id, datetime, "glace:glacier_fraction"
FROM read_parquet('https://data.source.coop/giuz/glace-alps/tiles/items.parquet')
WHERE "glace:glacier_fraction" > 0.2
  AND year(datetime) = 2026;
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

## Common pitfalls

**Composite quality.** Lower `*_qa_cqm` values indicate a lower local resolution of the composite: the closer to 0, the fewer favourable viewing geometries contributed. In the validation, a random forest maps glaciers less accurately where the CQM is below 0.5 (linear, about −3 dB; AUC 0.87) than elsewhere (0.97). These pixels make up 3% of the pixels within 250 m of the glacier outlines and 1.5% of the glacier pixels. Mask or downweight them, or use the CQM as an additional model input.

**Coherence bias.** Coherence is estimated over a 35 × 7 (range × azimuth) window. The finite window biases the estimate upwards, so fully decorrelated surfaces do not reach 0: over large lakes, the mean is about 0.09. Absolute values therefore differ from products that use larger windows, such as the global seasonal Sentinel-1 coherence of [Kellndorfer et al. (2022)](https://doi.org/10.1038/s41597-022-01189-6). Do not compare values across products, and choose thresholds on GLACE itself.

**Low coherence outside glaciers.** Other surfaces also decorrelate over 12 days and can have similarly low coherence, such as snow patches, lakes and unstable slopes.

**Validated scope.** The validation covers glacier mapping in Switzerland only. Its scores do not describe glacier mapping performance in other parts of the Alps, or farther than 250 m from the glacier outlines. Other applications may also benefit from the composites, but were not validated.

**2015 coverage.** In 2015, coherence is missing on 91 MGRS squares in the north-central Alps (100 km squares 32TNT, 32TNU, 32TPT, 32TPU and 32UPU) because Sentinel-1 coverage was limited that summer. Two of them, 32TNT14 and 32TNT24, have no 2015 items. Every other year covers all squares.

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
