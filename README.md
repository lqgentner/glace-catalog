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
  <a href="https://source.coop/lqgentner/glace-ch">💾 Dataset on Source Coop</a>
  ·
  <a href="https://browser.portolan-sdi.org/#/external/data.source.coop/lqgentner/glace-ch/catalog.json">🧭 Portolan/STAC browser</a>
  ·
  <a href="https://github.com/lqgentner/glace-production">⚙️ Production pipeline</a>
</p>

## About

This repository holds the metadata and the building and publishing pipeline for the the **GLACE** STAC catalog
published to [Source Cooperative](https://source.coop/lqgentner/glace-ch). The catalog complies with
the [Portolan SDI](https://www.portolan-sdi.org/) spec.

> **What is published today is `glace-ch`, a rehearsal.** It covers Switzerland
> and exists to exercise publication while the Alpine composites are produced.
> The dataset is **`glace-alps`**; every mention of Switzerland in this
> repository is placeholder text. [`docs/STATUS.md`](docs/STATUS.md) carries the
> swap checklist and what is still open.

The metadata of the catalog lives on GitHub so that its change history can be reviewed, updates can be published as releases, and errors can be raised with an issue.

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
uv run rashid check catalog/        # Verify Portolan conformance alone
```

For editing the metadata and building and publishing the store, see [AGENTS.md](AGENTS.md).

## Citing

Coming soon. Once the first release is out, the metadata will be archived on Zenodo with a corresponding DOI. Until then, please contact the author.

## License

The code is MIT, except for the vendored files listed in
[Third Party Notices](THIRD_PARTY_NOTICES.md), which stay under their own licenses.
See [`LICENSE`](LICENSE). The published data is CC-BY-4.0 and contains modified
Copernicus Sentinel data. The collection thumbnails are drawn over
[OpenStreetMap](https://www.openstreetmap.org/copyright) tiles and carry that credit
in the image.

## Comments and contributions

We welcome your feedback! Please raise an issue
[here](https://github.com/lqgentner/glace-catalog/issues).
