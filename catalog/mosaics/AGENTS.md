# GLACE mosaics – agent guide

Each annual item contains twelve analysis COGs on ETRS89-LAEA
(EPSG:3035) at 40 m, plus twelve PMTiles archives for web maps.

## Grid and analysis

The extent is a fixed constant, so every year has identical `proj:shape` and
`proj:transform`. Stack years without alignment or resampling. The grid is
equal-area: multiply a pixel count by 1600 m² to calculate area.

```python
import odc.stac
import pystac

collection = pystac.Collection.from_file(
    "https://data.source.coop/giuz/glace-alps/mosaics/collection.json"
)
cube = odc.stac.load(
    list(collection.get_items(recursive=True)),
    bands=["coh12_vv", "coh12_vh"],
    chunks={},
)
# Index the years by number. Subtracting two `sel(time="YYYY")` slices
# instead gives an empty result: each keeps its own date, and xarray aligns
# on the dates before subtracting.
yearly = cube.groupby("time.year").first()
change = yearly.sel(year=2026) - yearly.sel(year=2015)
```

Read COGs for analysis. Values are linear; `*_qa_num` contains mean observation
counts. See the [root guide](../AGENTS.md#interpret-values) for units and nodata.

## Item index

`items.parquet` has one row per year and these columns, also listed with their
types as `table:columns` on the collection's `items` asset:

| Column | Description |
| --- | --- |
| `type` | Always `Feature`. |
| `stac_version` | STAC version of the item. |
| `stac_extensions` | Schema URIs of the STAC extensions the item uses. |
| `id` | Item id, `{scope}-mosaic-{year}`, such as `alps-mosaic-2024`. |
| `geometry` | Footprint of the year's mosaic in WGS84 (EPSG:4326), as WKB: the union of the year's tiles, simplified. |
| `bbox` | Bounds of the footprint in WGS84. The GeoParquet covering column, with statistics per row group, so a bounding-box filter skips row groups. |
| `links` | The item document's links, with absolute hrefs: root, collection, parent and the CEOS-ARD specification. |
| `assets` | One struct per asset key, with absolute https hrefs. |
| `collection` | Id of the collection, `glace-{scope}-mosaics`. |
| `start_datetime` | First day of the year's compositing window, at 00:00 UTC. |
| `end_datetime` | The day after the window's last day, at 00:00 UTC. |
| `constellation` | Always `sentinel-1`. |
| `instruments` | Always `[c-sar]`. |
| `sar:instrument_mode` | Always `IW`, Interferometric Wide swath. |
| `sar:frequency_band` | Always `C`. |
| `sar:polarizations` | Polarizations with at least one composite, `VV` and `VH`. |
| `proj:code` | CRS of the item's COGs, `EPSG:3035`. |
| `proj:shape` | Raster shape of the item's COGs, `[rows, columns]`. |
| `proj:transform` | Affine transform of the item's COGs, six coefficients in `proj:code` units. |
| `version` | Dataset version the item belongs to. |
| `glace:build_fingerprint` | Fingerprint of the inputs and settings the year's mosaics were built from. It changes when any of them change. |
| `description` | Description of the item. |
| `datetime` | Midpoint of the compositing window. |

## Web maps

`{stem}_viz.pmtiles` archives contain value-encoded WebP tiles: an 8-bit code
in all three channels, code 0 nodata, linear over a fixed transport range per
layer. Decode a code with the asset band's `raster:scale` and `raster:offset`:
`value = code * scale + offset`. Backscatter and quality maps hold decibels;
coherence remains linear. The tiles are lossy and values outside the range are
clipped, so read the COGs for measurements.

Archives are listed as `visual` assets of their year's item, under the key
`{stem}_viz` with an href relative to the item. Each year's item also lists
its MapLibre style, `{year}/styles/default.json`, as the `style-default` asset
with the `style` and `default` roles. The style declares each archive
`{stem}_viz.pmtiles` as a `raster-dem` source with a custom encoding and draws
it as the `color-relief` layer `glace-{stem}-{year}`. The collection's
`style-default` asset, `styles/default.json`, is the latest year's style.

Each layer's transport range is the same every year, so years compare like
with like. The styles draw a default ramp over a default range, which a client
can change without re-tiling. The `renders` block holds the same defaults for
the COGs and declares their decibel conversions.

## Compare with tiles

Mosaics are derived from the 10 m tiles. Match items by year and reproject
before comparing pixels: tiles use UTM zones; mosaics use ETRS89-LAEA.
