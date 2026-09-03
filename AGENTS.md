# glace-catalog — repository guide

This is the **repository**, not the published catalog. The published catalog's
own agent guides are `catalog/AGENTS.md` and one per collection; read those to
*use* the data. Read this one to *change* it.

## The one rule

**Edit the generator, not the generated output.** Everything under
`catalog/tiles/{year}/`, `catalog/mosaics/{year}/`, and every `items.parquet`
is written by `scripts/publish-year.py` and `scripts/finalize-catalog.py`. A
hand edit there is overwritten on the next run and is not in git to begin with.

The hand-maintained documents are: `catalog/catalog.json`, both
`collection.json`, and every `README.md` / `AGENTS.md` down to the collection
level. Those are committed and reviewed.

## Publication model

Two uploaders, one bucket prefix, and they do not overlap:

- `tools/publish.py` syncs `catalog/` — metadata only. It compares local size
  and MD5 against the remote listing's size and ETag and keeps no state.
- `tools/upload_data.py` syncs the built rasters and parquet from `data_dir`,
  scoped by an allow-list of file suffixes.

Publishing never deletes. Removing a file from `catalog/` leaves the object in
the bucket; delete it separately if that is what you meant.

`portolan push` is **not** used here and must not be mixed in — it tracks
uploads in `versions.json` keyed on sha256, which the template publisher knows
nothing about.

## Gates

```bash
python3 tests/run_all.py
```

`rashid` is the Portolan conformance gate; `stac-check` is advisory. Where they
disagree, rashid wins. Do not widen the `ACCEPTED` set in
`tests/test_conformance.py` to make CI pass — add a row to
`docs/conformance.md` with a tracking issue, or fix the finding. Both, or
neither.

The gates run against the tracked tree, which has no generated items in it. Two
findings are structural consequences of that and are recorded in
`docs/conformance.md`; the full gate runs against a built tree before publication.

## Layout invariants worth not breaking

- **Collections own their subtrees.** `tiles/{year}/{tile}/item.json`, not
  `{year}/tiles/...`. Portolan requires a collection directory to hold its items,
  and a catalog may only appear *below* a collection to organize them.
- **A catalog carries no assets.** Only collections and items may. That is why
  each year's `manifest.parquet` is a *collection* asset keyed `manifest-{year}`
  rather than an asset of the year catalog that sits beside it.
- **Structural links are relative; asset hrefs are absolute https.** The first
  keeps the tree valid in the repo, in a preview and in production. The second
  is what makes assets resolve for a client reading one object in isolation, and
  what makes them clickable in the Source Cooperative file listing. `s3://`
  appears only under the `alternate` extension, never as a primary href.
- **The `self` link is not in the tracked tree.** The publish step writes it
  from `public_base`. A tracked file whose correct content depends on the
  deployment target is diff noise and a merge conflict waiting to happen.
- **`visual` assets are never `data`.** The false-colour rasters are 8-bit
  display codes under a fixed stretch and `rtc_rgb` is in dB while everything
  else is linear. Marking one `data` would let an analysis pipeline read a
  picture.

## Related repositories

- `../deep-glacier-mapping` — composite production, labels, ML. Produces what
  this catalog publishes.
- `../s1bursts` — the Sentinel-1 pipeline. Its COG writer must emit embedded
  band statistics or nothing here can pass `PTL-DAT-009`.
- `../freezebase` — geo and IO helpers, including the VRT chain the mosaics use.

Both first-party libraries are installed editable via `[tool.uv.sources]` while
the transition lands. Cut releases and drop the overrides before the Alps
backfill: an editable path is not reproducible from a checkout.
