# Metadata generation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render `catalog/` (JSON, markdown, style) from one tracked `metadata/` source, and make the build read the same asset vocabulary, so titles, descriptions and display numbers cannot drift.

**Architecture:** `metadata/catalog.yaml` and `metadata/assets.yaml` are loaded by `glace_catalog.metadata` into a `Vocabulary`; `glace_catalog.documents` renders every tracked document from it (Jinja2 for markdown); `scripts/render-catalog.py` writes or checks `catalog/`; `tests/test_render.py` is the regeneration gate. `items.py`, `publish-year.py`, `finalize-catalog.py` and `save.py` call the vocabulary instead of carrying strings.

**Tech Stack:** Python 3.13, PyYAML (present), Jinja2 (new runtime dependency), pyproj (present via rasterio) for the WGS84 extent, existing gates in `tests/run_all.py`.

**Spec:** `docs/superpowers/specs/2026-09-09-metadata-generation-design.md`

## Global Constraints

- American spelling in every rendered string ("polarized", "color").
- Titles: `"{Product} {POL}"`, `"{Product} {POL} – {QA}"`, `" – PMTiles"` suffix for archives, en dash with spaces.
- Canonical JSON: two-space indent, `ensure_ascii=False`, one trailing newline (`tests/test_data_format.py`).
- No `proj:code` in `item_assets`, no projection extension on either collection.
- QA `item_assets` roles: `["quality", "cloud-optimized"]`.
- Lint: `ruff format`, `ruff check`, `pyrefly check` strict must pass over `src/`, `scripts/`, `tools/`, `tests/`.
- `catalog/` stays tracked; `finalize-catalog` keeps patching it and never renders.

---

### Task 1: The vocabulary

**Files:**
- Create: `metadata/assets.yaml`, `metadata/catalog.yaml`, `src/glace_catalog/metadata.py`
- Modify: `pyproject.toml` (add `jinja2>=3.1`), `uv.lock`
- Test: `tests/test_render.py` (vocabulary half)

**Interfaces (produced):**
- `load_vocabulary(root: Path = METADATA_DIR) -> Vocabulary`
- `Vocabulary.asset_title(key: str) -> str`
- `Vocabulary.item_asset(key: str, collection: Literal["tiles","mosaics"]) -> dict[str, Any]`
- `Vocabulary.render(key: str) -> dict[str, Any]`
- `Vocabulary.display(key: str) -> LayerStyle | RgbStyle` (from `glace_catalog.styles`)
- `Vocabulary.legend(key: str) -> dict[str, Any]` (the `portolan:legend` block, stops derived)
- `Vocabulary.viz_asset(key: str, *, min_zoom: int, max_zoom: int) -> tuple[str, str]` (title, description)
- `Vocabulary.pmtiles_link_title(key: str, year: int) -> str`
- `Vocabulary.collection_title(collection) -> str`, `.catalog.short_title`, `.catalog.title`, `.catalog.description`, `.catalog.years`, providers, license, keywords, links, region.
- `VIZ_KEYS: tuple[str, ...]` = twelve `_viz` keys + `coh12_rgb_viz`, `rtc_rgb_viz`.

- [ ] Write `metadata/assets.yaml` with products, polarizations, QA metrics, stretches (values from `tests/test_styles.py` EXPECTED_*), and the description sentences.
- [ ] Write `metadata/catalog.yaml` (scope, region, titles, description, providers, license, keywords, links, years, collection titles/descriptions).
- [ ] Write the test: every `ASSET_KEYS` key and every `VIZ_KEYS` key resolves; titles unique; QA roles carry `cloud-optimized`; `render("rtc_vv")["expression"] == "10*log10(rtc_vv_b1)"`; `display("coh12_vv").vmax == 0.75`.
- [ ] Run, see it fail on import; implement `metadata.py`; run, pass.
- [ ] Commit: `feat: the asset vocabulary and catalog config in metadata/`.

### Task 2: The renderer and the gate

**Files:**
- Create: `src/glace_catalog/documents.py`, `scripts/render-catalog.py`, `metadata/templates/{catalog,tiles,tiles/year,mosaics}/{README,AGENTS}.md.j2`
- Modify: `tests/test_render.py` (regen half), `tests/run_all.py`, `tests/test_styles.py`
- Regenerate: `catalog/catalog.json`, both `collection.json`, six markdown files, `catalog/mosaics/styles/default.json`

**Interfaces (produced):**
- `render_tree(vocabulary: Vocabulary, out: Path) -> list[Path]`
- `render_year_prose(vocabulary: Vocabulary, year: int) -> tuple[str, str]` (README, AGENTS)
- `wgs84_bbox(grid: Grid) -> list[float]`
- CLI: `uv run scripts/render-catalog.py [--out catalog] [--check]`

- [ ] Write the regen test: render into a temp dir, compare file sets and bytes with `catalog/`, print each differing path.
- [ ] Implement `documents.py` and the templates; render; review the diff against the old tracked tree by eye.
- [ ] Make `test_styles.py` read expected values from the vocabulary; keep its invariants.
- [ ] Add `test_render.py` to `run_all.py` after `test_data_format.py`.
- [ ] Run `uv run tests/run_all.py`; all green.
- [ ] Commit: `feat: catalog/ is rendered from metadata/ and gated on regeneration`.

### Task 3: The build reads the vocabulary

**Files:**
- Modify: `src/glace_catalog/stac/items.py` (`_data_asset`, `_qa_asset`, drop `_band_title`), `scripts/publish-year.py` (`mosaic_assets`, `visual_assets`, root title), `scripts/finalize-catalog.py` (`pmtiles_links`), `src/glace_catalog/stac/save.py` (drop `_collection_title` and `year_catalog_prose`, take titles and prose as arguments), `src/glace_catalog/stac/__init__.py` exports
- Test: `tests/test_items.py` (title assertions), `tests/test_finalize.py` (link title)

- [ ] Add assertions: tile item `coh12_vv` title is `vocabulary.asset_title("coh12_vv")`; QA title matches; mosaic viz title ends with `– PMTiles`; pmtiles link title equals `pmtiles_link_title`.
- [ ] Run, fail; implement; run, pass; `uv run tests/run_all.py` green.
- [ ] Commit: `feat: items, links and year prose take their names from the vocabulary`.

### Task 4: Repository documentation

**Files:**
- Modify: `AGENTS.md` ("The one rule", hand-maintained list), `docs/DESIGN.md` (§4 table, M-21/M-27 wording, R-11 superseded, new R-18), `docs/STATUS.md`, `README.md` (layout section if it names `catalog/` as hand-written), `.cache/portolan-metadata-issues.md` untouched.

- [ ] Update the documents; `uv run tests/run_all.py`; commit: `docs: metadata/ is the source, catalog/ is generated`.

### Task 5: Review

- [ ] Dispatch a Fable subagent to review the diff since `b95be65` and the prose in `metadata/`; act on findings; commit.
