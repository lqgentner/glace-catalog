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
the Alpine Convention perimeter, 2015–2026. The data is available as
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

All files are public on Source Cooperative and can be read anonymously over
HTTPS (`https://data.source.coop/giuz/glace-alps/`) or S3 (`s3://eu-central-1.opendata.source.coop/giuz/glace-alps/`). Each collection
provides a searchable item index, `items.parquet`, in STAC-GeoParquet format:
one file to search across all items.

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
    datetime="2026-01-01/2027-01-01",
)
if not items:
    raise ValueError("No tiles found for this area and year")

url = items[0]["assets"]["coh12_vv"]["href"]
with rasterio.open(url) as src:
    coherence = src.read(1, masked=True)
print(coherence.shape, coherence.mean())
```

The bounding box selects whole tiles, so this reads the full first tile.

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
| Coherence (`coh12_*`) | Dimensionless, linear | Display as stored |
| Backscatter (`rtc_*`) | Linear power | Use `10*log10(x)` to display in decibels |
| Observation count (`*_qa_num`) | Integer on tiles; mean count on mosaics | Number of contributing observations |
| Composite quality map (`*_qa_cqm`) | Linear ratio | Describes how well the pixel was resolved; use `10*log10(x)` for decibel display |

Use the **COGs for analysis**. The `_viz.pmtiles` archives are for web maps:
they hold lossy 8-bit value codes over a fixed range per layer, which the map
colors in the browser. In the archives, backscatter and composite quality maps
are in decibels and coherence is linear.

Every analysis raster carries its description and processing record in its
GeoTIFF tags.

## Usage notes

**Composite quality.** Lower `*_qa_cqm` values indicate a lower local resolution of the composite: the closer to 0, the fewer favourable viewing geometries contributed. In the validation, a random forest maps glaciers less accurately where the CQM is below 0.5 (linear, about −3 dB; AUC 0.87) than elsewhere (0.97). These pixels make up 3% of the pixels within 250 m of the glacier outlines and 1.5% of the glacier pixels. Mask or downweight them, or use the CQM as an additional model input.

**Coherence bias.** Coherence is estimated over a 35 × 7 (range × azimuth) window. The finite window biases the estimate upwards, so fully decorrelated surfaces do not reach 0: over large lakes, the mean is about 0.09. Absolute values therefore differ from products that use larger windows, such as the global seasonal Sentinel-1 coherence of [Kellndorfer et al. (2022)](https://doi.org/10.1038/s41597-022-01189-6). Do not compare values across products, and choose thresholds on GLACE itself.

**Low coherence outside glaciers.** Other surfaces also decorrelate over 12 days and can have similarly low coherence, such as snow patches, lakes and unstable slopes.

**Validated scope.** The validation covers glacier mapping in Switzerland only. Its scores do not describe glacier mapping performance in other parts of the Alps, or farther than 250 m from the glacier outlines. Other applications may also benefit from the composites, but were not validated.

**2015 coverage.** In 2015, coherence is missing on 91 MGRS squares in the north-central Alps (100 km squares 32TNT, 32TNU, 32TPT, 32TPU and 32UPU) because Sentinel-1 coverage was limited that summer. Two of them, 32TNT14 and 32TNT24, have no 2015 items. Every other year covers all squares.

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

Cite this version of the dataset as:

> Gentner, L. Q. (2026). GLACE Alps (Version 1.0) [Data set]. LaRS – Language Repository of Switzerland. https://doi.org/10.48656/60er-3j55

To refer to the dataset across versions, use
https://doi.org/10.48656/d4r9-fj97.

## Feedback and contributions

Report problems or suggest improvements through the
[issue tracker](https://github.com/lqgentner/glace-catalog/issues). The catalog's metadata is
maintained in the [metadata repository](https://github.com/lqgentner/glace-catalog).

For programmatic agent guidance, see [`AGENTS.md`](AGENTS.md).
