# Portolan Conformance

Conformance means passing [rashid](https://github.com/portolan-sdi/rashid), not
claiming to conform, so it runs in CI:

```bash
python3 tests/test_conformance.py
```

That gate fails on any error-severity finding whose rule is not listed below.
The list must never grow without a row here. A known deviation with an issue
number is a debt someone can pay off. A silently widened allow-list is a false
claim about what this catalog conforms to.

Measured 2026-09-03 against **rashid 0.1.8** and spec **v0.2.0**.

## What the gate actually reads

The gate runs against the **tracked tree**, which holds the hand-maintained
documents and none of the generated ones. That is three objects: the root
catalog and two collections.

The **published** tree is what conformance is really a claim about, and it is
larger by every generated item. It was validated on 2026-09-03 against a
generated fixture — one year, then twenty-five items in that year, with the
same collection documents that are committed here:

```
0 error(s), 14 warning(s), 0 info(s) across 7 files      # 2-item year
0 error(s), 15 warning(s), 0 info(s) across 30 files     # 25-item year
```

Re-run this against the built store before every publication:

```bash
rashid check "$GLACE_STORE_ROOT" --schema --data
```

## Accepted Deviations

| Rule | Where | Why accepted | Tracking |
|---|---|---|---|
| `PTL-COL-005` | both collections, tracked tree only | Each collection registers its `items.parquet` mirror, and a fresh clone holds no items for it to mirror. Not a metadata defect — the finding disappears against any built tree. | none; structural, see below |

`PTL-COL-005` reads: *"a collection whose scene files or item mirror imply items
must publish them."* Both collections register a `collection-mirror` asset, and
in a fresh clone the item tree that mirror describes is absent, because the
items are generated to object storage rather than committed
(`.gitignore`: `catalog/tiles/[0-9][0-9][0-9][0-9]/`). Fields of the World does
the same thing for the same reason, at 45 000 items.

The alternative — leaving the mirror registration out of the committed
`collection.json` and having the generator add it — was rejected. It would make
the committed document differ from the published one, which is a worse property
than a finding whose cause is written down.

## Warnings Carried Deliberately

Neither of these fails the build; both are recorded so a clean run is not
mistaken for an empty one.

### `PTL-CAT-001` — one per year sub-catalog

*"a catalog or collection with twenty or more ungrouped children should use
subcatalogs."* Each `tiles/{year}/catalog.json` links that year's items
directly: 143 for Switzerland, a projected ~2 209 for the Alps. The rule counts
non-catalog children, so one warning fires per year.

Grouping them would mean a third level. UTM zone does not work here — every
Swiss tile is in 32T and the Alps span about three zones — so it would have to
be the MGRS 100 km square, which for Switzerland gives 9 groups of 1 to 56
tiles: the largest still warn, and a fourth level would be needed to get below
twenty. Every one of those levels is a directory Portolan requires to carry its
own `catalog.json`, `README.md` and `AGENTS.md` (`PTL-FIL-001`, `PTL-FIL-002`,
`PTL-FIL-003`), which for the Alps is hundreds of generated Markdown files whose
content would restate their own path.

Fields of the World groups by UTM zone and *omits* those files; measured against
the published tree, that is four rashid errors per sub-catalog. Their CI does not
see it because the zone catalogs are generated and gitignored. Taking the warning
is the better trade.

The browse path this rule protects is served instead by `items.parquet`, which
is registered on both collections and is what the documentation points every
consumer at. Revisit if browsing the published tree proves genuinely painful.

### `PTL-AST-003` — `file:size` and `file:checksum` on collection assets

*"every asset should carry file:size and file:checksum."* The collection-level
assets (`items`, `glacier-fractions`, `thumbnail`) point at generated files whose
bytes do not exist when the collection document is written by hand.
`finalize-catalog.py` fills both fields from the real bytes — on the **store
copy** of the document, which is what is published; the tracked document keeps
the fields absent, and an asset whose bytes are missing at finalize time is
warned about and left unstamped rather than guessed.

Portolan is explicit that a stale value is a conformance failure and that a
publisher who cannot keep one current should omit it, so an absent field here is
the correct state, not a shortcut. Item assets carry both from the run manifest,
whose `sha256` column is stored multihash-encoded so the value recorded and the
value published are the same string.

## Advisory Gate

`tests/test_stac_valid.py` runs stac-check, which answers a different question:
is this valid STAC, rather than does it conform to Portolan. It is advisory.

Two of its notes are ignored on purpose:

- **`self` link recommended.** The tracked tree deliberately omits it; the
  publish step writes it from `public_base`. `PORTO-CORE-081` recommends the
  link on a *published* catalog, and that is where it appears.
- **One tolerated crash**, on every Portolan Collection:

| What | Where | Why accepted | Tracking |
|---|---|---|---|
| stac-validator raises `AttributeError: 'list' object has no attribute 'get'` | both collections | Validator defect, not a metadata defect | [portolan-spec#157](https://github.com/portolan-sdi/portolan-spec/issues/157) |

The Portolan profile schema declares draft-07, in which `items` may be an array
of schemas; its `valid_bbox` definition uses that form. stac-validator ignores
the declared draft and pushes every schema through `Draft202012Validator`, where
`items` must be a single schema, and `referencing` then calls `.get("$id")` on a
list. The spec repository's own reference examples crash identically, so the
defect is not specific to this catalog. `catalog.json` passes, because only a
Collection reaches `valid_bbox`.

The tolerance in `test_stac_valid.py` matches that one error string and
re-validates the document against the schema directly before letting it through.
The count of skipped objects prints on every run, so the exception cannot quietly
outlive the bug.

Where stac-check and rashid disagree, **rashid wins**.

## Requirements Proved Elsewhere, and the One Still Open

One rule group is still unproven, and it is a property of the host rather than of
the metadata. The rest of this section's former entries have landed; they are
kept here with what closed them, because a reader wants to know the claim is
tested rather than assumed. Open work is tracked in [`STATUS.md`](STATUS.md).

| Rule | Requirement | State |
|---|---|---|
| `PTL-DAT-009` | every COG band carries embedded min/max/mean/stddev | **satisfied for both writers.** The Swiss 2021–2024 composites were regenerated with s1bursts 0.5.0 and verified on 2026-09-08 against real headers: the five `STATISTICS_*` tags on both a float32 data band and the uint16 QA-NUM band, and on every row of all four manifests. The annual mosaics are written *here*, so `build_mosaic_cog` takes the statistics in one streaming pass over the source VRT and writes them onto the band before the COG copy; `tests/test_mosaic_stats.py` reads them back with PAM disabled |
| `PTL-DAT-010` | a COG band with nodata declares its valid percent | **satisfied for both writers**, from the same headers and the same pass; the publisher keys the QA-NUM count to the data asset it counts for, and every mosaic band is float32 with a NaN sentinel |
| `PTL-DAT-006/7/8` | the item mirror is spatially ordered, carries per-row-group spatial statistics, and holds ≤150 000 rows per group | **implemented**: Hilbert-ordered rows, a float64 `bbox` covering column declared in the `geo` metadata, 2 048-row groups. Gated by `tests/test_mirror.py`, which calls rashid's own checks and fails when the ordering is removed |
| `PTL-DAT-016` | the item mirror reproduces the collection's items exactly | **implemented and gated**, through rashid's `_check_mirror` over an in-memory catalog graph of 400 and then 800 items |
| `PTL-LIV-001..005` | the host honours range requests and CORS | **untested.** Source Cooperative is expected to satisfy it — a manual check returned 206 for a range request — and `rashid check --live` against `public_base` is what will say so (STATUS.md P-4) |
