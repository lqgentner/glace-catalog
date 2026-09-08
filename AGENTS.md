# glace-catalog — repository guide

This is the **repository**, not the published catalog. The published catalog's
own agent guides are `catalog/AGENTS.md` and one per collection; read those to
*use* the data. Read this one to *change* it.

## Scope

`glace-ch` is a rehearsal over Switzerland. The dataset is `glace-alps`, and
every mention of Switzerland in `catalog/` is placeholder text with a removal
date. `src/glace_catalog/scopes.py` is the seam: it already carries both scopes
as committed constants, so the swap is a scope change below it and a prose
rewrite above it. `docs/DESIGN.md` §8 says what changes; `docs/STATUS.md` carries
the checklist.

## The one rule

**Edit the generator, not the generated output.** Everything under
`catalog/tiles/{year}/`, `catalog/mosaics/{year}/`, and every `items.parquet`
is written by `scripts/publish-year.py` and `scripts/finalize-catalog.py`. A
hand edit there is overwritten on the next run and is not in git to begin with.

The hand-maintained documents are: `catalog/catalog.json`, both
`collection.json`, and every `README.md` / `AGENTS.md` down to the collection
level. Those are committed and reviewed.

## Publication model

One uploader, one bucket prefix. `tools/sync_data.py` streams every object
under `GLACE_STORE_ROOT` to the `write_prefix` in `catalog.publish.yaml`,
documents and data alike: the store *is* the published tree, and its collection
documents are the tracked ones that `finalize-catalog` patched on the way in.
Syncing the tracked `catalog/` over them would strip those patches, which is why
nothing does. Nothing is staged anywhere in between; a `GetObject` body on the
private gateway is handed straight to `upload_fileobj` on the target. Change
detection is size against size from one listing per side; `--force` re-uploads
regardless. Build state (`.build-state.json`, `.items.parquet`, anything whose
name starts with a dot) never leaves the store.

**Source Cooperative is addressed through its S3 proxy.** The `source-coop`
profile carries `endpoint_url = https://data.source.coop`, and against that
endpoint the bucket is the *account* (`lqgentner`) and the key starts with the
*product* (`glace-ch/…`) — which is why `write_prefix` is
`s3://lqgentner/glace-ch` and not one of the AWS-native bucket names, which
reject these credentials. Public reads use `https://data.source.coop/...` or
anonymous S3 against `eu-central-1.opendata.source.coop`, which is the form the
`alternate` hrefs carry.

Publishing never deletes. Removing a file from the store leaves the object in
the bucket; delete it separately if that is what you meant.

`portolan push` is **not** used here and must not be mixed in — it tracks
uploads in `versions.json` keyed on sha256, which `sync_data.py` knows nothing
about.

## Gates

```bash
python3 tests/run_all.py
```

`tests/test_lint.py` runs `ruff format --check`, `ruff check` and `pyrefly
check` under `preset = "strict"`; a missing tool fails rather than skips.
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
- **A catalog carries no assets.** Only collections and items may. The year
  catalogs group items and hold nothing else; the run's manifest and
  configuration are not published at all (M-32).
- **Structural links are relative; asset hrefs are absolute https.** The first
  keeps the tree valid in the repo, in a preview and in production. The second
  is what makes assets resolve for a client reading one object in isolation, and
  what makes them clickable in the Source Cooperative file listing. `s3://`
  appears only under the `alternate` extension, never as a primary href.
- **The `self` link is not in the tracked tree.** The publish step writes it
  from `public_base`. A tracked file whose correct content depends on the
  deployment target is diff noise and a merge conflict waiting to happen.
- **Renderings are never `data`.** Every COG in the catalog is linear; the only
  display pixels are inside the PMTiles archives, which are 8-bit codes under a
  fixed stretch and, for backscatter and QA-CQM, in decibels. They are registered
  as `rel: "pmtiles"` links and `style` assets, never as a `data` asset, because
  marking one `data` would let an analysis pipeline read a picture.

## Related repositories

**If you are working in one of them, read [`docs/UPSTREAM.md`](docs/UPSTREAM.md),
not this file.** It is the per-repository work order: what to change, why, and
how to know it worked.

- `../deep-glacier-mapping` — composite production, labels, ML. Produces what
  this catalog publishes, including the run manifest (M-31, `docs/UPSTREAM.md`
  D-4).
- `../s1bursts` — the Sentinel-1 pipeline. Its COG writer emits the embedded band
  statistics `PTL-DAT-009` needs and the digest `file:checksum` publishes, both
  released in 0.5.0.
- `../freezebase` — geo and IO helpers, including the VRT chain the mosaics use.

**s1bursts is not a dependency of this repository**: it is the writer whose
output this repository reads, and nothing here imports it. `freezebase` is a
dependency and is consumed from PyPI, floored at `>=0.7,<0.8`; there are no
editable installs and `uv.lock` is tracked.
