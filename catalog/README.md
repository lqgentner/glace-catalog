# GLACE (preliminary) — Sentinel-1 Glacier Coherence and Backscatter

> **This is a preliminary release, not the GLACE dataset.** It exists to
> exercise the publication pipeline while the full dataset is produced. **The
> dataset is GLACE Alps**, over the Alpine Convention perimeter, which uses this
> same pipeline and supersedes this catalog. These URLs are provisional — do not
> cite them, and expect the coverage, the years and the identifiers to change.

Annual summer composites of Sentinel-1 12-day repeat-pass InSAR coherence and
radiometrically terrain-corrected backscatter over Alpine glaciers, as
Cloud-Optimized GeoTIFFs with a STAC catalog over them.

Two collections:

| collection | one item per | grid | what it is for |
| --- | --- | --- | --- |
| [tiles](tiles/) | (MGRS tile, year) | 10 m, MGRS / UTM | analysis, machine learning, per-tile reads |
| [mosaics](mosaics/) | year | 19.109 m, WebMercatorQuad z13 | mapping, inspection, change over time |

Both cover the same pixels. The tiles are the archive; the mosaics are one warp
of it onto a fixed grid that every year shares, so the series stacks and
differences without an alignment step.

## What the measurements are

Sentinel-1 SLC bursts are processed to 12-day repeat-pass coherence (COH12) and
radiometrically terrain-corrected gamma-nought backscatter (RTC), then
composited over a summer window with **local resolution weighting**: each
acquisition contributes in proportion to how well it resolves the pixel, so
layover and shadow are down-weighted rather than averaged in. Every measurement
carries two QA layers — the contributing observation count and the composite
quality metric, which is the weighting's own record of how well each pixel was
resolved.

Values are **linear throughout**: backscatter in linear power, the quality
metric as a linear ratio. The decibel conversion is declared in each
collection's `renders` block rather than applied to stored pixels, as CEOS-ARD
PFS SAR item 3.1 asks.

## How to draw it, and how to read it

Two paths, both zero-infrastructure:

- **Map** — PMTiles archives with MapLibre styles, registered on the mosaics
  collection. One HTTP range request per visible tile.
- **Analysis** — the COGs themselves, with the colour ramp and value range
  declared in `renders` so a client colorizes at draw time.

## License

**CC-BY-4.0.** Contains modified Copernicus Sentinel data. Cite the dataset and
the Copernicus programme.

## Provenance

Produced at the University of Zurich, Department of Geography, from Copernicus
Sentinel-1 SLC data distributed by the Copernicus Data Space Ecosystem. Burst
extraction, coherence estimation, terrain correction and compositing are the
`s1bursts` pipeline; every raster carries the full processing record in its own
GeoTIFF tags, so a file separated from this catalog still says how it was made.

## Corrections

The catalog metadata lives in a git repository:
[lqgentner/glace-catalog](https://github.com/lqgentner/glace-catalog). Wrong
metadata is a pull request, not an email.
