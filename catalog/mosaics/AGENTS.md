# GLACE mosaics – agent guide

Each annual item contains twelve analysis COGs on ETRS89-LAEA
(EPSG:3035) at 40 m, plus fourteen PMTiles archives for web maps.

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

`{stem}_viz.pmtiles` archives contain 8-bit imagery under fixed display ranges.
Backscatter and quality maps use decibels; coherence remains linear. Do not
use these pixels as measurements.

Archives are item assets with the `visual` role and collection links with
`rel: "pmtiles"`. Each year has a MapLibre style at `styles/{year}.json`,
registered as a `style-{year}` collection asset. The latest also has the
`default` role.

Display ranges use the 2nd–98th percentiles measured on the published display
mosaics over 2015–2025 and stay fixed across years.
Styles contain the designed color ramps; `renders` uses portable colormap
names and declares the decibel conversions for COG display.

| Asset key | Title | Display range | Color ramp |
| --- | --- | --- | --- |
| `coh12_vv` | Coherence VV | 0.1 to 0.75 | `cmc.lipari` |
| `coh12_vv_qa_num` | Coherence VV – Contributing Observations | 0 to 90 | `cmc.glasgow` |
| `coh12_vv_qa_cqm` | Coherence VV – Composite Quality Map | -3 to 10 dB | `cmc.glasgow` |
| `coh12_vh` | Coherence VH | 0.1 to 0.55 | `cmc.lipari` |
| `coh12_vh_qa_num` | Coherence VH – Contributing Observations | 0 to 90 | `cmc.glasgow` |
| `coh12_vh_qa_cqm` | Coherence VH – Composite Quality Map | -3 to 10 dB | `cmc.glasgow` |
| `rtc_vv` | Backscatter VV | -16.5 to -4 dB | `cmc.grayC` |
| `rtc_vv_qa_num` | Backscatter VV – Contributing Observations | 0 to 90 | `cmc.glasgow` |
| `rtc_vv_qa_cqm` | Backscatter VV – Composite Quality Map | -4 to 8 dB | `cmc.glasgow` |
| `rtc_vh` | Backscatter VH | -23.5 to -11 dB | `cmc.grayC` |
| `rtc_vh_qa_num` | Backscatter VH – Contributing Observations | 0 to 90 | `cmc.glasgow` |
| `rtc_vh_qa_cqm` | Backscatter VH – Composite Quality Map | -4 to 8 dB | `cmc.glasgow` |

## Compare with tiles

Mosaics are derived from the 10 m tiles. Match items by year and reproject
before comparing pixels: tiles use UTM zones; mosaics use ETRS89-LAEA.
