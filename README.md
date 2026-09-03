# glace-catalog

Metadata, build pipeline and publication for the **GLACE** STAC catalogs —
annual Sentinel-1 coherence and backscatter composites over Alpine glaciers,
published to [Source Cooperative](https://source.coop/lqgentner) as a
[Portolan](https://www.portolan-sdi.org/) catalog.

The catalog metadata lives in git so that it has a change history, a review
step, and a way for someone who spots a wrong license or a bad description to
open a pull request instead of finding an email address. The data does not: it
is built onto object storage and referenced by URL.

```
catalog/          the published tree — STAC JSON, README.md, AGENTS.md
src/glace_catalog/ the library that builds it
scripts/          the build commands
tools/            publish.py (metadata) and upload_data.py (bytes)
tests/            the gates, incl. rashid conformance
docs/             MIGRATION.md (living), conformance.md
```

## Quick start

```bash
uv sync --all-groups
python3 tests/run_all.py            # every gate
uv run rashid check catalog/        # Portolan conformance alone
```

Building a year needs credentials for the private source bucket:

```bash
uv run --env-file .env python3 scripts/publish-year.py 2024 --scope ch
uv run --env-file .env python3 scripts/finalize-catalog.py --scope ch
```

Publishing needs the Source Cooperative profile named in `catalog.publish.yaml`:

```bash
python3 tools/publish.py             # dry run, uploads nothing
python3 tools/publish.py --confirm   # metadata
python3 tools/upload_data.py --confirm  # the rasters and parquet
```

## Where things are

| what | where |
| --- | --- |
| what publishes, and to which bucket | `catalog.publish.yaml` |
| where the build reads and writes | `GLACE_SOURCE_ROOT`, `GLACE_STORE_ROOT` in `.env` |
| the transition from the old flat store | [`docs/MIGRATION.md`](docs/MIGRATION.md) |
| accepted validator findings | [`docs/conformance.md`](docs/conformance.md) |

Data never lands in this repository. `.gitignore` refuses `.tif` and `.parquet`,
and `tools/publish.py` only ever syncs `catalog/`.

## Contributing

Wrong metadata is a pull request. Run `python3 tests/run_all.py` before pushing —
a contribution that fails CI costs a round trip.

## License

Code: see `LICENSE`. The published data is CC-BY-4.0 and contains modified
Copernicus Sentinel data.
