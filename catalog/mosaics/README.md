# GLACE mosaics — annual composites on a fixed WebMercator grid

One item per year, each carrying twelve single-band analysis rasters and two
false-colour display rasters, all on the WebMercatorQuad grid at zoom 13
(19.109 m, about 13.2 m on the ground at 46.5°N).

## Why this grid

Two properties, both deliberate:

- **It is the same every year.** The extent is a hard-coded constant, not
  recomputed from whichever tiles happen to exist, so every year has identical
  `proj:shape` and `proj:transform`. Stacking and differencing are a read.
- **It is the tile matrix.** Because the grid *is* WebMercatorQuad z13, the
  COG's internal overview levels land exactly on z12, z11 and below. Each file
  is its own tile pyramid: a client reading it zoomed out fetches a decimated
  overview instead of full-resolution pixels it would throw away, and at max
  zoom a tiler's warp is the identity.

## Analysis rasters versus display rasters

The twelve `data` and `quality` assets carry the measurements, in linear units,
and are what you read for anything quantitative.

The two `visual` assets — `coh12_rgb` and `rtc_rgb` — are renderings. Their
pixels are 8-bit display codes under a fixed stretch, not measurements, and
`rtc_rgb`'s channels are in **decibels** while every other raster here is
linear. Their internal overviews average in display space, which is right for
drawing and wrong for measuring. Read the float32 assets for anything else.

The stretch is fixed rather than per-year on purpose: a percentile stretch
recomputed each year would give every year its own scale and turn a real change
in coherence into no visible change at all.

## Reading it

```python
import odc.stac
import pystac

collection = pystac.Collection.from_file(
    "https://data.source.coop/lqgentner/glace-ch/mosaics/collection.json"
)
cube = odc.stac.load(
    list(collection.get_items(recursive=True)),
    bands=["coh12_vv", "rtc_vv"],
    chunks={},
)
```

Every year shares one grid, so the result is a clean time axis with no
resampling step.

## Caveat: the count layers are means

`*_qa_num` on a mosaic is a **mean** of contributing observations, float32, not
an integer count. About 3.6 source pixels fall in each output pixel; `nearest`
would discard three in four and `max` is biased high, so they are averaged like
everything else and described as a mean. The 10 m tiles keep the integer count.

## License

**CC-BY-4.0.** Contains modified Copernicus Sentinel data.

## Provenance

Derived in this repository from the `glace-ch-tiles` collection by a VRT chain:
per-zone mosaic, warp to the canonical grid, union, then one streaming COG copy.
No pixel is resampled twice. Upstream provenance is the tiles collection's.
