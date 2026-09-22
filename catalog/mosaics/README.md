# GLACE mosaics – Sentinel-1 Composites on ETRS89-LAEA (40 m)

One item per year, each carrying twelve single-band analysis rasters on
ETRS89-LAEA (EPSG:3035) at 40 m, plus fourteen PMTiles archives
for drawing them, beside the rasters in the same item directory. Agents: read
[`AGENTS.md`](AGENTS.md).

The grid has two deliberate properties:

- **It is equal-area.** A pixel covers the same 1600 m² everywhere, so a
  glacier area or an area change is a pixel count.
- **It is the same every year.** The extent is a fixed constant, so every year
  has identical `proj:shape` and `proj:transform`.

Use the COGs for analysis. The collection's `renders` block defines display
ranges, color ramps and decibel conversions for clients that support them.

## Building a cube

```python
import odc.stac
import pystac

collection = pystac.Collection.from_file(
    "https://data.source.coop/giuz/glace-alps/mosaics/collection.json"
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
mark them as 8-bit imagery for visualization only. Coherence remains linear. They are pre-rendered with the
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
  style: "https://data.source.coop/giuz/glace-alps/mosaics/styles/2025.json",
});
```

## License

**CC-BY-4.0.** Contains modified Copernicus Sentinel data.
