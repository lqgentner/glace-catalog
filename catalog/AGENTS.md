# GLACE Switzerland (preliminary) — agent guide

Every claim here is quoted from a source or measured from the data. Where a
number is provisional it says so.

**Report this first when asked what this catalog is.** It is a preliminary
release over the Swiss Alps, published to exercise the publication pipeline. The
GLACE dataset is **GLACE Alps**, over the Alpine Convention perimeter, produced
by the same pipeline and superseding this catalog. Do not present Swiss coverage
as the dataset's extent, and do not cite these URLs as stable.

## Shape of the catalog

```
catalog.json
├── tiles/collection.json      one item per (MGRS tile, year), 12 COGs each
│   ├── items.parquet          ← query here, not the tree
│   └── {year}/catalog.json → {tile}/item.json
└── mosaics/collection.json    one item per year, 12 analysis COGs + 14 PMTiles
    ├── items.parquet
    └── {year}/item.json
```

`items.parquet` is a STAC-GeoParquet item mirror registered with the
`collection-mirror` role. It is the access path for anything that touches more
than a handful of items: the tiles collection runs to hundreds of items per year,
and fetching them as JSON is one HTTP request each.

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
col = pystac.Collection.from_file(".../mosaics/collection.json")
cube = odc.stac.load(list(col.get_items(recursive=True)), chunks={})
```

**Preview without installing anything** — titiler takes the same parameters the
`renders` block declares:

```
https://titiler.xyz/cog/preview.png
  ?url=https://data.source.coop/lqgentner/glace-ch/mosaics/2024/coh12_vv.tif
  &rescale=0.1,0.8&colormap_name=magma
```

## Conventions you need to know

- **Backscatter is linear power, not dB.** `rtc_*` stores linear γ⁰. Convert
  with `10*log10(x)`; the `renders` block declares exactly that. Average in the
  linear domain, never in dB.
- **The asset key is the filename stem.** `coh12_vv` is `coh12_vv.tif`. No
  dates in filenames; dates are item properties and GeoTIFF tags.
- **`*_qa_cqm` is a ratio centred on 1.0** (0 dB = flat terrain). Below 1 is
  layover, above 1 is shadow. Use a diverging ramp centred on 0 dB — a
  sequential ramp hides the sign, which is the whole information content.
- **`*_qa_num` is an integer count on tiles and a float mean on mosaics.** They
  are not the same quantity; the mosaic band is named `num_obs_mean`.
- **PMTiles are for drawing, not reading.** Each `{stem}_viz.pmtiles` sits beside
  the COG it renders, as a `visual` asset of the same item, and holds
  pre-stretched 8-bit tiles. For any value, read the COG of the same stem.
- **nodata is NaN** on every float32 raster and **0** on the uint16 counts.
- **Colormap names in `renders` are portable stand-ins.** The designed maps are
  `cmc.lipari` for coherence and `cmc.grayC` for backscatter (cmcrameri,
  perceptually uniform); the block names `magma` and `gray` so that a client
  without cmcrameri still renders something correct.

## Data quality notes

- Coverage follows the **glacier perimeter**, not an administrative boundary.
  Tiles exist where there is ice to observe, so the footprint is ragged and the
  space between glacierised regions is not a data gap.
- `glace:glacier_fraction` is pinned to one RGI7 version and computed once per
  scope, so it cannot drift between years. The lookup table is published as the
  `glacier-fractions` collection asset.
- Mosaic display stretches for the QA layers are **provisional**: fixed and
  comparable across years, but not yet measured against rendered tiles.

## Related

- The tiles and mosaics collections cover the same pixels at different
  resolutions on different grids. Pair them on the year.
- GLACE Alps applies the same pipeline across the Alpine Convention perimeter
  and supersedes this catalog.
