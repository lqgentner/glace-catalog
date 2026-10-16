<p align="center">
  <br />
  <img src="assets/glace-wordmark.svg" alt="GLACE" width="350">
  <br />
</p>

<h1 align="center">GLACE Catalog</h1>

<p align="center">
  Git-backed Portolan/STAC catalog for GLACE
  <br />
  Sentinel-1 interferometric coherence and backscatter composites of the European Alps
  <br />
  <br />
  <a href="https://lqgentner.github.io/glace-viewer">🗺️ Web map</a>
  ·
  <a href="https://source.coop/giuz/glace-alps">💾 Dataset on Source Coop</a>
  ·
  <a href="https://browser.portolan-sdi.org/#/external/data.source.coop/giuz/glace-alps/catalog.json">🧭 Portolan/STAC browser</a>
  ·
  <a href="https://github.com/lqgentner/glace-production">⚙️ Production pipeline</a>
</p>

## About

This repository holds the metadata of the **GLACE** STAC catalog on
[Source Cooperative](https://source.coop/giuz/glace-alps), and the pipeline
that builds and publishes it. The catalog follows the
[Portolan SDI](https://www.portolan-sdi.org/) spec.

> **Not yet published.** The metadata here describes **`glace-alps`**, ahead
> of its publication. [`docs/STATUS.md`](docs/STATUS.md) lists the open work.

Keeping the metadata on GitHub lets anyone review its history, cite a release
and report errors as issues.

```text
metadata/           the source: catalog.yaml, assets.yaml, the markdown templates
catalog/            the published tree, rendered from metadata/ and committed
src/glace_catalog/  the library that builds it
scripts/            the build commands, incl. render-catalog.py
tools/              sync_data.py, which publishes the built store
tests/              the gates, incl. rashid conformance
docs/               DESIGN.md, STATUS.md, UPSTREAM.md, conformance.md
```

## Quick start

```bash
uv sync --all-groups
uv run tests/run_all.py             # run all gates
uv run rashid check catalog/        # Portolan conformance only
```

To edit the metadata, or to build and publish the store, see [AGENTS.md](AGENTS.md).

## Citing

Cite version 1.0 of the dataset as:

> Gentner, L. Q. (2026). *GLACE Alps* (Version 1.0) [Data set]. LaRS –
> Language Repository of Switzerland. https://doi.org/10.48656/60er-3j55

To refer to the dataset across versions, use its canonical DOI,
https://doi.org/10.48656/d4r9-fj97. Both DOIs are registered once the dataset
is listed at LaRS.

## License

The code is MIT, except for the vendored files listed in
[Third Party Notices](THIRD_PARTY_NOTICES.md), which keep their own licenses.
See [`LICENSE`](LICENSE). The published data is CC-BY-4.0 and contains modified
Copernicus Sentinel data. The collection thumbnails are drawn over
[OpenStreetMap](https://www.openstreetmap.org/copyright) tiles and carry that credit
in the image.

## Comments and contributions

We welcome your feedback! Please raise an issue
[here](https://github.com/lqgentner/glace-catalog/issues).
