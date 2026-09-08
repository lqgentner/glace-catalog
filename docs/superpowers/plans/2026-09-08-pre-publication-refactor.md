# Pre-publication refactor of glace-catalog

**Spec:** the user's request of 2026-09-08 (refactor before the glace-ch rehearsal
build), the findings of the same day's investigation, and the decisions recorded
below. `docs/MIGRATION.md` is the design record the code must agree with; where this
plan and MIGRATION.md disagree, this plan wins and Task 5 updates MIGRATION.md.

## Context

The regenerated Swiss composites (s1bursts 0.5.0, 2021–2024) pass this repository's
manifest verification, so publication is unblocked on the data side. The build side
is not:

- `src/glace_catalog/` has no `__init__.py`, so mypy resolved every first-party import
  as `Any` and checked nothing across modules. With the file present, 14 errors
  surface, several of them runtime crashes in `scripts/publish-year.py` and
  `scripts/finalize-catalog.py`.
- Both `[tool.uv.sources]` editable overrides are obsolete: freezebase 0.7.0 is on
  PyPI with everything imported here, and this repository never imports s1bursts.
- The Source Cooperative upload path in `catalog.publish.yaml` is wrong for the
  credentials in use, and the vendored uploaders cannot stream from the UZH bucket.
- The docs describe the composites as "being regenerated", record
  `manifest_version = 1` (the producer writes 2), and MIGRATION.md has grown to 946
  lines of which a large part is superseded reasoning.

## Global constraints

- **Gates:** `uv run python3 tests/run_all.py` must pass at the end of every task.
  `tests/test_lint.py` runs `ruff format --check`, `ruff check` and (after Task 1)
  `pyrefly check`. A missing tool fails, never skips.
- **Vendored code:** `tools/publish.py`, `tests/test_publish.py`,
  `tests/test_links.py`, `tests/test_stac_valid.py`, `tests/test_conformance.py`
  come from `portolan-sdi/portolan-catalog-template` and stay byte-close to upstream:
  excluded from ruff and pyrefly, never reformatted. (`tools/upload_data.py` and
  `tests/test_upload_data.py` are deleted in Task 2.)
- **No imports between this repository and `deep-glacier-mapping`** (M-30).
- **Edit the generator, not generated output.** Nothing under `catalog/tiles/{year}/`
  or `catalog/mosaics/{year}/` is ever hand-written.
- **The committed style is the single source of truth for display constants**
  (M-27). No stretch, ramp or range may be copied into Python.
- **Structural links relative, asset hrefs absolute https** (M-13). The root `self`
  link is written at publication, never committed.
- **JSON canonical form:** two-space indent, `ensure_ascii=False`, one trailing
  newline (`tests/test_data_format.py`).
- Commit messages follow the repository's existing style: `type: lower-case
  sentence` (`feat:`, `fix:`, `refactor:`, `docs:`, `build:`), no trailing period.
  End every commit message with the attribution block the session provides.
- Do not run `publish-year.py`, `finalize-catalog.py` or any uploader against a real
  bucket inside a task. Tests are offline; the end-to-end run is the controller's.

## Decisions taken 2026-09-08 (bind every task)

| # | decision |
| --- | --- |
| R-1 | `src/glace_catalog/__init__.py` exists (a real package, not a namespace package). |
| R-2 | pyrefly `strict` replaces mypy. Configuration mirrors `../s1bursts/pyproject.toml`: `[tool.pyrefly]` with `project-includes`, `search-path = [".", "src"]`, `preset = "strict"`; vendored files in `project-excludes`. `pyrefly>=1.2.0` in the `dev` group; mypy removed. Typing stubs (`pandas-stubs`, `types-*`) stay. |
| R-3 | `build_mosaic_item` loses its `pmtiles` parameter and the item-level `rel: "pmtiles"` links. PMTiles are registered at collection level only (M-19, M-22, `PORTO-FMT-011`); `finalize-catalog` already does that. |
| R-4 | Both `[tool.uv.sources]` overrides are removed. `freezebase[s3]>=0.7,<0.8`. `uv.lock` regenerated. |
| R-5 | The old UZH prefix `s3://s1-bursts/data/glace-ch` is being deleted by the user; the `ch` scope's default root stays that value and the fresh build lands there once it is empty. |
| R-6 | The staging directory is dropped. Publication streams object-to-object from `GLACE_STORE_ROOT` on the UZH gateway to Source Cooperative through `tools/sync_data.py`; `tools/upload_data.py` and its test are deleted. Verified 2026-09-08: a `get_object` body handed to `upload_fileobj` moved a 1.66 MB COG in 0.8 s with a matching single-part ETag. |
| R-7 | Source Cooperative is addressed through its S3 proxy. The `source-coop` AWS profile carries `endpoint_url = https://data.source.coop`; against it the **bucket is the account** (`lqgentner`) and the **key starts with the product** (`glace-ch/...`). `write_prefix` becomes `s3://lqgentner/glace-ch`. The AWS-native bucket names (`eu-central-1.opendata.source.coop`) reject these credentials. |
| R-8 | `tools/publish.py` (vendored, syncs `catalog/` from git) is deleted too: the published tree is the **store**, whose collection documents are the tracked ones *patched* by `finalize-catalog` (year links, extent, PMTiles links). Syncing the tracked tree over it would strip those patches. `sync_data.py` publishes the whole store, documents included. |
| R-9 | Thumbnails are rendered from the latest year's `coh12_rgb.pmtiles` archive (false-colour coherence, the flagship product), so the image obeys the committed style by construction. Mosaics collection: the whole scope. Tiles collection: tile `32TMS24` (Aletsch). Both framed 3:2, PNG, matching the `thumbnail` asset the collections already declare. No chiitiler, no MapLibre: a raster PMTiles archive already holds rendered tiles. |
| R-10 | The root catalog gets a `rel: "icon"` link to `_assets/glace-wordmark.svg` (`image/svg+xml`, titled), copied from `../glace-viewer/assets/glace-wordmark.svg`. P-12 closes. |
| R-11 | No `.portolan/metadata.yaml`. It is the input of `portolan add`, which this repository does not use; the collection documents are hand-written source. A second copy of title, license and contact that nothing reads is drift waiting to happen. |
| R-12 | The item mirror satisfies `PTL-DAT-006/007/008/016` as rashid implements them (`rashid/data/checks.py::_check_geoparquet`, `_check_mirror`). P-2 closes. |
| R-13 | `RasterInfo.statistics` becomes a typed `BandStatistics` record with the five fields s1bursts writes; a missing field is a `ValueError` at manifest read, not a `KeyError` at publication. |
| R-15 | `uv.lock` is tracked. Dropping the editable overrides makes the dependency set resolvable from a checkout; without a committed lock a fresh clone still resolves to whatever PyPI serves that day. |
| R-16 | **The catalog is versioned by git tags and GitHub releases, each archived on Zenodo for a DOI.** Source Cooperative is not immutable, so a citation must name a release and a reader must be able to verify their bytes against it. One version string per release: `DATASET_VERSION` in `src/glace_catalog/stac/items.py` is the source of truth, the STAC `version` on both collections and every item equals it, and the git tag is `v{DATASET_VERSION}`. `file:checksum` on every item asset is what lets a reader check downloaded bytes against a cited release. Zenodo archives the repository snapshot (the metadata), never the data. Metadata for Zenodo comes from `CITATION.cff`. The `sci:doi` / `sci:citation` fields (STAC scientific extension) are added to the root catalog and both collections **after** the first release mints the concept DOI, because a published placeholder DOI is worse than none; until then STATUS.md carries it as an open item with the exact fields to fill. |
| R-14 | Docs: `docs/MIGRATION.md` is replaced by `docs/DESIGN.md` (the stable design and the decision table, no superseded reasoning) and `docs/STATUS.md` (what is done, what is open, the §11 swap checklist). `docs/UPSTREAM.md` shrinks to the three surviving on-disk contracts. `docs/superpowers/specs/2026-09-05-display-product-design.md` is deleted (folded into M-24/M-25). |

---

## Task 1 — Make the type gate real: pyrefly strict, package init, runtime fixes, dependency cleanup

**Files:** `src/glace_catalog/__init__.py` (new), `pyproject.toml`, `uv.lock`,
`tests/test_lint.py`, `src/glace_catalog/stac/items.py`, `src/glace_catalog/stac/tags.py`,
`src/glace_catalog/stac/store.py`, `src/glace_catalog/stac/save.py`,
`src/glace_catalog/mosaics.py`, `scripts/publish-year.py`, `scripts/finalize-catalog.py`,
`tests/test_manifest_contract.py`, `tests/test_items.py`, and whatever else pyrefly flags.

Steps, in order, each its own commit where it stands alone:

1. **`build: freezebase from PyPI, drop both editable overrides`** — R-4. Remove
   `[tool.uv.sources]` entirely. Floor `freezebase[s3]>=0.7,<0.8`. Delete the
   comment block that justified the overrides and the AGENTS.md sentence "Both
   first-party libraries are installed editable…" (replace with one line saying both
   are consumed as releases). Run `uv lock` and `uv sync --all-groups`; the lock
   must show `freezebase 0.7.x` from the registry and no s1bursts entry.
2. **`build: pyrefly strict replaces mypy`** — R-1, R-2. Create the empty
   `src/glace_catalog/__init__.py` with a one-line module docstring. In
   `pyproject.toml`: replace `mypy` with `pyrefly>=1.2.0` in `dev`; delete
   `[tool.mypy]`; add

   ```toml
   [tool.pyrefly]
   project-includes = ["src/glace_catalog", "scripts", "tools", "tests"]
   project-excludes = [<the same vendored files ruff excludes>]
   search-path = [".", "src"]
   preset = "strict"
   ```

   plus whatever `sub-config` the tests need (s1bursts relaxes
   `implicit-any-lambda` under `tests/**`; do the same only if a test needs it).
   In `tests/test_lint.py` replace the mypy invocation with `pyrefly check`; the
   gate's docstring and the comment about mypy reading its own `files` key change
   accordingly. Update the `pyproject.toml` comments that mention mypy.
3. **Fix every pyrefly finding** in non-vendored code. Measured 2026-09-08 with the
   config above: 71 errors, of which the vendored files account for about 25. The
   ones that are runtime bugs, and how to fix them:
   - `scripts/publish-year.py`: `build_mosaic_item(...)` is called without
     `pmtiles`. Per R-3, delete the `pmtiles` parameter, the
     `WEB_MAP_LINKS_EXTENSION_URI` append and the link loop from
     `build_mosaic_item`; delete the `pmtiles` docstring entry; adjust any test.
   - `scripts/finalize-catalog.py`: `layout.items_index` → `layout.tiles_index`;
     `layout.mosaics(year)` → `layout.mosaic_year(year)` (twice);
     `layout.thumbnail` → `layout.mosaics_thumbnail`. Do **not** redesign
     `render_thumbnail`; Task 3 replaces it. Make it type-correct only.
   - The `sorted(...)` union-attr findings in `finalize-catalog.py` (`suffix`,
     `stem`, `name` on a `SupportsDunderLT | SupportsDunderGT`): annotate the
     iterable so pyrefly knows it holds paths.
   - `implicit-any-type-argument`: give bare `dict`, `list`, `Sequence`, `Mapping`
     their type arguments. Prefer precise types over `Any`.
   - `missing-import`: for third-party packages without stubs that pyrefly cannot
     infer, use `ignore-missing-imports` in `[tool.pyrefly]` with a comment naming
     the package, as s1bursts does. Do not silence first-party imports.
   - Everything else: fix at the source. A `# pyrefly: ignore` needs a reason in the
     same comment and is a last resort.
4. **`refactor: typed band statistics`** — R-13. In `stac/tags.py` introduce a frozen
   dataclass `BandStatistics(minimum, maximum, mean, stddev, valid_percent)` of
   floats; `RasterInfo.statistics: BandStatistics | None`. `read_raster_info` builds
   it from the five `STATISTICS_*` tags (all five present → record; none → `None`;
   some → `ValueError` naming the file and the missing tags).
   `store.manifest_raster_info` builds it from the `stat_*` columns the same way.
   `items.py` writes `bands[].statistics` from the record (`minimum`, `maximum`,
   `mean`, `stddev`, `valid_percent`, matching the raster extension's field names).
   Update `tests/test_manifest_contract.py` and `tests/test_items.py` to construct
   the record.
5. **`refactor: one XML parser entry point in mosaics.py`** — the module imports
   `xml.etree.ElementTree as ET` for building VRT elements and
   `defusedxml.ElementTree as DefusedET` for parsing. Keep both roles but confine
   parsing to one small helper (`_parse_vrt(text: str) -> ET.Element`) with a
   docstring saying why the defused parser is used for input and the stdlib one
   for construction, so no caller ever sees two parsers. If only one parse site
   exists and it is already a helper, rename/document it and stop.
6. Run `uv run python3 tests/run_all.py`. All green, including
   `pyrefly check` reporting 0 errors.

**Tests:** existing gates plus: `tests/test_items.py` gains a check that the mosaic
item carries no `rel: "pmtiles"` link and does not declare the web-map-links
extension; `tests/test_manifest_contract.py` gains a check that a manifest missing one
`stat_*` value fails with a `ValueError` naming the column.

**Report contract:** status, commits, the `pyrefly check` summary line, and the count
of `# pyrefly: ignore` comments added (target: zero; list each with its reason).

---

## Task 2 — `tools/sync_data.py`: stream the store to Source Cooperative; retire the vendored uploaders

**Files:** `tools/sync_data.py` (new), `tests/test_sync_data.py` (new),
`catalog.publish.yaml`, `pyproject.toml` (ruff/pyrefly excludes, `tests/run_all.py`
if it enumerates gates), delete `tools/publish.py`, `tools/upload_data.py`,
`tests/test_publish.py`, `tests/test_upload_data.py`.

Decisions R-6, R-7, R-8 bind this task. Read them first.

**`catalog.publish.yaml`** after the task:

```yaml
write_prefix: s3://lqgentner/glace-ch      # bucket = account, key = product, via the profile's endpoint
public_base: https://data.source.coop/lqgentner/glace-ch
profile: source-coop
```

Drop `region`, `publish_dir` and `data_dir`; update the head comment to say the
profile's `endpoint_url` (the Source Cooperative S3 proxy) is what makes the bucket
the account name. Keep the note that the profile holds the credentials so no key
passes through the environment.

**`tools/sync_data.py`**, house style (numpy docstrings, full ruff rule set, pyrefly
strict), run as `uv run --env-file .env python tools/sync_data.py [--confirm]
[--force] [--prefix tiles/2024] [--workers N]`:

- **Source:** `glace_catalog.config.settings.store_root` (an S3 `UPath` on the UZH
  gateway; refuse a local path with a clear message — there is no staging any more).
  Listing: one recursive listing of the prefix (`store.list_objects_recursive`
  exists; reuse or extend it so it returns sizes). Skip any object whose final path
  segment starts with `.` (`.build-state.json`, `.items.parquet`).
- **Target:** `write_prefix` and `profile` from `catalog.publish.yaml`, parsed with
  `yaml.safe_load`; a `boto3.Session(profile_name=profile).client("s3")` with **no
  explicit endpoint** so the profile's `endpoint_url` applies. `write_prefix`
  splits into bucket and key prefix.
- **Change detection:** size from one remote listing versus size from the source
  listing; `--force` re-uploads everything. Never delete. Print a summary table
  (new / changed / unchanged / skipped-dotfile counts and total bytes) before
  uploading; without `--confirm` stop there.
- **Transfer:** per object, `get_object` on the source client and `upload_fileobj`
  on the target with `ContentType` from the extension. Content types: `.json` →
  `application/json`, `.md` → `text/markdown`, `.parquet` →
  `application/vnd.apache.parquet`, `.tif` →
  `image/tiff; application=geotiff; profile=cloud-optimized`, `.pmtiles` →
  `application/vnd.pmtiles`, `.png` → `image/png`, `.svg` → `image/svg+xml`,
  `.yaml` → `application/yaml`, otherwise `application/octet-stream`. A
  `ThreadPoolExecutor` sized by `--workers` (default 8). The source client must be
  built from the same profile and endpoint the `UPath` carries
  (`settings.storage_options`, or however `config.py` exposes it — read it).
- **Exit status:** non-zero if any transfer failed; failed keys listed at the end.

**Tests** (`tests/test_sync_data.py`, offline, house style, under `run_all.py`):
change-detection over synthetic listings (new / changed / unchanged / dotfile
skipped / `--force`), content-type mapping, `write_prefix` parsing, refusal of a
local store root, and the dry-run summary. Mock the two clients; never touch the
network.

**Cleanup:** delete the four vendored files; remove them from `extend-exclude`
and `project-excludes`; update `tests/run_all.py` if it lists gates by name; update
the `.gitignore` comment lines that mention `publish.py` if any; fix the README's
"Publishing" section to the single command (Task 5 rewrites the docs in full, so
keep the README edit to the command block and one sentence).

**Report contract:** status, commits, test summary, and the exact dry-run output
against a fixture listing.

---

## Task 3 — Thumbnails from the display archive, the catalog icon, and store-level collection assets

**Files:** `src/glace_catalog/thumbnails.py` (new), `tests/test_thumbnails.py` (new),
`scripts/finalize-catalog.py`, `src/glace_catalog/stac/store.py`,
`catalog/catalog.json`, `catalog/_assets/glace-wordmark.svg` (new),
`tests/test_links.py` is vendored — do not edit it.

Decisions R-9, R-10 bind this task.

**`thumbnails.py`:**

- `render_from_pmtiles(archive: Path | UPath, bbox_wgs84: tuple[float, float, float, float], *, width: int = 1200) -> PIL.Image.Image`.
  Opens the archive with the `pmtiles` package (installed, ≥3.7; read the header
  for `min_zoom`/`max_zoom`/tile type), picks the highest zoom at which the bbox
  spans ≤ `width` pixels, fetches the covering tiles, decodes them with Pillow
  (WEBP or PNG per the header), composites onto a transparent RGBA canvas, crops
  to the bbox in pixel space, and returns the image. Reading from S3 goes through
  `UPath.open("rb")` with a small range-read wrapper; do not download the archive.
- `frame_3_2(bbox) -> bbox`: widens the shorter side of a WGS84 bbox about its
  centre so the *projected* (WebMercator) aspect is 3:2. Never crops data.
- `mosaics_bbox(scope) -> bbox`: the scope's display-grid extent in WGS84
  (`scopes.py` already has the extent; convert).
- `tile_bbox(tile: str) -> bbox`: the MGRS tile's WGS84 bounds via
  `freezebase.mgrs.MGRSGeoBox` (see how `items.py` gets tile geometry).
- `THUMBNAIL_TILE = "32TMS24"` and `THUMBNAIL_LAYER = "coh12_rgb"` as module
  constants with a comment saying why (flagship product; Aletsch).
- `write_collection_thumbnails(layout, year) -> None`: renders both images from
  `layout.pmtiles(year) / "coh12_rgb.pmtiles"`, saves PNG to
  `layout.tiles_thumbnail` and `layout.mosaics_thumbnail`. Logs the size of each.

**`finalize-catalog.py`:** delete `render_thumbnail`, `THUMBNAIL_WIDTH` and
`THUMBNAIL_RANGES`; call `write_collection_thumbnails(layout, years[-1])` instead
(honouring `--skip-thumbnail`). Then **fill `file:size` and `file:checksum`** on the
three collection-level assets whose bytes now exist (`thumbnail`, `items`,
`glacier-fractions`) in the patched documents, from the store's bytes (multihash
`1220` + sha256, as the items do). Check whether `patch_collections` already does
this; if not, add it there, and if so leave it. This closes the `PTL-AST-003`
warning that `docs/conformance.md` says finalize fills.

**Icon:** copy `../glace-viewer/assets/glace-wordmark.svg` to
`catalog/_assets/glace-wordmark.svg`. In `catalog/catalog.json` add, after the
`agents` link:

```json
{ "rel": "icon", "href": "./_assets/glace-wordmark.svg", "type": "image/svg+xml", "title": "GLACE wordmark" }
```

Rename `StoreLayout.logo` to return `_assets/glace-wordmark.svg`. In
`write_documents`, copy `_assets/` from the source directory into the staged tree so
the icon reaches the store. Confirm `uv run rashid check catalog/` still reports 0
errors (`PTL-LNK-007/008` are the icon rules).

**Tests** (`tests/test_thumbnails.py`, offline): build a tiny PMTiles archive in a
temp dir (the `pmtiles` package can write one; two zoom levels, 2×2 WEBP tiles of
known solid colours), render a bbox, and assert the output size, aspect ratio and
that sampled pixels carry the expected colours; `frame_3_2` on a tall and a wide
bbox; `tile_bbox("32TMS24")` lies inside the Swiss extent. Plus a check in the
existing link gate output that the icon link validates (run
`uv run rashid check catalog/`).

**Report contract:** status, commits, test summary, the rashid summary line.

---

## Task 4 — The item mirror under the GeoParquet rules (P-2)

**Files:** `src/glace_catalog/stac/save.py`, `tests/test_mirror.py` (new),
`docs/conformance.md` is Task 5's — leave it.

Decision R-12 binds this task. Read `rashid/data/checks.py` in the project
environment: `_check_geoparquet` (ordering, per-row-group statistics, row-group
ceiling), `_rowgroup_stat_defects`, `_covering_bboxes`, `_bbox_struct`, and
`_check_mirror` (`PTL-DAT-016`). Those functions are the definition of done.

**Changes to `save.py`:**

- Every parquet this module writes (`write_stac_geoparquet`, `concat_indexes`,
  `index_from_items`) goes through one writer `_write_mirror(table, out_path)` that:
  - adds (or keeps) a `bbox` struct column `{xmin, ymin, xmax, ymax}` of float64
    derived from the geometry, and declares it in the GeoParquet `geo` metadata as
    `covering.bbox` for the primary geometry column;
  - sorts rows by a Hilbert index of the bbox centre over the table's total bounds
    (implement the 2-D Hilbert curve in ~30 lines; no new dependency);
  - writes with `row_group_size` ≤ 150 000 and column statistics on (so each row
    group carries min/max of the four bbox fields).
- `concat_indexes` keeps the schema metadata of the first part as now, then goes
  through the same writer, so the store-level mirror is ordered across years rather
  than concatenated in year order.
- `stac_geoparquet.arrow.to_parquet` may already add a `bbox` covering column —
  check the installed version's output and do not add a second one.

**Tests** (`tests/test_mirror.py`, offline): build 400 synthetic tile items on a
grid (ids, geometries, datetimes in the style of `tests/test_items.py`), write a
mirror, then assert with the rashid functions themselves (import them; they are the
gate) that ordering, row-group statistics and the ceiling pass, and that
`_check_mirror` finds every item and no divergence when handed the items and the
mirror. Assert that the `geo` metadata names the covering column. A second case
concatenates two per-year mirrors and checks the same.

**Report contract:** status, commits, test summary, and the rashid function names the
test calls.

---

## Task 6 — Release model: tracked lock, version gate, citation metadata

**Files:** `.gitignore`, `uv.lock` (now tracked), `CITATION.cff` (new),
`tests/test_version.py` (new), `src/glace_catalog/stac/items.py` (docstring of
`DATASET_VERSION` only), `.github/workflows/*.yml` if a release workflow is wanted
(see below).

Decisions R-15 and R-16 bind this task.

1. **`build: track uv.lock`** — remove the `uv.lock` line from `.gitignore` (and the
   comment that justified it, if any), `git add uv.lock`, commit. Nothing else in
   that commit.
2. **`CITATION.cff`** at the repository root, CFF 1.2.0, `type: dataset`:
   title "GLACE: Sentinel-1 coherence and backscatter composites over Alpine
   glaciers"; authors: Luis Q. Gentner (University of Zurich, Department of
   Geography, email luis.gentner@geo.uzh.ch, add `orcid` only if it is found in the
   repository or the user's other repositories — otherwise omit the key);
   `license: CC-BY-4.0`; `repository-code: https://github.com/lqgentner/glace-catalog`;
   `url: https://data.source.coop/lqgentner/glace-ch`; `version` equal to
   `DATASET_VERSION`; `date-released` left for the release commit (omit the key
   now); `keywords` matching the collections' `keywords` arrays; an `abstract` of two
   or three sentences taken from the root catalog description. Zenodo reads this
   file when the GitHub bridge archives a release. Validate it with
   `uv run --with cffconvert cffconvert --validate` if that package installs
   cleanly; otherwise state in the report that it was not validated.
3. **`tests/test_version.py`**, a gate under `run_all.py`: `DATASET_VERSION`, the
   `version` field of both tracked `collection.json`, and `CITATION.cff`'s `version`
   are one string; the string matches `^\d+\.\d+(\.\d+)?$`. Fail with a message that
   names each place and its value.
4. **`DATASET_VERSION` docstring:** state the release rule from R-16 in three lines
   (bump here; collections and CITATION.cff follow, the gate enforces it; tag
   `v{version}` after the store is published and finalized).
5. **Release workflow — do not add one.** Zenodo's GitHub bridge needs the
   repository enabled on zenodo.org by its owner, which is the user's action; a
   workflow that publishes a GitHub release on tag push is optional and is left for
   the user to ask for. Record the manual procedure in the report so Task 5 can
   document it: bump `DATASET_VERSION` and the two collections and `CITATION.cff`
   → build and finalize the store → `tools/sync_data.py --confirm` → run
   `rashid check --live` against `public_base` → commit `date-released` →
   `git tag v{version}` and push the tag → create the GitHub release with the
   rashid summary in its notes → Zenodo mints the version DOI → add `sci:doi`
   (concept DOI) and `sci:citation` to the root catalog and both collections in the
   next commit if this was the first release.

**Report contract:** status, commits, test summary, whether `CITATION.cff` was
validated and how.

---

## Task 5 — Consolidate the documentation

**Files:** `README.md`, `AGENTS.md`, `docs/DESIGN.md` (new), `docs/STATUS.md` (new),
`docs/UPSTREAM.md`, `docs/conformance.md`, delete `docs/MIGRATION.md`,
`docs/superpowers/specs/2026-09-05-display-product-design.md`.
Also the three catalog prose files that carry stale grid statements:
`catalog/catalog.json` (`description` and the mosaics child title say "fixed
WebMercator grid"; the mosaics are ETRS89-LAEA 40 m since M-24), and
`catalog/mosaics/collection.json` / `catalog/mosaics/README.md` /
`catalog/mosaics/AGENTS.md` wherever they say the same.

Decision R-14 binds this task. Every statement in the new documents must be true of
the repository **after Tasks 1–4 and 6**; read the code, not the old prose, when in doubt.

**`docs/DESIGN.md`** (target ≤ 350 lines): the layout tree and the reasons that are
still live (§3 of MIGRATION.md minus the reversed arguments); the visualization
model as it stands (§4: PMTiles + styles, `renders`, LAEA analysis grid, the
"how standard is this" table); the repository model (§5, corrected: the build
writes to `GLACE_STORE_ROOT`, never into `catalog/`; publication is
`tools/sync_data.py` from the store); the checksum policy (sha256 only, one
paragraph); the gates (§8, with pyrefly); the decision table (§9) **with retired
rows dropped** and R-1…R-14 of this plan appended in the same style; the
glace-ch → glace-alps seam (§11's table and the one-vs-two-catalogs question,
still open). No history of reversals: git holds it.

**`docs/STATUS.md`** (target ≤ 120 lines): dated "as of" line; the state table
(§1, rewritten: composites regenerated and verified, gates real, mirror rules
implemented, thumbnails implemented, sync tool implemented; published store: old
prefixes being cleared, rehearsal build pending); the remaining-work list with only
open items, renumbered from P-1 and each with a one-line "verified when";
the swap checklist for the Alps (§11's grep command and its file list); an open
item "first release: enable the Zenodo bridge, then add `sci:doi` and
`sci:citation` to `catalog/catalog.json` and both `collection.json`" naming the
exact fields.

**`docs/UPSTREAM.md`** (target ≤ 120 lines): keep the framing paragraph; then the
three on-disk contracts only — D-1 glacier fractions (columns, the correct `--out`
path `{store_root}/tiles/glacier-fractions.parquet`, the `--perimeter-tiles`
argument), D-3 the label masks, D-4 the run manifest (columns table verbatim,
`manifest_version = 2` and the version-2 note from
`glacier_mapping/manifest.py`, the three constraints). Everything marked done is
one line in a closing "settled" list with its date. Drop "Simplifications taken"
and "Still worth doing" (all done after Task 1).

**`docs/conformance.md`:** update "Requirements Not Yet Met": `PTL-DAT-009/010`
satisfied by the regenerated composites (verified on real headers 2026-09-08),
`PTL-DAT-006/7/8` and `PTL-DAT-016` implemented (Task 4, gated by
`tests/test_mirror.py`); `PTL-LIV-*` still untested. Update the `PTL-AST-003`
paragraph to say finalize fills the fields on the store copy (Task 3). Keep the
accepted-deviation and stac-check sections.

**`README.md`:** a short "Citing" section (cite by release version; the DOI
arrives with the first release; `CITATION.cff` is the machine-readable form);
the tree listing (`tools/` line, `docs/` line); Quick start
unchanged; "Where things are" table rows pointing at the new files; step 1's
`--out` path corrected and `--perimeter-tiles 143` added; "Publishing" rewritten
for `tools/sync_data.py` (dry run, `--confirm`, `--prefix`); a sentence that the
`.env` needs `GLACIERS_S3_*` and `GLACE_*` variables and an example block listing
the variable names. Drop the rehearsal blockquote's pointer to MIGRATION §11 in
favour of STATUS.md.

**`AGENTS.md`:** the "Related repositories" section (s1bursts is released; no
editable installs); "Publication model" rewritten for the single uploader and the
proxy addressing (R-7, R-8); the "Gates" section mentions pyrefly; the pointer to
MIGRATION.md becomes DESIGN.md / STATUS.md. Keep the rest.

**Checks:** `uv run python3 tests/run_all.py` (the JSON edits must stay canonical;
`rashid check catalog/` must stay at 0 errors); `grep -rn 'MIGRATION.md\|mypy\|upload_data\|publish.py\|staging' --include='*.md' --include='*.py' --include='*.toml' --include='*.yaml' .` returns nothing outside `.git` and this plan file.

**Report contract:** status, commits, line counts of the four docs, and the grep
output (expected empty).
