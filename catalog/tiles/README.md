# GLACE tiles – Sentinel-1 Composites on MGRS squares (10 m)

Each item contains one year of data for a 10 km MGRS square: twelve
single-band Cloud-Optimized GeoTIFFs (COGs) at 10 m grid spacing, in the
square's UTM zone. Search the item index with
[rustac](https://stac-utils.github.io/rustac-py/), as shown below.
For agent guidance, see [`AGENTS.md`](AGENTS.md).

## The twelve assets

| asset key | title | dtype | nodata |
| --- | --- | --- | --- |
| `coh12_vv` | Coherence VV | float32 | NaN |
| `coh12_vv_qa_num` | Coherence VV – Contributing Observations | uint16 | 0 |
| `coh12_vv_qa_cqm` | Coherence VV – Composite Quality Map | float32 | NaN |
| `coh12_vh` | Coherence VH | float32 | NaN |
| `coh12_vh_qa_num` | Coherence VH – Contributing Observations | uint16 | 0 |
| `coh12_vh_qa_cqm` | Coherence VH – Composite Quality Map | float32 | NaN |
| `rtc_vv` | Backscatter VV | float32 | NaN |
| `rtc_vv_qa_num` | Backscatter VV – Contributing Observations | uint16 | 0 |
| `rtc_vv_qa_cqm` | Backscatter VV – Composite Quality Map | float32 | NaN |
| `rtc_vh` | Backscatter VH | float32 | NaN |
| `rtc_vh_qa_num` | Backscatter VH – Contributing Observations | uint16 | 0 |
| `rtc_vh_qa_cqm` | Backscatter VH – Composite Quality Map | float32 | NaN |

Each asset key is its filename without `.tif`. Dates are recorded in the STAC
item and GeoTIFF tags.

## Reading the data

Use `items.parquet` to search all tiles and years in one file. Rustac
searches this static STAC-GeoParquet index directly. Install it with
`pip install rustac`.

```python
import rustac

items = rustac.search_sync(
    "https://data.source.coop/giuz/glace-alps/tiles/items.parquet",
    bbox=[7.9, 46.4, 8.2, 46.6],
    datetime="2026-01-01/2027-01-01",
)
for item in items:
    print(item["id"], item["assets"]["coh12_vv"]["href"])
```

The index is generated from the individual STAC item JSON files, which remain
the authoritative records.

## Build a data cube

Search an area and load its tiles with
[odc-stac](https://odc-stac.readthedocs.io/) to compare years:

```python
import odc.stac
import pystac
import rustac

tiles = "https://data.source.coop/giuz/glace-alps/tiles/items.parquet"

bbox = [7.9, 46.4, 8.2, 46.6]
items = rustac.search_sync(tiles, bbox=bbox)
cube = odc.stac.load(
    [pystac.Item.from_dict(item) for item in items],
    bands=["coh12_vv", "rtc_vv"],
    bbox=bbox,
    chunks={},
)
change = cube.coh12_vv.sel(time="2026") - cube.coh12_vv.sel(time="2015")
```

## Open the tiles in QGIS

Create a virtual raster for one year and measurement. QGIS then streams only
the parts of the COGs it displays:

```python
from osgeo import gdal
import rustac

tiles = "https://data.source.coop/giuz/glace-alps/tiles/items.parquet"
bbox = [7.9, 46.4, 8.2, 46.6]

items = rustac.search_sync(tiles, bbox=bbox, datetime="2026-01-01/2027-01-01")
gdal.BuildVRT(
    "coh12_vv_2026.vrt",
    ["/vsicurl/" + item["assets"]["coh12_vv"]["href"] for item in items],
)
```

Tiles from different UTM zones need one VRT per zone; the index's `proj:code`
column gives each tile's zone.

Open the resulting `.vrt` file as a raster layer in QGIS.

## Tile grid

`grid.pmtiles` holds every tile of any year as a polygon in the vector layer
`mgrs`, with its MGRS code and glacier fractions: the share of the tile within
RGI7-C-11 glacier outlines, plain and buffered by 250 m.
`styles/grid.json` draws it, each tile filled by its buffered fraction.

## Units

- Coherence (`coh12_vv`, `coh12_vh`) is dimensionless and linear.
- Backscatter (`rtc_vv`, `rtc_vh`) is linear power. Use `10*log10(x)` to display
  it in decibels.
- Observation counts (`*_qa_num`) are integers; 0 is nodata.
- Composite quality maps (`*_qa_cqm`) are dimensionless linear ratios of local
  resolution. Higher values indicate better resolution. Use `10*log10(x)` for
  decibel display.

Average backscatter and quality ratios in linear units. Float rasters use NaN
for nodata.

## License

**CC-BY-4.0.** Contains modified Copernicus Sentinel data.
