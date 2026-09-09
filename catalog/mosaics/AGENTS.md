# GLACE mosaics – agent guide

One item per year on a fixed ETRS89-LAEA (EPSG:3035) grid at 40 m. Twelve
analysis rasters per year and fourteen PMTiles archives beside them, which are
what drawing uses.

## The grid is the point

Every year shares one fixed extent, resolution and transform. Two things
follow, and both are what this collection exists for:

1. **Temporal stacking is free.** No reprojection, no alignment, no resampling.
2. **Areas are pixel counts.** The grid is equal-area, so summing pixels gives an
   area without a latitude correction. Do not do this on a Web Mercator raster.

```python
import odc.stac, pystac
col = pystac.Collection.from_file(
    "https://data.source.coop/lqgentner/glace-ch/mosaics/collection.json")
cube = odc.stac.load(
    list(col.get_items(recursive=True)),
    bands=["coh12_vv", "coh12_vh"], chunks={},
)
change = cube.sel(time="2024") - cube.sel(time="2021")
```

## Drawing versus reading

Two products, and they are not interchangeable.

**PMTiles**, `{stem}_viz.pmtiles` in each year's item directory, are what a map
client uses. Each one is an asset of that item with the `visual` role and a
`rel: "pmtiles"` link on this collection, and each year has its own MapLibre
style as a `style-{year}` asset under `styles/`; the latest year's also carries
the `default` role. Their pixels are 8-bit display codes under a fixed stretch,
and the backscatter and quality-map archives are in **decibels** while every
raster in this catalog is linear. They are renderings. Never read one for a
value.

**The COGs** carry the measurements. The `renders` block declares the stretch,
the ramp and the decibel `expression`, so a client can colorize them at draw
time and get the same picture with the numbers still underneath.

## `*_qa_num` is a mean here

On the 10 m tiles it is an integer count of contributing acquisitions. Warping
to 40 m averages sixteen source pixels, so the mosaic band is a float32
**mean** and is named `num_obs_mean`. It is not the same quantity as the tile
band of the same asset key.

## Rendering parameters

Every stretch is fixed across years and was measured on the published display
mosaics (2 to 98 % over 2021–2024). The archives bake these; the `renders`
block declares the same numbers with portable colormap names, which any
titiler-compatible client accepts verbatim:

```
https://titiler.xyz/cog/preview.png
  ?url=https://data.source.coop/lqgentner/glace-ch/mosaics/2024/rtc_vv.tif
  &expression=10*log10(b1)&rescale=-16.5,-4&colormap_name=gray
```

| asset key | title | stretch | ramp |
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

The count ceiling is shared by both products so that backscatter seeing more
acquisitions than coherence, which needs pairs, stays visible. The quality map
is one-sided, so its range is the measured one rather than a symmetric one;
0 dB has no physical meaning in the composites.

## Related

The tiles collection is the archive these are derived from: same measurements
at 10 m in each square's own UTM zone, one item per (square, year). Pair on the
year, not on geometry.
