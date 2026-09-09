# Web-map archives as item assets — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move each year's fourteen PMTiles archives into the year's item directory as `{stem}_viz.pmtiles`, register them on the year item as `visual` assets, publish one MapLibre style per year, and keep the collection-level `rel: "pmtiles"` links the spec requires.

**Architecture:** `publish-year` already builds the item from the build state, so the `pmtiles` step records a checksum beside the size it records today and the `items` step turns those records into assets exactly as `mosaic_assets` does for the COGs. `finalize-catalog` keeps writing the links, now from the year directories, and loops its existing year rewrite of the tracked style to write one published style per year. The viewer draws from the links unchanged and learns to read a year's style by year.

**Tech Stack:** Python 3.13, uv, pystac, rasterio, pmtiles; the repo's own `checks`-dict test scripts run by `uv run tests/run_all.py`; Node `--test` in `../glace-viewer`.

**Spec:** `docs/superpowers/specs/2026-09-09-webmap-item-assets-design.md`. Read it first; every task below argues from it.

## Global Constraints

- Read `AGENTS.md` at the repo root before touching anything: edit generators, never generated output; hand-maintained documents are `catalog/catalog.json`, both `collection.json`, every README/AGENTS.
- Gates: `uv run tests/run_all.py` must be green at the end of every task. `tests/test_lint.py` runs `ruff format --check`, `ruff check` and `pyrefly check` under `preset = "strict"`; write typed code.
- Tests are scripts, not pytest: each fills a `checks: dict[str, bool]`, prints `error  <name>` per failure, and exits 1 on any. Follow that shape (see `tests/test_webmap.py::main`). New test files are added to the explicit list in `tests/run_all.py`.
- Naming: renderings carry the suffix `_viz` on key and filename (`coh12_vv_viz`, `coh12_vv_viz.pmtiles`). Style layer ids keep the data stem: `glace-coh12_vv-2024`. Style source keys stay `src-coh12_vv`.
- Roles on an archive asset: `["visual"]` only. Never `data`, never `tiles`.
- Asset hrefs are absolute https on published items (the publisher does that already; item assets are written relative, `coh12_vv_viz.pmtiles`, exactly like `coh12_vv.tif`).
- Commit after every task with a conventional message. Do not push. Commit trailers:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_011518dSGqHiY9Xm2bKwndRo`.
- Do not run `publish-year.py` or `finalize-catalog.py` against the store; that is the user's migration step (spec "Migration"). Everything here is verified offline by the gates.
- Simplified, direct prose in docstrings and docs, in the voice the existing files use.

---

### Task 1: Naming helpers for renderings

**Files:**
- Modify: `src/glace_catalog/stac/naming.py`
- Modify: `src/glace_catalog/stac/__init__.py` (re-export if the package re-exports naming helpers; check how `asset_key`/`published_name` are exported and follow it)
- Test: `tests/test_items.py` (add checks to `main`)

**Interfaces:**
- Produces: `VIZ_SUFFIX: str = "_viz"`, `viz_key(asset_key: str) -> str` returning `f"{asset_key}{VIZ_SUFFIX}"`, `viz_filename(asset_key: str) -> str` returning `f"{viz_key(asset_key)}.pmtiles"`, `data_key(viz_key: str) -> str` returning the stem with the suffix removed (raises `ValueError` if the suffix is absent). Later tasks import these from `glace_catalog.stac.naming`.

- [x] **Step 1: Write the failing checks**

Add to `tests/test_items.py::main`, before the `failed = ...` line:

```python
    from glace_catalog.stac.naming import data_key, viz_filename, viz_key

    checks["a rendering's key is the data stem plus _viz"] = viz_key("coh12_vv") == "coh12_vv_viz"
    checks["a rendering's filename follows its key"] = (
        viz_filename("rtc_vh_qa_cqm") == "rtc_vh_qa_cqm_viz.pmtiles"
    )
    checks["the data stem is recoverable from the rendering key"] = (
        data_key("coh12_rgb_viz") == "coh12_rgb"
    )
    try:
        data_key("coh12_vv")
        checks["a key without the suffix is refused"] = False
    except ValueError:
        checks["a key without the suffix is refused"] = True
```

- [x] **Step 2: Run to verify it fails**

Run: `uv run tests/test_items.py`
Expected: ImportError on `viz_key` (the script fails before printing checks).

- [x] **Step 3: Implement**

In `src/glace_catalog/stac/naming.py`, near the existing key helpers:

```python
VIZ_SUFFIX = "_viz"
"""Suffix that marks a rendering: ``coh12_vv_viz.pmtiles`` is the web-map
archive of ``coh12_vv.tif``. The two sit in one directory (spec D-2), so the
stem has to say which is the measurement and which is the picture."""


def viz_key(asset_key: str) -> str:
    """Asset key of the rendering of one data asset."""
    return f"{asset_key}{VIZ_SUFFIX}"


def viz_filename(asset_key: str) -> str:
    """Filename of the rendering of one data asset, beside its COG."""
    return f"{viz_key(asset_key)}.pmtiles"


def data_key(key: str) -> str:
    """The data asset a rendering key belongs to."""
    if not key.endswith(VIZ_SUFFIX):
        msg = f"{key!r} is not a rendering key; expected the {VIZ_SUFFIX!r} suffix"
        raise ValueError(msg)
    return key[: -len(VIZ_SUFFIX)]
```

- [x] **Step 4: Run to verify it passes**

Run: `uv run tests/test_items.py` — expected `OK`. Then `uv run tests/test_lint.py`.

- [x] **Step 5: Commit**

```bash
git add src/glace_catalog/stac/naming.py src/glace_catalog/stac/__init__.py tests/test_items.py
git commit -m "feat: renderings carry a _viz suffix on key and filename"
```

---

### Task 2: The tiler writes `{stem}_viz.pmtiles` and records a checksum

**Files:**
- Modify: `src/glace_catalog/webmap.py` (`LayerInfo`, `build_pmtiles`, `build_layers.build_one`, module docstring paragraph about where archives go)
- Test: `tests/test_webmap.py::check_build`

**Interfaces:**
- Consumes: `viz_filename` from Task 1; `multihash_sha256_file(path)` from `glace_catalog.stac.store` (already used by `mosaics.py`).
- Produces: `LayerInfo.url == "coh12_vv_viz.pmtiles"`, new field `LayerInfo.checksum: str` (multihash sha256 of the archive). `build_layers` writes `out_dir / viz_filename(layer_id)`.

- [x] **Step 1: Update the checks**

In `tests/test_webmap.py::check_build` change the expectations:

```python
    checks["nothing but archives is staged"] = sorted(
        path.name for path in (work / "pmtiles").iterdir()
    ) == ["coh12_vh_viz.pmtiles", "coh12_vv_viz.pmtiles"]

    info = built[1]
    archive = work / "pmtiles" / info.url
    checks["the manifest entry names the archive and its range"] = (
        info.url == "coh12_vv_viz.pmtiles"
        and info.size_bytes == archive.stat().st_size
        and (info.vmin, info.vmax, info.cmap) == (VMIN, VMAX, CMAP)
        and info.min_zoom == 11
        and info.max_zoom == MOSAIC_ZOOM
    )
    checks["the manifest entry carries the archive's digest"] = (
        info.checksum == multihash_sha256_file(archive)
    )
```

and fix the later `read_tile(work / "pmtiles" / "coh12_vh.pmtiles", ...)` to `"coh12_vh_viz.pmtiles"`. Import `multihash_sha256_file` from `glace_catalog.stac.store`.

- [x] **Step 2: Run to verify it fails**

Run: `uv run tests/test_webmap.py` — expected failures on the three renamed checks and an AttributeError on `info.checksum`.

- [x] **Step 3: Implement**

In `webmap.py`:
- `LayerInfo` gains `checksum: str = ""` after `size_bytes`.
- In `build_pmtiles`, after `size = out_path.stat().st_size`, compute `checksum = multihash_sha256_file(out_path)` and pass `checksum=checksum` to `LayerInfo(...)`.
- In `build_layers.build_one`, replace `out_dir / f"{layer_id}.pmtiles"` with `out_dir / viz_filename(layer_id)`.
- The `build_pmtiles` docstring says the layer id "is also the archive's filename stem"; change to: the archive is named `viz_filename(layer_id)`, so a reader derives the data stem by stripping the suffix.
- Module docstring: the sentence "Nothing is written beside them" and the paragraph about `rel="pmtiles"` links: add that each archive is also an item asset with the `visual` role, written by `publish-year` from the record this module returns.

- [x] **Step 4: Run to verify it passes**

Run: `uv run tests/test_webmap.py && uv run tests/test_tiler.py && uv run tests/test_lint.py`.

- [x] **Step 5: Commit**

```bash
git add src/glace_catalog/webmap.py tests/test_webmap.py
git commit -m "feat: the tiler writes {stem}_viz.pmtiles and reports its digest"
```

---

### Task 3: Store layout — archives live in the year directory

**Files:**
- Modify: `src/glace_catalog/stac/store.py` (remove `StoreLayout.pmtiles`, add `StoreLayout.viz(year, asset_key)`, fix the layout docstring tree and its two paragraphs on `mosaics/pmtiles/`)
- Modify: `src/glace_catalog/thumbnails.py:392`
- Modify: `scripts/publish-year.py::step_pmtiles` (upload target) and the `pmtiles` state record (add `"checksum": layer.checksum`)
- Test: `tests/test_thumbnails.py` (whatever check pins the archive path; read the file), `tests/test_items.py` (layout checks if any exist; add one)

**Interfaces:**
- Produces: `StoreLayout.viz(self, year: int, asset_key: str) -> Path | UPath` returning `self.mosaic_year(year) / viz_filename(asset_key)`. No more `StoreLayout.pmtiles`.
- The `pmtiles` build-state record per layer gains `"checksum"`. Keys of that record stay the LayerInfo url without `.pmtiles`, i.e. now `"coh12_vv_viz"`; Task 4 relies on that key shape.

- [x] **Step 1: Write the failing check**

In `tests/test_items.py::main` add:

```python
    from glace_catalog.stac.store import StoreLayout

    layout = StoreLayout(Path("/store"))
    checks["a rendering sits in its year's item directory"] = (
        layout.viz(2024, "coh12_vv") == Path("/store/mosaics/2024/coh12_vv_viz.pmtiles")
    )
    checks["the layout no longer has a pmtiles directory"] = not hasattr(layout, "pmtiles")
```

- [x] **Step 2: Run to verify it fails**

Run: `uv run tests/test_items.py` — AttributeError on `viz`.

- [x] **Step 3: Implement**

`store.py`: delete `pmtiles()`; add

```python
    def viz(self, year: int, asset_key: str) -> Path | UPath:
        """One year's web-map archive of ``asset_key``, beside the COG it renders.

        Same directory, distinct stem: ``coh12_vv_viz.pmtiles`` next to
        ``coh12_vv.tif``. The archive is an asset of the year item with the
        ``visual`` role and, on the collection, a ``rel="pmtiles"`` link.
        """
        return self.mosaic_year(year) / viz_filename(asset_key)
```

Update the docstring tree in the same class: drop the `pmtiles/{year}/{layer}.pmtiles` line, change the year line to `{year}/item.json + 12 analysis COGs + 14 web-map archives`, and replace the paragraph starting "The web-map archives sit under ``mosaics/pmtiles/{year}/``" with two sentences: they sit beside the rasters they render, and the `_viz` suffix keeps the two products apart in a listing.

`thumbnails.py:392`: `archive = layout.viz(year, THUMBNAIL_LAYER)`.

`publish-year.py::step_pmtiles`: `stac.store.upload_tree(staged, layout.mosaic_year(args.year))` (the staged directory holds only archives; `upload_tree` merges into the year directory beside the COGs and deletes nothing). In the `pmtiles` state record add `"checksum": layer.checksum,`.

Grep for any other `layout.pmtiles(` or `"pmtiles" /` and fix it: `grep -rn "\.pmtiles(\|/ \"pmtiles\"" src scripts tools tests`.

- [x] **Step 4: Run the gates**

Run: `uv run tests/test_items.py && uv run tests/test_thumbnails.py && uv run tests/test_lint.py`. If `test_thumbnails` pins the old path, update that check to the new one.

- [x] **Step 5: Commit**

```bash
git add src/glace_catalog/stac/store.py src/glace_catalog/thumbnails.py scripts/publish-year.py tests/
git commit -m "refactor: web-map archives live in the year directory, beside their COGs"
```

---

### Task 4: The year item registers its archives as `visual` assets

**Files:**
- Modify: `scripts/publish-year.py` (new `visual_assets(state, styles)`; `step_items` merges it into the assets passed to `build_mosaic_item`)
- Modify: `src/glace_catalog/stac/items.py` (`MosaicAsset` gains `description: str | None = None`, rendered into the asset)
- Test: `tests/test_items.py` (the mosaic item checks; replace the "links no PMTiles archive" checks' comment, keep them true)

**Interfaces:**
- Consumes: the `pmtiles` build-state record from Task 3 (`state.details["pmtiles"]`, keyed `"coh12_vv_viz"`, fields `url, size_bytes, checksum, cmap, vmin, vmax, units, min_zoom, max_zoom`), `data_key`/`viz_key` from Task 1, `MosaicAsset` from `glace_catalog.stac`.
- Produces: `visual_assets(state: stac.BuildState) -> dict[str, stac.MosaicAsset]`, keys `coh12_vv_viz` etc., each `MosaicAsset(href="coh12_vv_viz.pmtiles", title="coh12_vv web-map tiles", roles=["visual"], media_type="application/vnd.pmtiles", size=..., checksum=..., description=..., extra={"proj:code": "EPSG:3857"})`.

- [x] **Step 1: Write the failing checks**

`tests/test_items.py` builds a mosaic item through `_mosaic_item()`; read that helper. Extend it (or add a sibling) so the assets passed in include one rendering, then add:

```python
    viz = mosaic.assets["coh12_vv_viz"]
    checks["a rendering is an item asset with the visual role"] = viz.roles == ["visual"]
    checks["a rendering is not data"] = "data" not in (viz.roles or [])
    checks["a rendering declares its own grid"] = viz.extra_fields.get("proj:code") == "EPSG:3857"
    checks["a rendering carries its digest"] = viz.extra_fields.get("file:checksum", "").startswith("1220")
    checks["a rendering is a PMTiles archive"] = viz.media_type == "application/vnd.pmtiles"
    checks["a rendering's href is beside the COG"] = viz.href == "coh12_vv_viz.pmtiles"
    checks["a rendering says what it is"] = "Not for analysis" in (viz.description or "")
```

The `MosaicAsset` for that check is built as `visual_assets` will build it; to test `visual_assets` itself, import it from the script (`tests/test_items.py` already imports from scripts? check how `test_mosaic_stats.py` or others import `publish-year.py`; if none does, load it with `importlib.util.spec_from_file_location("publish_year", ROOT / "scripts/publish-year.py")`) and feed a `stac.BuildState` whose `details["pmtiles"]` holds one record:

```python
    state = stac.BuildState(path=Path("/nowhere/.build-state.json"))  # match the real constructor; read store.py:590-630
    state.details["pmtiles"] = {
        "coh12_vv_viz": {
            "url": "coh12_vv_viz.pmtiles", "size_bytes": 10, "checksum": "1220" + "ab" * 32,
            "cmap": "cmc.lipari", "vmin": 0.1, "vmax": 0.8, "units": "", "min_zoom": 5, "max_zoom": 13,
            "product": "COH12", "polarization": "VV", "year": 2024, "id": "coh12_vv_2024",
            "start_date": "2024-07-09", "end_date": "2024-10-07", "bounds": [5.9, 45.8, 10.5, 47.8],
        },
    }
    built = publish_year.visual_assets(state)
    checks["visual_assets keys by rendering key"] = list(built) == ["coh12_vv_viz"]
```

- [x] **Step 2: Run to verify it fails**

Run: `uv run tests/test_items.py` — AttributeError/KeyError.

- [x] **Step 3: Implement**

`items.py::MosaicAsset`: add `description: str | None = None` and in `to_asset` pass `description=self.description` to `pystac.Asset(...)`.

`publish-year.py`, beside `mosaic_assets`:

```python
def visual_assets(state: stac.BuildState) -> dict[str, stac.MosaicAsset]:
    """Assemble the year item's renderings from what the ``pmtiles`` step recorded.

    One asset per archive, with the ``visual`` role and never ``data``: the
    pixels are an 8-bit stretch of the mosaic, backscatter in decibels, and an
    analysis pipeline that read one would be reading a picture. The archive's
    own header carries bounds and zoom range; the asset carries the grid, the
    digest, and a sentence saying which COG to read instead.
    """
    written = state.details.get("pmtiles", {})
    assets: dict[str, stac.MosaicAsset] = {}
    for key, detail in sorted(written.items()):
        stem = stac.data_key(key)
        stretch = f"{detail['vmin']:g}-{detail['vmax']:g}"
        ramp = f" through {detail['cmap']}" if detail.get("cmap") else ""
        units = f" {detail['units']}" if detail.get("units") else ""
        assets[key] = stac.MosaicAsset(
            href=detail["url"],
            title=f"{stem} web-map tiles",
            description=(
                f"Pre-styled 8-bit WebP rendering of {stem} on WebMercatorQuad "
                f"z{detail['min_zoom']}-z{detail['max_zoom']}, stretched {stretch}{units}{ramp} "
                f"as the collection's style for {detail['year']} declares. "
                f"Not for analysis: read {stem}.tif."
            ),
            roles=["visual"],
            media_type="application/vnd.pmtiles",
            size=detail.get("size_bytes"),
            checksum=detail.get("checksum"),
            extra={"proj:code": "EPSG:3857"},
        )
    return assets
```

Export `data_key` (and `viz_key`, `viz_filename`) through `glace_catalog.stac` the way the other naming helpers are exported (Task 1 did that; if it did not, do it here).

In `step_items`, where `assets = mosaic_assets(state, frame)` feeds `build_mosaic_item`, merge: `assets = {**mosaic_assets(state, frame), **visual_assets(state)}`. `build_mosaic_item` sorts keys, so the COG and its rendering land next to each other.

Update the comment in `tests/test_items.py` above "the mosaic item links no PMTiles archive": the item carries the archives as `visual` assets; the link with `pmtiles:layers` stays on the collection (PORTO-FMT-011), so the item still declares no web-map-links extension. Keep both existing checks.

- [x] **Step 4: Run the gates**

Run: `uv run tests/test_items.py && uv run tests/test_mirror.py && uv run tests/test_lint.py`. `test_mirror.py` builds a mirror from items; if it uses a fixture item with a fixed asset list, it should be indifferent to the extra assets; if it asserts asset counts, extend it.

- [x] **Step 5: Commit**

```bash
git add scripts/publish-year.py src/glace_catalog/stac/items.py tests/test_items.py
git commit -m "feat: the year item registers its web-map archives as visual assets"
```

---

### Task 5: Finalize — links from the year directories, one style per year

**Files:**
- Modify: `scripts/finalize-catalog.py` (`pmtiles_links`, `repoint_styles`, new `register_style_assets`, `write_documents`, `main`, new `check_style_coverage`)
- Modify: `catalog/mosaics/styles/default.json` (the fourteen `sources[].url` values: `pmtiles://../pmtiles/2024/{stem}.pmtiles` → `pmtiles://../2024/{stem}_viz.pmtiles`)
- Create: `tests/test_finalize.py`; add it to `tests/run_all.py` after `test_webmap.py`
- Test: `tests/test_styles.py` (if it checks source urls, update the expectation)

**Interfaces:**
- Consumes: `StoreLayout.mosaic_year`, `data_key`, `VIZ_SUFFIX`.
- Produces:
  - `pmtiles_links(layout, years, base_url) -> list[Document]`: one link per `*_viz.pmtiles` in `layout.mosaic_year(year)`, href `f"{base}/{MOSAICS_DIR}/{year}/{archive.name}"`, `pmtiles:layers == [f"glace-{data_key(archive.stem)}-{year}"]`, title `f"{stem} web-map tiles, {year}"`.
  - `repoint_styles(source_dir, staged, years: Sequence[int]) -> dict[str, Path]`: for each year writes `staged/mosaics/styles/{year}.json` from every tracked `styles/*.json` template (today one, `default.json`); returns `{f"style-{year}": path}`. With several tracked templates the key is `f"style-{template_stem}-{year}"` and the file `{template_stem}-{year}.json`, except the template named `default`, which drops its stem: `style-2024` / `2024.json`.
  - `register_style_assets(document: Document, years: Sequence[int], latest: int) -> None`: replaces every asset carrying the `style` role in the mosaics document with one entry per (template, year) built from that template's tracked entry: href `./styles/{year}.json`, title `f"{template title} ({year})"`, roles `["style", "default"]` for `latest` and the `default` template, else `["style"]`; description copied.
  - `check_style_coverage(document: Document, styles: Mapping[str, Path]) -> None`: every `pmtiles:layers` id on the document's `rel: "pmtiles"` links must be a `layers[].id` in the published style of that link's year; raise `SystemExit` naming the first missing id.

- [x] **Step 1: Write the failing test**

Create `tests/test_finalize.py` in the checks-dict shape. Load the script with `importlib` (`ROOT / "scripts/finalize-catalog.py"`, module name `finalize_catalog`). Build a temporary store: `tmp/mosaics/2023/coh12_vv_viz.pmtiles` and `tmp/mosaics/2024/coh12_vv_viz.pmtiles` (empty files), `tmp/mosaics/2024/coh12_vv.tif` (empty, must be ignored), and a temporary source dir with `mosaics/styles/default.json` copied from the tracked one and `mosaics/collection.json` copied from the tracked one. Then:

```python
    links = fin.pmtiles_links(stac.StoreLayout(store), [2023, 2024], "https://example.org/x")
    checks["one link per archive, none for COGs"] = len(links) == 2
    checks["the link points into the year directory"] = (
        links[1]["href"] == "https://example.org/x/mosaics/2024/coh12_vv_viz.pmtiles"
    )
    checks["pmtiles:layers names the data stem's style layer"] = (
        links[1]["pmtiles:layers"] == ["glace-coh12_vv-2024"]
    )

    written = fin.repoint_styles(source, staged, [2023, 2024])
    checks["one published style per year"] = sorted(written) == ["style-2023", "style-2024"]
    style_2023 = json.loads(written["style-2023"].read_text())
    checks["the 2023 style points at 2023 archives"] = all(
        "/2023/" in s["url"] and s["url"].endswith("_viz.pmtiles")
        for s in style_2023["sources"].values()
    )
    checks["the 2023 style's layer ids end in 2023"] = all(
        layer["id"].endswith("-2023") for layer in style_2023["layers"]
    )
    checks["the style name carries its year"] = "(2023)" in style_2023["name"]

    document = json.loads((source / "mosaics/collection.json").read_text())
    fin.register_style_assets(document, [2023, 2024], latest=2024)
    styles = {k: v for k, v in document["assets"].items() if "style" in v["roles"]}
    checks["the collection registers one style per year"] = sorted(styles) == ["style-2023", "style-2024"]
    checks["only the latest year is the default"] = (
        "default" in styles["style-2024"]["roles"] and "default" not in styles["style-2023"]["roles"]
    )
    checks["the tracked template entry is gone from the published document"] = "style-default" not in document["assets"]
    checks["the href is the year's file under styles/"] = styles["style-2023"]["href"] == "./styles/2023.json"

    document["links"] += links
    try:
        fin.check_style_coverage(document, written)
        checks["every linked layer is in its year's style"] = True
    except SystemExit:
        checks["every linked layer is in its year's style"] = False
    document["links"].append({**links[1], "pmtiles:layers": ["glace-nonesuch-2024"]})
    try:
        fin.check_style_coverage(document, written)
        checks["a link to a layer no style holds is refused"] = False
    except SystemExit:
        checks["a link to a layer no style holds is refused"] = True
```

- [x] **Step 2: Run to verify it fails**

Run: `uv run tests/test_finalize.py` — AttributeError on `register_style_assets` / wrong hrefs.

- [x] **Step 3: Implement**

`pmtiles_links`: iterate `layout.mosaic_year(year)`, keep `archive.suffix == ".pmtiles" and archive.stem.endswith(VIZ_SUFFIX)`, `stem = data_key(archive.stem)`, href `f"{base}/{MOSAICS_DIR}/{year}/{archive.name}"`, layers `[f"glace-{stem}-{year}"]`. Docstring: the link is where the spec puts `pmtiles:layers` (PORTO-FMT-011); the same archive is a `visual` asset of the year item.

`repoint_styles(source_dir, staged, years)`: loop the existing body over `years`; output name per the interface above; the regexes already rewrite `/\d{4}/` in every source url, `(\d{4})` in the name and `-\d{4}$` in layer ids. The tracked source urls must first be changed in `default.json` to `pmtiles://../2024/{stem}_viz.pmtiles` (fourteen edits; keep everything else). Docstring: one tracked template, one published copy per year, so every year's archive has a style that names its layers; the colours are still reviewed once.

`register_style_assets(document, years, latest)`: take the tracked style entries (assets whose roles include `style`), delete them from `document["assets"]`, and for each template × year add the entry described in the interface. Preserve key order by inserting the new entries where the first style entry was, if that is cheap; otherwise append.

`check_style_coverage(document, styles)`: read each published style once, collect its layer ids, then for each `rel == PMTILES_LINK_REL` link take the year from the href (`re.search(r"/(\d{4})/", href)`), look up `styles[f"style-{year}"]`, and require every id in `pmtiles:layers` to be present; `raise SystemExit(f"{href} names {id}, which styles/{year}.json does not draw")` on the first miss.

`write_documents`: `stamp_style_assets(documents, styles := repoint_styles(source_dir, staged, years))` needs `years`, so change its signature to take `years: Sequence[int]` alongside `latest` (or drop `latest` and derive `max(years)`); call `register_style_assets(documents[MOSAICS_DIR], years, max(years))` before stamping, then `check_style_coverage(documents[MOSAICS_DIR], styles)` after the links have been added (they are added in `main` before `write_documents`, so the check goes inside `write_documents` after registration).

`main`: pass `years` through.

- [x] **Step 4: Run the gates**

Run: `uv run tests/test_finalize.py && uv run tests/test_styles.py && uv run tests/test_links.py && uv run tests/test_lint.py`. `test_links.py` walks the tracked tree: `styles/default.json` still exists and is still linked by `style-default` in the tracked `collection.json`, so nothing breaks there.

- [x] **Step 5: Commit**

```bash
git add scripts/finalize-catalog.py catalog/mosaics/styles/default.json tests/test_finalize.py tests/run_all.py tests/test_styles.py
git commit -m "feat: one published style per year; archive links from the year directories"
```

---

### Task 6: Tracked catalog documents and prose

**Files:**
- Modify: `catalog/mosaics/collection.json` (`item_assets`: add the fourteen `{stem}_viz` entries; the description's sentence "Drawing is served separately by PMTiles archives registered on this collection…"; the `style-default` entry's description says it is the template every published year's style is written from; the thumbnail description names `coh12_rgb_viz.pmtiles`)
- Modify: `catalog/mosaics/README.md`, `catalog/mosaics/AGENTS.md`, `catalog/AGENTS.md` if it lists the tree, root `AGENTS.md` ("Renderings are never `data`" bullet and the layout tree)
- Modify: `docs/DESIGN.md` (§3 paragraph on where archives live; M-22 marked superseded by the new entry; M-23 amended; new M entries for item-level `visual` assets and per-year styles; take the next free numbers), `docs/STATUS.md` (state row for the pipeline, an Open entry for the migration: rebuild the four years' `pmtiles` and `items` steps, finalize, delete `mosaics/pmtiles/` on the UZH store and on Source Cooperative)
- Test: `tests/test_conformance.py`, `tests/test_data_format.py`, `tests/test_links.py` (they run on the tracked tree)

**Interfaces:** none new. `item_assets` entries for renderings:

```json
"coh12_vv_viz": {
  "type": "application/vnd.pmtiles",
  "title": "coh12_vv web-map tiles",
  "roles": ["visual"],
  "proj:code": "EPSG:3857",
  "description": "Pre-styled 8-bit WebP rendering of coh12_vv on WebMercatorQuad z5-z13, stretched as the year's style declares. Not for analysis: read coh12_vv.tif."
}
```

one per stem in `coh12_vv, coh12_vh, rtc_vv, rtc_vh, coh12_rgb, rtc_rgb, coh12_vv_qa_num, coh12_vh_qa_num, rtc_vv_qa_num, rtc_vh_qa_num, coh12_vv_qa_cqm, coh12_vh_qa_cqm, rtc_vv_qa_cqm, rtc_vh_qa_cqm`.

- [x] **Step 1: Run the tracked-tree gates before editing**

Run: `uv run tests/test_conformance.py && uv run tests/test_data_format.py` — both green; note the accepted findings so a new one is visible afterwards.

- [x] **Step 2: Edit the documents**

Make the edits listed under Files. In prose, say: each year's archives are assets of that year's item with the `visual` role and files beside the COGs, `{stem}_viz.pmtiles`; the collection registers every archive as a `rel: "pmtiles"` link and one style per year; the tracked `styles/default.json` is the template the published `styles/{year}.json` are written from. Keep the existing reasoning about PMTiles vs COG in DESIGN.md §3; only the location and registration change.

- [x] **Step 3: Run the tracked-tree gates**

Run: `uv run tests/test_conformance.py && uv run tests/test_data_format.py && uv run tests/test_links.py`. rashid must report no new finding; `PTL-COL-004` counts raster data assets only and `item_assets` is descriptive, so none is expected. If rashid objects to `proj:code` inside `item_assets`, keep it on the item assets only and drop it from `item_assets`.

- [x] **Step 4: Full gate**

Run: `uv run tests/run_all.py` — green.

- [x] **Step 5: Commit**

```bash
git add catalog docs AGENTS.md
git commit -m "docs: archives are item assets beside their COGs; one style per year"
```

---

### Task 7: Viewer reads a year's style by year

**Files (all in `../glace-viewer`):**
- Modify: `js/store.js` (`styleHref` → `styleHrefs`, `readStore` fetches every year's style and merges legends; keep the stem fallback)
- Modify: `AGENTS.md` ("The published store" tree and the `ln -s` recipe; "Reading the catalog" bullets: the style per year is `style-{year}`, the default is the latest)
- Test: `tests/store.test.js`, `tests/store-catalog.test.js`, fixtures under `tests/fixtures/` (the `two-years/` fixture already has "a style describing only the newer year", which is the fallback path; add a `style-2023` asset and file there so the per-year path is exercised too)

**Interfaces:**
- Produces: `styleHrefs(collection) -> Map<number|"default", string>`: every asset with the `style` role; a key/href ending in a four-digit year maps that year, the `default`-role asset maps `"default"`. `readStore` resolves a layer's legend from the style of its own year first, then the default, then the stem fallback.

- [x] **Step 1: Write the failing test**

In `tests/store.test.js`, following its existing pattern for building a fake collection, add a case: a collection with `style-2023` (`./styles/2023.json`) and `style-2024` (`./styles/2024.json`, roles `style, default`) and two `rel: "pmtiles"` links (`glace-coh12_vv-2023`, `glace-coh12_vv-2024`) whose styles carry different `vmax` in `portolan:legend`; assert the 2023 record's legend comes from `2023.json`, not from the default.

- [x] **Step 2: Run to verify it fails**

Run: `cd ../glace-viewer && npm test` — the new case fails (only the default style is read).

- [x] **Step 3: Implement**

`store.js`: replace `styleHref` with

```js
function styleHrefs(collection) {
  const out = new Map();
  for (const [key, asset] of Object.entries(collection?.assets ?? {})) {
    if (!Array.isArray(asset?.roles) || !asset.roles.includes("style") || !isNonEmptyString(asset.href)) continue;
    const year = /(\d{4})(?:\.json)?$/.exec(asset.href) ?? /(\d{4})$/.exec(key);
    if (year) out.set(Number(year[1]), asset.href);
    if (asset.roles.includes("default")) out.set("default", asset.href);
  }
  if (out.size === 0) throw new Error("the mosaics collection names no style asset");
  return out;
}
```

In `readStore`, fetch every distinct href in the map in parallel with the items, build `legends()` per style, and when joining an archive `{stem, year}` look up `byId` in the style of `year`, then the default's, then the stem fallback. Keep the existing error text for a collection with no style.

- [x] **Step 4: Run the suite**

Run: `npm test` — green, including `store-catalog.test.js` against the committed fixture (which still has one default style; the fallback path).

- [x] **Step 5: Docs and commit**

Update `AGENTS.md` as listed. Commit in the viewer repo:

```bash
git add js/store.js tests AGENTS.md
git commit -m "feat: read each year's style by year, falling back to the default"
```

Do **not** touch the `// SPIKE` files (`js/cog-webp.js`, and the `// SPIKE` lines in `js/config.js`, `js/map.js`, `js/rasters.js`): they are throwaway and uncommitted; leave them out of the commit (`git add` the listed paths only) and out of the diff.

---

### Task 8: Final gate and handoff notes

- [x] Run `uv run tests/run_all.py` in glace-catalog and `npm test` in glace-viewer; both green.
- [x] `git log --oneline main..HEAD` in both repos; every task is one commit.
- [x] Write the migration steps into `docs/STATUS.md` if Task 6 did not: `publish-year.py {2021..2024} --scope ch --steps pmtiles,items`, then `finalize-catalog.py --base-url …`, then delete `mosaics/pmtiles/` on the UZH store and on Source Cooperative by hand (publishing never deletes), then `rashid check --data`.
- [x] Report: commits, anything the plan said that turned out wrong and what was done instead, and any check the plan asked for that could not be made to pass.
