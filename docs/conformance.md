# Portolan Conformance

Conformance means passing [rashid](https://github.com/portolan-sdi/rashid), so
it runs in CI:

```bash
uv run tests/test_conformance.py
```

The gate fails on any error-severity finding whose rule is not listed below,
and the list never grows without a row here. A known deviation with an issue
number is a debt someone can pay off; a silently widened allow-list is a false
claim about conformance.

Checked against **rashid 0.3.0** and spec **v0.2.0**.

## What the gate reads

The gate runs against the **tracked tree**: the root catalog and the two
collections. The generated items are not in it.

Conformance is really a claim about the **published** tree, which adds every
generated item. rashid takes a local catalog directory only, so both targets
mirror the store's documents first (`tools/mirror_docs.py`):

```bash
make conformance        # before publication: documents and schema, no asset bytes
make conformance-live   # after publication: also the collection assets' bytes and the server
```

Both targets validate every object against the schemas of the extensions it
declares. rashid bundles the ones its registry pins; `--schema-allow-network`
fetches the four it does not (`sar`, `mgrs`, `grid` and `processing`, which
rashid otherwise reports as `PTL-EXT-002` and leaves unchecked).

The collection asset hrefs are absolute public URLs, so `make conformance-live`
reads the published collection assets and probes the server's range and CORS
support. Item asset hrefs are relative and do not resolve in the mirror, so
neither target reads the COGs or archives. `make links-live` requests each of
them instead. rashid would also request them, one at a time, once it knows the
publish base: about ten hours for the Alps. It reads that base from the root
catalog's `self` link, so `make conformance-live` mirrors the documents
without it (`tools/mirror_docs.py --drop-self`).

The last run over the published catalog, on 2026-10-22 over the Alpine years
2015–2026 (`make conformance-live`, 26 545 files, rashid 0.3.0, every
extension schema), gave 0 errors and only the twelve `PTL-CAT-001` warnings
accepted below.

## Accepted Deviations

| Rule | Where | Why accepted | Tracking |
|---|---|---|---|
| `PTL-COL-005` | both collections, tracked tree only | Each collection registers its `items.parquet` mirror, and a fresh clone holds no items for it to mirror. Not a metadata defect: the finding disappears against any built tree. | none; structural, see below |
| `PTL-AST-007` | both collections, tracked tree only | `PORTO-CORE-082`: a collection refers to its primary data through a `data` asset on itself or its items. Both collections keep their data on their items, which a fresh clone does not hold. The finding disappears against any built tree. | none; structural, as above |

`PTL-COL-005` reads: *"a collection whose scene files or item mirror imply
items must publish them."* Both collections register a `collection-mirror`
asset. In a fresh clone the items it describes are absent, because they are
generated to object storage rather than committed (`.gitignore`:
`catalog/tiles/[0-9][0-9][0-9][0-9]/`). Fields of the World (FTW) does the same,
for the same reason, at 45 000 items.

Leaving the mirror out of the committed `collection.json`, for the generator
to add, was rejected: the committed document would then differ from the
published one, which is worse than a finding whose cause is written down.

### `PORTO-FMT-011` — no `rel: "pmtiles"` links

| Requirement | Where | Why accepted | Tracking |
|---|---|---|---|
| `PORTO-FMT-011`, `PORTO-FMT-012` | mosaics collection, built tree | The web-map archives are item assets with the `visual` role and the collection does not link them. | none yet |

*"A PMTiles file … MUST be registered through a collection-level
`rel: "pmtiles"` link."* The rule is written for a vector collection, which has
one archive. This catalog uses PMTiles the way it uses COGs: one archive per
product and year, twelve a year, 144 across 2015–2026. As collection
links they would form a list that long on the collection page.

rashid does not report the omission: `PTL-VIZ-003` checks only a collection
that carries a PMTiles asset or link, and the mosaics collection carries
neither. The tiles collection conforms: its one archive, the tile grid, is a
`visual` asset with a `rel: "pmtiles"` link and a style (M-38). So the `ACCEPTED` list in `tests/test_conformance.py` does not include
it; it is recorded here so that a clean run is not read as conformance.

The archives are still fully described. Each year's item lists its archives as
assets, with size and checksum. Each year's style draws the archive
`{stem}_viz.pmtiles` as the layer `glace-{stem}-{year}`, and `finalize-catalog`
refuses a store holding an archive its year's style does not draw.

### `PORTO-CORE-069` — year styles on the mosaic items

| Requirement | Where | Why accepted | Tracking |
|---|---|---|---|
| `PORTO-CORE-069` | mosaics collection and its items, built tree | Each year's style is an asset of that year's mosaic item; the collection registers one style, the latest year's. | spec issue drafted, not raised |

*"Style files are STAC assets: each style MUST be registered as a
collection-level asset carrying the `style` role."* A year style draws only
that year's archives, which are assets of the year's mosaic item. Registered on
the collection, the twelve styles crowded its asset list and its style picker.
Each item now carries `style-default` at `./styles/default.json`, beside its
archives, with the `style` and `default` roles, as the year items of the FIRMS
mirror do. The Portolan browser reads a style from the entity that owns the
data it draws, an item included.

rashid does not report it: `PTL-VIZ-002` asks only that a collection with a
visual derivative register some style, and the mosaics collection keeps one.
The `ACCEPTED` list in `tests/test_conformance.py` therefore does not include
it.

## Warnings Carried Deliberately

Neither of these fails the build; both are recorded so that a clean run is not
mistaken for an empty one.

### `PTL-CAT-001` — one per year sub-catalog

*"a catalog or collection with twenty or more ungrouped children should use
subcatalogs."* Each `tiles/{year}/catalog.json` links that year's items
directly, up to 2 210. The rule counts non-catalog children, so one warning
fires per year.

Grouping them would take a third level. UTM zone does not split them finely
enough (five grid zones), and the MGRS 100 km square gives 55 groups of 1 to
100 tiles, so most groups still warn and a fourth level would be needed. Each level is a directory that
Portolan requires to carry its own `catalog.json`, `README.md` and `AGENTS.md`
(`PTL-FIL-001`, `PTL-FIL-002`, `PTL-FIL-003`): for the Alps, hundreds of
generated Markdown files that would only restate their path.

FTW groups by UTM zone and *omits* those files, which costs four rashid errors
per sub-catalog against their published tree. Taking the warning is the better
trade.

`items.parquet`, registered on both collections and the access path the
documentation recommends, serves the browse path this rule protects. Revisit
if browsing the published tree proves painful.

### `PTL-AST-003` — `file:size` and `file:checksum` on collection assets

*"every asset should carry file:size and file:checksum."* The collection-level
assets (`items`, `grid`, `thumbnail`, the styles) point at files
generated during the build, so their bytes do not exist when the tracked
document is rendered. `finalize-catalog.py` fills both fields from the real
bytes on the **store copy** of each collection, which is what is published.
The tracked documents leave the fields out, and an asset whose bytes are
missing at finalize time is warned about and left without them rather than
guessed.

Portolan treats a stale value as a conformance failure and asks a publisher who
cannot keep one current to omit it, so an absent field here is correct. Item
assets carry both fields from the run manifest, whose `sha256` column is stored
multihash-encoded, so the recorded and published values are the same string.

## Advisory Gate

`tests/test_stac_valid.py` runs stac-check, which asks a different question:
is this valid STAC, rather than does it conform to Portolan. It is advisory.

Two of its notes are ignored on purpose:

- **`self` link recommended.** The tracked tree leaves it out, and the publish
  step writes it from `public_base`. `PORTO-CORE-081` recommends the link on a
  *published* catalog, which is where it appears.
- **One tolerated crash**, on every Portolan Collection:

| What | Where | Why accepted | Tracking |
|---|---|---|---|
| stac-validator raises `AttributeError: 'list' object has no attribute 'get'` | both collections | Validator defect, not a metadata defect | [portolan-spec#157](https://github.com/portolan-sdi/portolan-spec/issues/157) |

The Portolan profile schema declares JSON Schema draft-07, in which `items`
may be an array of schemas, and its `valid_bbox` definition uses that form.
stac-validator ignores the declared draft and validates every schema as draft
2020-12, where `items` must be a single schema, and then fails on the list.
The spec repository's own examples crash the same way. `catalog.json` passes,
because only a Collection reaches `valid_bbox`.

The tolerance in `test_stac_valid.py` matches that one error string and
re-validates the document against the schema directly before letting it
through. It prints the count of skipped objects on every run, so the exception
cannot quietly outlive the bug.

Where stac-check and rashid disagree, **rashid wins**.

## Requirements Proved Elsewhere

These rules need more than the tracked metadata to check. Each is listed with
what satisfies it; open work is tracked in [`STATUS.md`](STATUS.md).

| Rule | Requirement | State |
|---|---|---|
| `PTL-DAT-009` | every COG band carries embedded min/max/mean/stddev | **Satisfied for both writers.** The 2015–2026 composites, written by s1bursts 0.6.0, carry the five `STATISTICS_*` tags on float32 data bands and uint16 QA-NUM bands, and in every manifest row (verified 2026-10-16). The exception is a raster with no valid pixel, such as the 2015 backscatter of `32TNT24`, which has no statistics to carry. The annual mosaics are written here: `build_mosaic_cog` computes the statistics in one streaming pass over the source VRT and passes them to freezebase's `write_cog` as band tags, set before the COG encode; `tests/test_mosaic_stats.py` reads them back with PAM disabled |
| `PTL-DAT-010` | a COG band with nodata declares its valid percent | **Satisfied for both writers**, from the same headers and the same pass. The build keys the QA-NUM count to the data asset it counts for; every mosaic band is float32 with NaN nodata |
| `PTL-DAT-006/7/8` | the item mirror is spatially ordered, carries per-row-group spatial statistics, and holds ≤150 000 rows per group | **Implemented**: Hilbert-ordered rows, a float64 `bbox` covering column declared in the `geo` metadata, 2 048-row groups. Gated by `tests/test_mirror.py`, which calls rashid's own checks |
| `PTL-DAT-016` | the item mirror reproduces the collection's items exactly | **Implemented and gated** through rashid's `_check_mirror`, over in-memory catalogs of 400 and 800 items |
| `PTL-LIV-001..005` | the host honours range requests and CORS | **Passes** on Source Cooperative: `rashid check --live` over the Swiss test catalog, 2026-09-08, 0 errors |
