<p align="center">
  <br />
  <img src="https://data.source.coop/giuz/glace-alps/_assets/glace-wordmark.svg" alt="GLACE" width="350">
  <br />
</p>

<h1 align="center">GLACE Alps</h1>

<p align="center">
  Sentinel-1 interferometric coherence and backscatter composites of the European Alps
  <br />
  <br />
  <a href="https://lqgentner.github.io/glace-viewer">🗺️ Web map</a>
  ·
  <a href="https://browser.portolan-sdi.org/#/external/data.source.coop/giuz/glace-alps/catalog.json">🧭 Portolan/STAC browser</a>
  ·
  <a href="https://github.com/lqgentner/glace-catalog">🏷️ Metadata repository</a>
  ·
  <a href="https://github.com/lqgentner/glace-production">⚙️ Production pipeline</a>
</p>

Annual summer composites of Sentinel-1 12-day repeat-pass interferometric
coherence (COH12) and radiometrically terrain-corrected backscatter (RTC) for
the Alpine Convention perimeter, 2015–2025. The data is available as
Cloud-Optimized GeoTIFFs (COGs), described by a STAC catalog.

## Collections

| Collection | One item per | Grid | Use it for |
| --- | --- | --- | --- |
| [Tiles](tiles/) | (MGRS square, year) | 10 m, UTM 31N/32N/33N | Detailed analysis, machine learning |
| [Mosaics](mosaics/) | year | 40 m, ETRS89-LAEA | Regional analysis, inspection, time series |

Both represent the same source composites on different grids. The tiles hold
the main product for scientific analysis. The mosaics put every year on one
fixed equal-area grid, so years can be stacked and compared without alignment.
The mosaics collection also includes PMTiles archives for web maps.

To explore the data visually, open the [GLACE Viewer](https://lqgentner.github.io/glace-viewer).

## Get started

All files are publicly readable on Source Cooperative, with no API key required,
over HTTPS (`https://data.source.coop/giuz/glace-alps/`) or S3 (`s3://eu-central-1.opendata.source.coop/giuz/glace-alps/`). Each collection
provides a searchable item index, `items.parquet`, in STAC-GeoParquet format,
so you can find data without fetching each item's JSON individually.

This example uses [rustac](https://stac-utils.github.io/rustac-py/) to find tiles
for an area and year, then `rasterio`
to read coherence values from the first matching tile. Install both packages
before running it.

```python
import rasterio
import rustac

items = rustac.search_sync(
    "https://data.source.coop/giuz/glace-alps/tiles/items.parquet",
    bbox=[7.9, 46.4, 8.2, 46.6],
    datetime="2025-01-01/2026-01-01",
)
if not items:
    raise ValueError("No tiles found for this area and year")

url = items[0]["assets"]["coh12_vv"]["href"]
with rasterio.open(url) as src:
    coherence = src.read(1, masked=True)
print(coherence.shape, coherence.mean())
```

This reads the whole matching tile; the search bounding box selects items,
it does not clip their rasters.

For more workflows, see [building a data cube](tiles/README.md#build-a-data-cube),
[opening tiles in QGIS](tiles/README.md#open-the-tiles-in-qgis), and
[working with mosaics](mosaics/README.md).

## Products

Each tile or annual mosaic contains twelve analysis rasters: coherence
and backscatter in VV and VH polarization, plus an observation count and a
composite quality map for each measurement. See the [asset table](tiles/README.md#the-twelve-assets)
for filenames, data types and nodata values.

| Quantity | Stored values | Interpretation and display |
| --- | --- | --- |
| Coherence (`coh12_*`) | Dimensionless, linear | Read directly; no decibel conversion |
| Backscatter (`rtc_*`) | Linear power | Use `10*log10(x)` to display in decibels |
| Observation count (`*_qa_num`) | Integer on tiles; mean count on mosaics | Number of contributing observations |
| Composite quality map (`*_qa_cqm`) | Linear ratio | Describes how well the pixel was resolved; use `10*log10(x)` for decibel display |

The coherence and backscatter rows refer to the measurements; the QA suffixes
identify their accompanying quality layers.

Use **COGs for analysis**. The `_viz.pmtiles` archives contain lossy 8-bit
value codes over a fixed range per layer, which a web map colors on the
client. Backscatter and composite quality maps are encoded in decibels;
coherence remains linear. These archives are for visualization only. Every analysis raster carries its description and
processing record in its GeoTIFF tags.

## Composite processing

Sentinel-1 SLC bursts are processed to 12-day repeat-pass coherence and
terrain-corrected gamma-nought backscatter, then composited over each year's
90-day summer window with **local resolution weighting** after
[Small et al. (2022)](https://doi.org/10.1109/TGRS.2021.3055562). Local
resolution depends on terrain slope and aspect relative to the radar viewing
geometry. At each pixel, observations with finer resolution in map coordinates
receive greater weight. Each year's window is chosen by minimizing MODIS NDSI
over the target area.

See the [production pipeline](https://github.com/lqgentner/glace-production) for processing details.

## License and citation

**CC-BY-4.0.** Contains modified Copernicus Sentinel data.

A dataset citation will be added with the preprint. Until then, cite the
specific [metadata repository release](https://github.com/lqgentner/glace-catalog/releases)
you used. Catalog URLs are not persistent citation identifiers.

## Feedback and contributions

Report problems or suggest improvements through the
[issue tracker](https://github.com/lqgentner/glace-catalog/issues). The catalog's metadata is
maintained in the [metadata repository](https://github.com/lqgentner/glace-catalog).

For programmatic agent guidance, see [`AGENTS.md`](AGENTS.md).
