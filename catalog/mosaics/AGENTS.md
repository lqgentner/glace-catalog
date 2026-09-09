# GLACE mosaics — agent guide

One item per year on a fixed ETRS89-LAEA (EPSG:3035) grid at 40 m. Twelve
analysis rasters per year and fourteen PMTiles archives beside them, which are
what drawing uses.

## The grid is the point

Every year shares one hard-coded extent, resolution and transform. Two things
follow, and both are what this collection exists for:

1. **Temporal stacking is free.** No reprojection, no alignment, no resampling.
2. **Areas are pixel counts.** The grid is equal-area, so summing pixels gives an
   area without a latitude correction. Do not do this on a WebMercator raster.

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
the `default` role. Their pixels are 8-bit display codes
under a fixed stretch, and the backscatter archives are in **decibels** while
every raster in this catalog is linear. They are renderings. Never read one for
a value.

**The COGs** carry the measurements. The `renders` block declares the stretch,
the ramp and the decibel `expression`, so a client can colorize them at draw
time and get the same picture with the numbers still underneath.

## `*_qa_num` is a mean here

On the 10 m tiles it is an integer count of contributing acquisitions. Warping
to 40 m averages sixteen source pixels, so the mosaic band is a float32 **mean**
and is named `num_obs_mean`. It is not the same quantity as the tile band of the
same asset key.

## Rendering parameters

The `renders` block on this collection carries the fixed stretch for each layer,
which any titiler-compatible client accepts verbatim:

```
https://titiler.xyz/cog/preview.png
  ?url=.../mosaics/2024/rtc_vv.tif
  &expression=10*log10(b1)&rescale=-18.5,-5&colormap_name=gray
```

The stretch is fixed rather than per-year deliberately: a percentile stretch
recomputed per year gives every year its own scale, and a real change in
coherence then renders as no change at all.

The QA stretches are `*_qa_num` 0–90 and `*_qa_cqm` −3 to 10 dB (COH12) and −4
to 8 dB (RTC), measured on the warped Swiss 2021–2024 display mosaics. The count
ceiling is the two-satellite maximum of 2021 with headroom for the three
satellites of 2026; one count range is shared by both products, so that RTC
seeing more acquisitions than COH12 stays visible rather than being normalised
away. CQM is one-sided and its range is the measured one, not a symmetric one:
0 dB has no physical meaning in the composites. They are Swiss numbers and are
re-derived before the Alps.

## Related

`glace-ch-tiles` is the archive these are derived from: same measurements at
10 m in each tile's own UTM zone, one item per (tile, year). Pair on the year,
not on geometry.
