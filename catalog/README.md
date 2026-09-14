<p align="center">
<a href="https://source.coop/lqgentner/glace-ch">
<img src="https://data.source.coop/lqgentner/glace-ch/_assets/glace-wordmark.svg" alt="GLACE" width="350">
</a>
</p>
<p align="center">
Annual local resolution weighted composites of Sentinel-1 coherence and backscatter over Swiss glaciers
</p>

---

- 🗺️ [Interactive web map](https://lqgentner.github.io/glace-viewer)
- 💾 Dataset on Source Cooperative (you are here)
- 🧭 [Portolan browser of the STAC catalog](https://browser.portolan-sdi.org/#/external/data.source.coop/lqgentner/glace-ch/catalog.json)
- ⚙️ Production pipeline on GitHub (coming soon)
- 🏷️ [Dataset metadata on GitHub](https://github.com/lqgentner/glace-catalog)

---

> **This is a preliminary release over the Swiss Alps, published to exercise the GLACE publication pipeline while the full dataset is produced. The dataset is GLACE Alps, over the Alpine Convention perimeter, which uses this same pipeline and supersedes this catalog. These URLs are provisional: do not cite them, and expect the coverage, the years and the identifiers to change.**

Annual summer composites of Sentinel-1 12-day repeat-pass interferometric
coherence (COH12) and radiometrically terrain-corrected backscatter (RTC) over
the Alpine Convention perimeter, 2021–2024, as Cloud-Optimized GeoTIFFs with a
STAC catalog over them. Agents: read [`AGENTS.md`](AGENTS.md).

## Collections

| collection | one item per | grid | what it is for |
| --- | --- | --- | --- |
| [tiles](tiles/) | (MGRS square, year) | 10 m, UTM 32N | analysis, machine learning |
| [mosaics](mosaics/) | year | 40 m, ETRS89-LAEA | inspection, large-scale analysis |

Both cover the same pixels. The tiles hold the main product for scientific
analysis. The mosaics serve as overviews on one equal-area grid, where every
year shares the same extent, so the series stacks and differences without
alignment. The mosaics also carry the web-map product: one PMTiles archive per
layer and year, drawn by the [GLACE Viewer](https://lqgentner.github.io/glace-viewer).

## Data access

All files are anonymous-read on Source Cooperative, with no API key required,
over HTTPS (`https://data.source.coop/lqgentner/glace-ch/`) or S3
(`s3://eu-central-1.opendata.source.coop/lqgentner/glace-ch/`). Every collection publishes its items as one
STAC-GeoParquet file, `items.parquet`, which is the query path: one range
request instead of one HTTP fetch per item.

**Search the catalog.** [rustac](https://github.com/stac-utils/rustac-py)
searches the item mirror directly:

```python
import rustac

tiles = "https://data.source.coop/lqgentner/glace-ch/tiles/items.parquet"
items = rustac.search_sync(
    tiles,
    bbox=[7.9, 46.4, 8.2, 46.6],
    datetime="2024-01-01/2025-01-01",
)
print(len(items), items[0]["assets"]["coh12_vv"]["href"])
```

**Plot a mosaic.** Read a decimated overview rather than the full raster:

```python
import numpy as np
import rasterio
import matplotlib.pyplot as plt

url = "https://data.source.coop/lqgentner/glace-ch/mosaics/2024/rtc_vv.tif"
with rasterio.open(url) as src:
    power = src.read(1, out_shape=(src.height // 8, src.width // 8), masked=True)
    extent = [src.bounds.left, src.bounds.right, src.bounds.bottom, src.bounds.top]
plt.imshow(10 * np.log10(power), cmap="gray", vmin=-16.5, vmax=-4, extent=extent)
plt.colorbar(label="RTC VV backscatter (dB)")
```

**Build a data cube.** Find the tiles in an area of interest and load them with
[odc-stac](https://odc-stac.readthedocs.io/); every year becomes one time step:

```python
import odc.stac
import pystac

bbox = [7.9, 46.4, 8.2, 46.6]
items = rustac.search_sync(tiles, bbox=bbox)
cube = odc.stac.load(
    [pystac.Item.from_dict(item) for item in items],
    bands=["coh12_vv", "rtc_vv"],
    bbox=bbox,
    chunks={},
)
change = cube.coh12_vv.sel(time="2024") - cube.coh12_vv.sel(time="2021")
```

**Open the tiles in QGIS.** Build a virtual raster of one year and product
to stream the COGs:

```python
from osgeo import gdal

items = rustac.search_sync(tiles, bbox=bbox, datetime="2024-01-01/2025-01-01")
gdal.BuildVRT(
    "coh12_vv_2024.vrt",
    ["/vsicurl/" + item["assets"]["coh12_vv"]["href"] for item in items],
)
```

Tiles from different UTM zones need one VRT per zone; the item mirror's
`proj:code` column says which.

## About the dataset

Sentinel-1 SLC bursts are processed to 12-day repeat-pass coherence and
terrain-corrected gamma-nought backscatter, then composited over each year's
90-day summer window with **local resolution weighting** after
[Small et al. (2022)](https://doi.org/10.1109/TGRS.2021.3055562): each
acquisition contributes in proportion to how well it resolves the pixel, so
layover and shadow are down-weighted rather than averaged in. Every measurement
carries two QA layers, the number of contributing observations and the
composite quality map, which is the weighting's own record of how well each
pixel was resolved.

Stored values are **linear throughout**; use `10*log10(x)` to convert to
decibels. Every raster carries its description and processing record in its
GeoTIFF tags.

## License

**CC-BY-4.0.** Contains modified Copernicus Sentinel data.

## Cite

A citation will be added with the preprint. Until then, cite the repository
release at [lqgentner/glace-catalog](https://github.com/lqgentner/glace-catalog).

## Comments and contributions

The metadata of this catalog lives on
[GitHub](https://github.com/lqgentner/glace-catalog). Issue reports are welcome
[there](https://github.com/lqgentner/glace-catalog/issues).
