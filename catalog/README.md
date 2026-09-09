# GLACE Switzerland (preliminary) – Sentinel-1 Glacier Coherence and Backscatter

> **This is a preliminary release over the Swiss Alps, published to exercise the GLACE publication pipeline while the full dataset is produced. The dataset is GLACE Alps, over the Alpine Convention perimeter, which uses this same pipeline and supersedes this catalog. These URLs are provisional: do not cite them, and expect the coverage, the years and the identifiers to change.**

Annual summer composites of Sentinel-1 12-day repeat-pass interferometric
coherence (COH12) and radiometrically terrain-corrected backscatter (RTC) over
the Alpine Convention perimeter, 2021–2024, as Cloud-Optimized GeoTIFFs with a
STAC catalog over them. Agents: read [`AGENTS.md`](AGENTS.md).

## Collections

| collection | one item per | grid | what it is for |
| --- | --- | --- | --- |
| [tiles](tiles/) | (MGRS square, year) | 10 m, MGRS / UTM | analysis, machine learning, per-tile reads |
| [mosaics](mosaics/) | year | 40 m, ETRS89-LAEA (EPSG:3035) | mapping, inspection, change over time |

Both cover the same pixels. The tiles are the archive; the mosaics are one
resampling of it onto a fixed equal-area grid that every year shares, so the
series stacks and differences without an alignment step. The mosaics also carry
the web-map product: one PMTiles archive per layer and year, drawn by the
[GLACE Viewer](https://lqgentner.github.io/glace-viewer).

## What the measurements are

Sentinel-1 SLC bursts are processed to 12-day repeat-pass coherence and
terrain-corrected gamma-nought backscatter, then composited over each year's
90-day summer window with **local resolution weighting** after
[Small et al. (2022)](https://doi.org/10.1109/TGRS.2021.3055562): each
acquisition contributes in proportion to how well it resolves the pixel, so
layover and shadow are down-weighted rather than averaged in. Every measurement
carries two QA layers, the number of contributing observations and the
composite quality map, which is the weighting's own record of how well each
pixel was resolved.

Stored values are **linear throughout**. Backscatter is linear power and the
quality map a linear ratio; the decibel conversion is declared in each
collection's `renders` block rather than applied to the pixels, as CEOS-ARD PFS
SAR item 3.1 asks.

## License

**CC-BY-4.0.** Contains modified Copernicus Sentinel data. Cite the
dataset and the Copernicus programme.

## Provenance

Produced at the University of Zurich, Department of Geography, from Copernicus
Sentinel-1 SLC data distributed by the Copernicus Data Space Ecosystem. Every
raster carries its full processing record in its own GeoTIFF tags, so a file
separated from this catalog still says how it was made.

## Corrections

The catalog metadata lives in a git repository:
[lqgentner/glace-catalog](https://github.com/lqgentner/glace-catalog).
Wrong metadata is a pull request, not an email.
