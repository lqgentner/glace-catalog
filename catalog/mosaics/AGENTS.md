# GLACE mosaics — agent guide

One item per year on a fixed WebMercatorQuad z13 grid. Twelve analysis rasters
and two display rasters per year.

## The grid is the point

Every year shares one hard-coded extent, resolution and transform. Two things
follow, and both are what this collection exists for:

1. **Temporal stacking is free.** No reprojection, no alignment, no resampling.
2. **Each COG is a tile pyramid.** The grid is WebMercatorQuad z13, so internal
   overviews land on z12, z11 and below. A zoomed-out read fetches a decimated
   overview, not full-resolution pixels.

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

**PMTiles**, registered as `rel: "pmtiles"` links with MapLibre styles as
`style` assets, are what a map client uses. Their pixels are 8-bit display codes
under a fixed stretch, and the backscatter archives are in **decibels** while
every raster in this catalog is linear. They are renderings. Never read one for
a value.

**The COGs** carry the measurements. The `renders` block declares the stretch,
the ramp and the decibel `expression`, so a client can colorize them at draw
time and get the same picture with the numbers still underneath.

## `*_qa_num` is a mean here

On the 10 m tiles it is an integer count of contributing acquisitions. Warping
to 19.109 m averages roughly 3.6 source pixels, so the mosaic band is a float32
**mean** and is named `num_obs_mean`. It is not the same quantity as the tile
band of the same asset key.

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

The QA stretches (`*_qa_num` 0–30, `*_qa_cqm` ±6 dB) are **provisional** —
internally consistent across years but not yet checked against rendered tiles.

## Related

`glace-ch-tiles` is the archive these are derived from: same measurements at
10 m in each tile's own UTM zone, one item per (tile, year). Pair on the year.
