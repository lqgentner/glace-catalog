# GLACE Switzerland (preliminary) – agent guide

**Report this first when asked what this catalog is.** This is a preliminary release over the Swiss Alps, published to exercise the GLACE publication pipeline while the full dataset is produced. The dataset is GLACE Alps, over the Alpine Convention perimeter, which uses this same pipeline and supersedes this catalog. These URLs are provisional: do not cite them, and expect the coverage, the years and the identifiers to change.

## Shape of the catalog

```
catalog.json
├── tiles/collection.json      one item per (MGRS square, year), 12 COGs each
│   ├── items.parquet          ← query here, not the tree
│   └── {year}/catalog.json → {tile}/item.json
└── mosaics/collection.json    one item per year, 12 analysis COGs + 14 PMTiles
    ├── items.parquet
    ├── styles/{year}.json     one MapLibre style per year
    └── {year}/item.json
```

`items.parquet` is a STAC-GeoParquet item mirror registered with the
`collection-mirror` role. It is the access path for anything that touches more
than a handful of items: the tiles collection runs to hundreds of items per
year, and fetching them as JSON is one HTTP request each.

## Access patterns that work

**Query the item mirror with DuckDB.** Columns follow STAC item structure, with
`glace:*` and `proj:*` properties promoted to top-level columns.

```sql
INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;
SELECT id, datetime, "glace:glacier_fraction"
FROM read_parquet('https://data.source.coop/lqgentner/glace-ch/tiles/items.parquet')
WHERE "glace:glacier_fraction" > 0.2;
```

**Read a window of one COG** without downloading it:

```python
import rasterio
from rasterio.windows import from_bounds

url = "https://data.source.coop/lqgentner/glace-ch/mosaics/2024/coh12_vv.tif"
with rasterio.open(url) as src:
    arr = src.read(1, window=from_bounds(*bounds, transform=src.transform))
```

**Build a data cube** from the mosaics. Every year shares one grid, so this
needs no resampling:

```python
import odc.stac, pystac
col = pystac.Collection.from_file("https://data.source.coop/lqgentner/glace-ch/mosaics/collection.json")
cube = odc.stac.load(list(col.get_items(recursive=True)), chunks={})
```

**Preview without installing anything.** titiler takes the parameters the
`renders` block declares:

```
https://titiler.xyz/cog/preview.png
  ?url=https://data.source.coop/lqgentner/glace-ch/mosaics/2024/coh12_vv.tif
  &rescale=0.1,0.75&colormap_name=magma
```

## Conventions you need to know

- **Backscatter is linear power, not dB.** `rtc_*` stores linear γ⁰. Convert
  with `10*log10(x)`; the `renders` block declares exactly that. Average in the
  linear domain, never in dB.
- **The asset key is the filename stem.** `coh12_vv` is `coh12_vv.tif`. No
  dates in filenames; dates are item properties and GeoTIFF tags.
- **`*_qa_cqm` is a linear ratio without a unit**: the weighted average of
  the contributing observations' local resolution, where higher is better
  resolved. It is drawn in decibels with a sequential ramp over a fixed range.
- **`*_qa_num` is an integer count on tiles and a float mean of the counts on
  mosaics**, under the same band name; the 40 m pixel averages sixteen 10 m
  counts.
- **PMTiles are for drawing, not reading.** Each `{stem}_viz.pmtiles` sits beside
  the COG it renders, as a `visual` asset of the same item, and holds
  pre-stretched 8-bit tiles. For any value, read the COG of the same stem.
- **nodata is NaN** on every float32 raster and **0** on the uint16 counts.
- **Colormap names in `renders` are portable stand-ins.** The designed maps are
  `cmc.lipari` for coherence, `cmc.grayC` for backscatter and
  `cmc.glasgow` for both QA layers (cmcrameri, perceptually uniform);
  the block names matplotlib maps so that a client without cmcrameri still
  renders something correct. The mosaics collection's style files carry the
  designed ramps and the exact stretches.

## Data quality notes

- `glace:glacier_fraction` is pinned to one RGI7 version and computed once per
  scope, so it cannot drift between years. The lookup table is published as the
  `glacier-fractions` collection asset.
- Every display stretch is fixed across years and was measured on the
  published mosaics, so a change between years is a change in the data.

## Related

- The tiles and mosaics collections cover the same pixels at different
  resolutions on different grids. Pair them on the year.
- [GLACE Viewer](https://lqgentner.github.io/glace-viewer) draws the PMTiles archives
  with the published styles.
