# GLACE mosaics — annual composites on a fixed WebMercator grid

One item per year, each carrying twelve single-band analysis rasters on the
WebMercatorQuad grid at zoom 13 (19.109 m, about 13.2 m on the ground at 46.5°N),
plus a set of PMTiles archives for drawing them.

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

## Two ways to consume it

**To draw a map**, use the PMTiles under `pmtiles/{year}/`. They are registered
as `rel: "pmtiles"` links on this collection, with MapLibre GL styles as `style`
assets — load the style directly and it works:

```js
const protocol = new pmtiles.Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);
new maplibregl.Map({
  container: "map",
  style: "https://data.source.coop/lqgentner/glace-ch/mosaics/styles/default.json",
});
```

**To read values**, use the COGs. The `renders` block on this collection carries
the fixed stretch, colour ramp and — for backscatter and the quality metric —
the decibel `expression`, so a titiler-compatible client draws them correctly
without a style file:

```
https://titiler.xyz/cog/preview.png
  ?url=.../mosaics/2024/rtc_vv.tif
  &expression=10*log10(b1)&rescale=-18.5,-5&colormap_name=gray
```

The stretch is fixed rather than per-year on purpose: a percentile stretch
recomputed each year would give every year its own scale and turn a real change
in coherence into no visible change at all.

## Building a cube

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

## The two products are not interchangeable

`{year}/rtc_vv.tif` is linear power. `pmtiles/{year}/rtc_vv.pmtiles` is the same
measurement rendered: 8-bit, pre-stretched, and in **decibels**. They are kept in
separate directories rather than side by side for exactly that reason — the
names would otherwise differ by one extension while the quantities differ by a
logarithm.

Coherence is not converted: `coh12_*` archives are linear values under a fixed
stretch. Only `rtc_*` and `*_qa_cqm` are in decibels, because those span decades
of linear power and a linear 8-bit stretch would collapse them.

## Caveat: the count layers are means

`*_qa_num` on a mosaic is a **mean** of contributing observations, float32, not
an integer count. About 3.6 source pixels fall in each output pixel; `nearest`
would discard three in four and `max` is biased high, so they are averaged like
everything else and described as a mean. The 10 m tiles keep the integer count.

## License

**CC-BY-4.0.** Contains modified Copernicus Sentinel data.

## Provenance

Derived in this repository from the tiles collection by a VRT chain: per-zone
mosaic, warp to the canonical grid, union, then one streaming COG copy. No pixel
is resampled twice. The PMTiles are tiled from the published mosaics, so the
level a client reads at max zoom is a copy rather than a second resampling.
Upstream provenance is the tiles collection's.
