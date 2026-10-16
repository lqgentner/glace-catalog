# Tile-grid PMTiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish one vector PMTiles of the MGRS tile grid, carrying each tile's glacier fractions, and stop publishing `glacier-fractions.parquet`.

**Architecture:** `finalize-catalog.py` builds `tiles/grid.pmtiles` from the per-year item indexes it already reads, since every tile item carries its geometry and both fractions. GDAL's PMTiles driver writes it through pyogrio. The tiles collection registers the archive as a `visual` asset with a `rel: "pmtiles"` link and a grid style, the FIRMS pattern, which conforms to `PORTO-FMT-011`. The fractions table becomes a dot-prefixed build input that `build-year.py` reads and `publish.py` never uploads.

**Tech Stack:** Python 3.13, GDAL 3.12 via pyogrio (bundled with geopandas, no new dependency), pyarrow, geopandas, the `pmtiles` reader for tests.

**Spec:** this document's *Design* section. It was agreed in chat on 2026-10-16 and has no separate spec file. The viewer side comes from the glace-viewer agent's measurement on an Alpine mirror rebuilt from the 2015–2026 manifests: 26,519 rows, 13 row groups and a 1.17 MB footer. To draw the grid, the viewer downloads 1.28 MB to get 107 KB of grid columns.

**When:** after the current Alpine build finishes. Do not change code or the store while it runs.

## Design

- **Archive:** `tiles/grid.pmtiles`, `application/vnd.pmtiles`, with a vector layer `mgrs`.
- **Features:** one per MGRS tile that has an item in any published year, about 2,210 for the Alps. A tile missing from one year, such as 32TNT14 in 2015, is still in the grid.
- **Properties:**
  - `mgrs_tile` (string, e.g. `32TMS55`)
  - `glacier_fraction` (number, 0–1)
  - `glacier_fraction_buffered` (number, 0–1)
- **Geometry:** the tile item's geometry, the grid cell simplified to its corners, in EPSG:4326.
- **Zooms:** max 10. Min is the lowest zoom at which every feature survives. Start at 0; Task 2's test sets the floor.
- **Years:** one archive per scope; the grid does not depend on the year.
- **Registration on the tiles collection:**
  - asset `grid`: absolute href with `alternate`, roles `["visual"]`, `file:size`, `file:checksum`;
  - link `{"rel": "pmtiles", "href": <same absolute href>, "type": "application/vnd.pmtiles", "pmtiles:layers": ["mgrs"]}`;
  - the web-map-links v1.3.0 extension;
  - style asset `style-grid` at `./styles/grid.json`, roles `["style", "default"]`, type `application/vnd.mapbox.style+json`.

  rashid PTL-VIZ-002, -003 and -005 require this; `ACCEPTED` stays empty.
- **Grid style:** a MapLibre vector style. Source `pmtiles://../grid.pmtiles`, a fill layer coloured by `glacier_fraction_buffered` over 0–1, and an outline layer.
- **Removed:** the `glacier-fractions` collection asset. Items keep `glace:glacier_fraction` and `glace:glacier_fraction_buffered`.
- **Build input:** `tiles/.glacier-fractions.parquet`, renamed from `tiles/glacier-fractions.parquet`. Dot-prefixed files stay in the store (`tools/publish.py`). Columns are unchanged (D-1).
- **Inventory provenance:** each scope gets `glacier_inventory = "RGI7-C-11"` and `glacier_buffer_m = 250` (the values in the current table). `build-year.py` fails when the table disagrees. The prose names both values.

## Global Constraints

- The asset key is `grid`, the file is `tiles/grid.pmtiles`, the layer is `mgrs`, and the properties are `mgrs_tile`, `glacier_fraction` and `glacier_fraction_buffered`. The glace-viewer contract depends on these exact names.
- No new dependency: write with `pyogrio.write_dataframe(..., driver="PMTiles")`. Do not use tippecanoe.
- Do not widen `ACCEPTED` in `tests/test_conformance.py`.
- Edit sources and re-render: `uv run scripts/render-catalog.py`. Never hand-edit `catalog/`.
- Prose and comments describe the current state only, with no history ("now", "formerly", "replaces").
- The gate is `uv run tests/run_all.py`: ruff format, ruff check and strict pyrefly.

## Review Focus

1. **Small cells at low zoom.** GDAL's MVT encoder drops a polygon that collapses after quantisation, and a 10 km cell is about one unit at z0. Expect every tile at the archive's min zoom; Task 2 tests this.
2. **One tile with different fractions in two years.** That means a rebuilt input, and the grid must not pick one silently. Raise instead; Task 2 tests this.
3. **A tile present in only some years.** It must still appear in the grid; Task 2 tests this.
4. **A store that still holds `tiles/glacier-fractions.parquet`.** Publishing would upload it as an unregistered object. Finalization fails and names the rename; Task 4 tests this.
5. **A store on S3 (`UPath`).** GDAL writes PMTiles to a local file only. Write to a temporary directory, then copy. Task 2 takes a `Path | UPath` target, and the test writes through a local `UPath`.

---

### Task 1: Fractions table as a dot-prefixed, checked build input

**Files:**
- Create: `src/glace_catalog/grid.py`, with the fraction reader only; Task 2 adds the archive.
- Modify:
  - `src/glace_catalog/scopes.py`: add `glacier_inventory: str` and `glacier_buffer_m: float` to the scope dataclass, and set both scopes to `"RGI7-C-11"` and `250`.
  - `src/glace_catalog/stac/store.py:212-214`: `glacier_fractions` returns `self.tiles / ".glacier-fractions.parquet"`. Also update the layout docstring tree at line 130.
  - `scripts/build-year.py:303-330`: replace `load_glacier_fractions` with a call to `grid.read_glacier_fractions`.
- Docs: in `AGENTS.md` step 1, `docs/UPSTREAM.md` D-1 and `docs/STATUS.md:15`, change the `--out` path to `.../tiles/.glacier-fractions.parquet`. D-1 also says the table is a build input that is not published.
- Test: create `tests/test_grid.py`, following the `check_*` and `main()` pattern of `tests/test_finalize.py`, and add it to `tests/run_all.py` before `test_finalize.py`.

**Interfaces:**
- Produces: `read_glacier_fractions(path: Path | UPath, scope: Scope) -> dict[str, tuple[float, float]]`, mapping `mgrs_code` to `(glacier_fraction, glacier_fraction_buffered)`.
  - It raises `FileNotFoundError` when the file is missing, keeping the current message pointing to `derive-glacier-fractions.py`.
  - It raises `ValueError` when `rgi_version` or `buffer_m` differ from `scope.glacier_inventory` or `scope.glacier_buffer_m`, naming both values.

- [ ] **Step 1: Write the failing checks** `check_fraction_reader` in `tests/test_grid.py`, against a two-row parquet written to a temporary directory:
  - a matching table returns `{"32TMS55": (0.1, 0.2), "32TMS56": (0.0, 0.05)}`;
  - a table with `rgi_version="RGI6"` raises `ValueError`, and `"RGI7-C-11"` appears in the message;
  - `buffer_m=500` raises `ValueError`;
  - a missing path raises `FileNotFoundError`.
- [ ] **Step 2: Run** `uv run tests/test_grid.py`. Expected: fails on import of `glace_catalog.grid`.
- [ ] **Step 3: Implement** the scope fields, `read_glacier_fractions`, the `store.py` path and the `build-year.py` call.
- [ ] **Step 4: Run** `uv run tests/test_grid.py && uv run tests/run_all.py`. Expected: all pass. The tracked catalog does not change in this task.
- [ ] **Step 5: Commit** with the message "Read glacier fractions from a dot-prefixed build input and check its inventory".

### Task 2: Build the grid archive

**Files:**
- Modify: `src/glace_catalog/grid.py`.
- Modify: `src/glace_catalog/stac/store.py`: add the `tiles_grid` property, returning `self.tiles / "grid.pmtiles"`, and add it to the layout docstring tree.
- Test: `tests/test_grid.py`.

**Interfaces:**
- Consumes: the per-year index tables `finalize-catalog.py` reads (`read_year_index`). Their columns are `glace:mgrs_tile`, `glace:glacier_fraction`, `glace:glacier_fraction_buffered` and `geometry` (WKB). Confirm the names in `src/glace_catalog/stac/save.py` before writing.
- Produces:
  - `GRID_LAYER = "mgrs"`, `GRID_MAX_ZOOM = 10` and `GRID_MIN_ZOOM: int`. The minimum is the value the min-zoom check passes with: try 0 first, then raise it one step at a time.
  - `grid_frame(tables: Sequence[pa.Table]) -> gpd.GeoDataFrame`: one row per distinct tile, sorted by `mgrs_tile`, with columns `mgrs_tile`, `glacier_fraction`, `glacier_fraction_buffered` and `geometry`, in EPSG:4326. It raises `ValueError` when one tile has different fractions or geometries across tables, naming the tile, and when a fraction is null.
  - `write_grid(frame: gpd.GeoDataFrame, target: Path | UPath) -> None`: writes to a temporary local file, then copies to `target`. It uses `pyogrio.write_dataframe(frame, tmp, driver="PMTiles", layer=GRID_LAYER, dataset_options={"MINZOOM": GRID_MIN_ZOOM, "MAXZOOM": GRID_MAX_ZOOM, "SIMPLIFICATION": 0, "MAX_FEATURES": <≥ rows>, "MAX_SIZE": <large enough that no tile drops features>, "NAME": ..., "DESCRIPTION": ...})`. Check each option name against the GDAL 3.12 PMTiles/MVT driver docs.

- [ ] **Step 1: Write the failing checks** in `tests/test_grid.py`. They use synthetic year tables: two years, three real Alpine MGRS cells, one present only in the second year. Build the geometry the way `stac/items.py:_tile_geometry` does.
  - `check_grid_frame`: three rows; the tile present in one year is included; the column dtypes are string, float and float.
  - `check_grid_conflict`: changing one tile's `glace:glacier_fraction` in one year raises `ValueError` naming that tile.
  - `check_grid_archive`:
    - after `write_grid` to a local `UPath`, the archive header reports `min_zoom == GRID_MIN_ZOOM` and `max_zoom == 10`;
    - the metadata's `vector_layers` contains exactly one layer, with id `mgrs`;
    - decoding every tile at `GRID_MIN_ZOOM` and at 10 yields all three `mgrs_tile` values;
    - the decoded fractions equal the inputs to six decimals.

    Decode by reading the tiles with the `pmtiles` reader and opening them with `pyogrio`/GDAL's MVT driver, or by reading the archive back with `pyogrio.read_dataframe(path, layer="mgrs")` at a zoom. Pick whichever GDAL 3.12 supports.
- [ ] **Step 2: Run** `uv run tests/test_grid.py`. Expected: fails on the missing `grid_frame`.
- [ ] **Step 3: Implement** `grid_frame` and `write_grid`. Start with `GRID_MIN_ZOOM = 0`.
- [ ] **Step 4: Run** `uv run tests/test_grid.py`. If the min-zoom check loses cells, raise `GRID_MIN_ZOOM` until it passes, and record the value in a one-line comment saying why.
- [ ] **Step 5: Measure** on the real store. In the scratchpad, run `grid_frame` over the year indexes under `/mnt/data/glace-alps`, write the result and note the row count, which should be 2,210, and the file size. Do not write into the store.
- [ ] **Step 6: Run** `uv run tests/run_all.py`, then commit with the message "Build a vector PMTiles of the tile grid with glacier fractions".

### Task 3: Register the grid and its style; drop the fractions asset

**Files:**
- Modify `src/glace_catalog/documents.py`:
  - `_collection_assets`, lines 245–257: replace the `glacier-fractions` asset with `grid` (plus `alternate`) and `style-grid`.
  - `collection_document`: for `TILES_DIR`, add `WEB_MAP_LINKS_EXTENSION_URI = "https://stac-extensions.github.io/web-map-links/v1.3.0/schema.json"` and the `rel: "pmtiles"` link.
  - Add `grid_style_document(vocabulary: Vocabulary) -> Document` and render it in `render_tree` to `tiles/styles/grid.json`.
- Modify `metadata/templates/AGENTS.md.j2:66-67`, `metadata/templates/tiles/AGENTS.md.j2` and `metadata/templates/tiles/README.md.j2`. They name the grid archive, its layer and properties, and the inventory (`RGI7-C-11`, 250 m buffer), from `vocabulary.catalog.scope`. The lookup is the item properties or the grid; the parquet is no longer mentioned.
- Modify `docs/DESIGN.md`:
  - the layout tree at line 45;
  - M-36, line 456, scoped to "the mosaics collection";
  - new decision M-38: the tile grid is one vector PMTiles on the tiles collection, registered per `PORTO-FMT-011`, with fractions as properties; the fractions table is a build input.
- Modify `docs/conformance.md`:
  - line 105 drops `glacier-fractions` and adds `grid`;
  - the `PORTO-FMT-011` section says the tiles collection conforms and the deviation covers the mosaics collection only.
- Re-render: `catalog/`, by running the renderer.
- Test: extend `tests/test_styles.py` and `tests/test_render.py` (see Step 1).

**Interfaces:**
- Consumes: `GRID_LAYER` from Task 2, and `PMTILES_MEDIA_TYPE` and `STYLE_MEDIA_TYPE` from `metadata.py`/`documents.py`.
- Produces:
  - the tracked `catalog/tiles/collection.json` with the `grid` and `style-grid` assets and the `pmtiles` link;
  - the tracked `catalog/tiles/styles/grid.json`;
  - `GRID_STYLE_NAME = "grid"`, used by Task 4.

- [ ] **Step 1: Write the failing checks.**
  - In `tests/test_styles.py`:
    - `grid.json` has `version == 8` and a single vector source with url `pmtiles://../grid.pmtiles`;
    - every layer's `source-layer` is `"mgrs"`;
    - the fill colour expression reads `glacier_fraction_buffered`.
  - In the tiles collection:
    - there is no `glacier-fractions` key;
    - `grid.roles == ["visual"]` and `grid.type == "application/vnd.pmtiles"`;
    - exactly one link has `rel == "pmtiles"`, with the same href as `grid` and `pmtiles:layers == ["mgrs"]`;
    - the web-map-links URI is in `stac_extensions`.
  - The mosaics collection has no `pmtiles` link (M-36).
- [ ] **Step 2: Run** `uv run tests/test_styles.py`. Expected: fails.
- [ ] **Step 3: Implement**, then run `uv run scripts/render-catalog.py`.
- [ ] **Step 4: Run** `uv run tests/test_render.py && uv run tests/test_conformance.py && uv run tests/run_all.py`. Expected: all pass with `ACCEPTED` unchanged. If rashid reports a finding on the tiles collection, fix the documents. Do not accept the finding.
- [ ] **Step 5: Commit** with the message "Register the tile grid PMTiles and its style; stop publishing the fractions table".

### Task 4: Write and stamp the grid in finalization

**Files:**
- Modify `scripts/finalize-catalog.py`:
  - in `main`, after `concat_indexes`, call `require_no_published_fractions(layout)`, then `write_grid(grid_frame(tables), layout.tiles_grid)`;
  - `stamped_asset_paths`: the tiles entry swaps `"glacier-fractions"` for `"grid": layout.tiles_grid`;
  - `write_documents`: stage `tiles/styles/*.json` unchanged, the same way the Markdown is copied, then stamp `file:size` and `file:checksum` on `style-grid` from the staged file;
  - update the module docstring list.
- Test: `tests/test_finalize.py`.

**Interfaces:**
- Consumes: `grid_frame`, `write_grid` (Task 2), `StoreLayout.tiles_grid` (Task 2) and the tracked `catalog/tiles/styles/grid.json` (Task 3).
- Produces: `require_no_published_fractions(layout: stac.StoreLayout) -> None`. It raises `SystemExit` when `layout.tiles / "glacier-fractions.parquet"` exists. The message names the rename: `mv … tiles/.glacier-fractions.parquet`, or for S3, copy then delete.

- [ ] **Step 1: Write the failing checks** in `tests/test_finalize.py`:
  - `check_no_published_fractions`: a store with `tiles/glacier-fractions.parquet` raises `SystemExit`; a store with only the dot-prefixed file passes.
  - `check_grid_stamping`: after `stamp_collection_assets` over a store that holds a `tiles/grid.pmtiles`, the tiles document's `grid` asset has `file:size` equal to the file's length.
  - `check_grid_style_staged`: after `write_documents` into a temporary store, `tiles/styles/grid.json` exists there, and `style-grid` carries `file:checksum`.
- [ ] **Step 2: Run** `uv run tests/test_finalize.py`. Expected: fails.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run tests/run_all.py`. Expected: all pass.
- [ ] **Step 5: Commit** with the message "Write and stamp the tile grid during finalization".

### Task 5: Migrate the stores, validate the built tree, hand over to the viewer

This is operational: no new code, and it runs only after the Alpine build has finished.

- [ ] **Step 1: Rename the build input in each store.**
  - Local store: `mv /mnt/data/glace-alps/tiles/glacier-fractions.parquet /mnt/data/glace-alps/tiles/.glacier-fractions.parquet`.
  - If `s3://s1-bursts/data/glace-alps/tiles/glacier-fractions.parquet` exists, copy it to the dot-prefixed key, then delete the original.
- [ ] **Step 2: Check whether the rename invalidates the `items` step's fingerprint**, by running `build-year.py <year> --steps items` for one year. If it rebuilds, it rebuilds every year's items (about 1 min each). Note this in the commit message of the run, not in the code.
- [ ] **Step 3: Finalize locally:** `make finalize`. Expected: `tiles/grid.pmtiles` is written, and validation passes.
- [ ] **Step 4: Validate the built tree with rashid**, as `docs/conformance.md` describes. Expected: no new findings on the tiles collection. Then open `tiles/styles/grid.json` in a MapLibre/PMTiles viewer (or the Portolan browser after publication) and check that the cells draw at the archive's min zoom and at zoom 10.
- [ ] **Step 5: Update the memory notes** with the grid's size, min zoom and feature count.
- [ ] **Step 6: Hand over to the glace-viewer agent** through herdr (`herdr agent list`, then prompt the glace-viewer pane). Send:
  - the final contract: path `tiles/grid.pmtiles`, layer `mgrs`, the three properties, `GRID_MIN_ZOOM`/`GRID_MAX_ZOOM`, and the style at `tiles/styles/grid.json`;
  - the local store path to test against.

  The viewer then drops `tile-grid.js` and hyparquet, as it proposed.
- [ ] **Step 7: Before publishing, run** `make publish-plan`. It must list `tiles/grid.pmtiles` and `tiles/styles/grid.json`, and must not list either fractions file. If an earlier upload left `tiles/glacier-fractions.parquet` on Source Coop, remove it there separately.
