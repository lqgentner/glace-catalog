# GLACE mosaics – Sentinel-1 Composites on ETRS89-LAEA (40 m)

One item per year, each carrying twelve single-band analysis rasters on
ETRS89-LAEA (EPSG:3035) at 40 m, plus fourteen PMTiles archives
for drawing them, beside the rasters in the same item directory. Agents: read
[`AGENTS.md`](AGENTS.md).

The grid has two deliberate properties:

- **It is equal-area.** A pixel covers the same 1600 m² everywhere, so a
  glacier area or an area change is a pixel count.
- **It is the same every year.** The extent is a fixed constant, not recomputed
  from whichever tiles happen to exist, so every year has identical
  `proj:shape` and `proj:transform`. Stacking and differencing are a read.

**To read values**, use the COGs. The `renders` block on this collection carries
the fixed stretch, the color ramp and, for backscatter and the quality map, the
decibel `expression`, so a titiler-compatible client draws them correctly
without a style file:

```
https://titiler.xyz/cog/preview.png
  ?url=https://data.source.coop/lqgentner/glace-ch/mosaics/2024/rtc_vv.tif
  &expression=10*log10(b1)&rescale=-16.5,-4&colormap_name=gray
```

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

## PMTiles for web maps

In addition to the LAEA COGs for scientific analysis, PMTiles archives are
provided for web maps. They are tiled on WebMercatorQuad up to zoom
13, resampled from the MGRS tiles, and carry a `_viz` suffix to
mark them as renderings for visualization only. They are pre-rendered with the
[Scientific colour maps](https://doi.org/10.5281/zenodo.1243862)
([Crameri et al., 2020](https://doi.org/10.1038/s41467-020-19160-7)) over fixed
ranges, as their style files declare. To suit visualization, the backscatter
and composite quality map archives are converted to decibels, unlike the
analysis products, which come in linear units only. False-color RGB archives
(VV, VH, VV/VH) are provided as well.

**To draw a map**, use the `{stem}_viz.pmtiles` archives in each year's item
directory, beside the COGs they render. Load a style directly and it works:

```js
const protocol = new pmtiles.Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);
new maplibregl.Map({
  container: "map",
  style: "https://data.source.coop/lqgentner/glace-ch/mosaics/styles/2024.json",
});
```

## The two products are not interchangeable

`{year}/rtc_vv.tif` is linear power. `{year}/rtc_vv_viz.pmtiles` is the same
measurement rendered: 8-bit, pre-stretched, and in **decibels**. They sit side by
side, and the `_viz` suffix is what keeps them apart: without it the names would
differ by one extension while the quantities differ by a logarithm.

Coherence is not converted: `coh12_*` archives are linear values under a fixed
stretch. Only `rtc_*` and `*_qa_cqm` are in decibels, because backscatter spans
decades of linear power and the quality ratio spans decades too, and a linear
8-bit stretch would collapse either.

## License

**CC-BY-4.0.** Contains modified Copernicus Sentinel data.
