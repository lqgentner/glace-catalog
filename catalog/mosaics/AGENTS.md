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
change = cube.sel(time="2025") - cube.sel(time="2015")
```

Read COGs for analysis. Values are linear; `*_qa_num` contains mean observation
counts. See the [root guide](../AGENTS.md#interpret-values) for units and nodata.

## Web maps

`{stem}_viz.pmtiles` archives contain value-encoded WebP tiles: an 8-bit code
in all three channels, code 0 nodata, linear over a fixed transport range per
layer. Decode a code with the asset band's `raster:scale` and `raster:offset`:
`value = code * scale + offset`. Backscatter and quality maps hold decibels;
coherence remains linear. The tiles are lossy and values outside the range are
clipped, so read the COGs for measurements.

Archives are assets of their year's item with the `visual` role; the
collection does not link them. The key is `{stem}_viz`, and the href is
relative to the item. Each year has a MapLibre style at
`styles/{year}.json`, registered as a `style-{year}` collection asset. It
declares each archive `{stem}_viz.pmtiles` as a `raster-dem` source with a
custom encoding and draws it as the `color-relief` layer `glace-{stem}-{year}`.
The latest style also has the `default` role.

Transport ranges are fixed across years, so years compare like with like.
The styles draw a default ramp over a default range; change either in the
client without new tiles. `renders` uses portable colormap names and declares
the decibel conversions for COG display.

| Asset key | Title | Transport range | Default range | Default ramp |
| --- | --- | --- | --- | --- |
| `coh12_vv` | Coherence VV | 0 to 1 | 0.1 to 0.75 | `cmc.lipari` |
| `coh12_vv_qa_num` | Coherence VV – Contributing Observations | 0 to 253 | 0 to 90 | `cmc.glasgow` |
| `coh12_vv_qa_cqm` | Coherence VV – Composite Quality Map | -13 to 19 dB | -3 to 10 dB | `cmc.glasgow` |
| `coh12_vh` | Coherence VH | 0 to 1 | 0.1 to 0.55 | `cmc.lipari` |
| `coh12_vh_qa_num` | Coherence VH – Contributing Observations | 0 to 253 | 0 to 90 | `cmc.glasgow` |
| `coh12_vh_qa_cqm` | Coherence VH – Composite Quality Map | -13 to 19 dB | -3 to 10 dB | `cmc.glasgow` |
| `rtc_vv` | Backscatter VV | -25 to 5 dB | -16.5 to -4 dB | `cmc.grayC` |
| `rtc_vv_qa_num` | Backscatter VV – Contributing Observations | 0 to 253 | 0 to 90 | `cmc.glasgow` |
| `rtc_vv_qa_cqm` | Backscatter VV – Composite Quality Map | -13 to 12 dB | -4 to 8 dB | `cmc.glasgow` |
| `rtc_vh` | Backscatter VH | -31 to -1 dB | -23.5 to -11 dB | `cmc.grayC` |
| `rtc_vh_qa_num` | Backscatter VH – Contributing Observations | 0 to 253 | 0 to 90 | `cmc.glasgow` |
| `rtc_vh_qa_cqm` | Backscatter VH – Composite Quality Map | -13 to 12 dB | -4 to 8 dB | `cmc.glasgow` |

## Compare with tiles

Mosaics are derived from the 10 m tiles. Match items by year and reproject
before comparing pixels: tiles use UTM zones; mosaics use ETRS89-LAEA.
