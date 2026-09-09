# GLACE mosaics — annual composites on a fixed equal-area grid

One item per year, each carrying twelve single-band analysis rasters on
ETRS89-LAEA (EPSG:3035) at 40 m, plus fourteen PMTiles archives for drawing
them, beside the rasters in the same item directory.

## Why this grid

Two properties, both deliberate:

- **It is equal-area.** A pixel covers the same 1 600 m² everywhere, so a glacier
  area or an area change is a pixel count. On WebMercator it is not: a nominal
  19.109 m pixel is about 13.2 m on the ground at 46.5°N and the factor varies
  with latitude, so every area statistic would need a correction this catalog
  does not declare.
- **It is the same every year.** The extent is a hard-coded constant, not
  recomputed from whichever tiles happen to exist, so every year has identical
  `proj:shape` and `proj:transform`. Stacking and differencing are a read.

Drawing does not use this grid at all: the PMTiles archives are tiled on
WebMercatorQuad z13, resampled from the same 10 m tiles rather than from these
mosaics, so a map never pays for a second warp.

## Two ways to consume it

**To draw a map**, use the `{stem}_viz.pmtiles` archives in each year's item
directory, beside the COGs they render. They are assets of that item with the
`visual` role, and registered on this collection as `rel: "pmtiles"` links, with
one MapLibre GL style per year as `style` assets — load a style directly and it
works:

```js
const protocol = new pmtiles.Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);
new maplibregl.Map({
  container: "map",
  style: "https://data.source.coop/lqgentner/glace-ch/mosaics/styles/2024.json",
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

`{year}/rtc_vv.tif` is linear power. `{year}/rtc_vv_viz.pmtiles` is the same
measurement rendered: 8-bit, pre-stretched, and in **decibels**. They sit side by
side, which is the pairing to see, and the `_viz` suffix is what keeps them
apart: without it the names would differ by one extension while the quantities
differ by a logarithm.

Coherence is not converted: `coh12_*` archives are linear values under a fixed
stretch. Only `rtc_*` and `*_qa_cqm` are in decibels, because those span decades
of linear power and a linear 8-bit stretch would collapse them.

## Caveat: the count layers are means

`*_qa_num` on a mosaic is a **mean** of contributing observations, float32, not
an integer count. Sixteen 10 m source pixels fall in each 40 m output pixel;
`nearest` would discard fifteen of them and `max` is biased high, so they are
averaged like everything else and described as a mean. The 10 m tiles keep the
integer count.

## License

**CC-BY-4.0.** Contains modified Copernicus Sentinel data.

## Provenance

Derived in this repository from the tiles collection by a VRT chain: per-zone
mosaic, warp to the canonical grid, union, then one streaming COG copy. No pixel
is resampled twice — the web-map archives run the same chain onto the display
grid rather than being tiled from these files. Upstream provenance is the tiles
collection's.
