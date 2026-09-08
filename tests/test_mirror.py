#!/usr/bin/env python3
"""Gate on the item mirror against the GeoParquet rules rashid enforces.

Portolan holds an ``items.parquet`` to the full vector rules: spatially ordered
rows (``PTL-DAT-006``), per-row-group spatial statistics over a ``bbox``
covering column (``PTL-DAT-007``), bounded row groups (``PTL-DAT-008``), and one
row reproducing each of the collection's items (``PTL-DAT-016``). Decision R-12
says those rules are what the mirror satisfies *as rashid implements them*, so
this gate imports rashid's own check functions and uses them as its assertions
rather than restating them. If rashid's reading of the spec changes, this gate
changes with it and there is nothing to keep in step by hand.

The fixture is 400 synthetic tile items on a 20 x 20 MGRS grid — the ids,
geometries and datetimes a real tile item carries, without the tags
``tests/test_items.py`` owns. 400 rows is above the floor below which rashid's
row-ordering measurement says nothing, and small enough to build in a second.

Two negative controls keep the gate honest: the same rows in shuffled order must
fail the ordering rule, and a mirror stripped of its covering declaration must
fail the statistics rule. Without them a gate that only ever sees conforming
files cannot tell that it is checking anything.

Reads no data and needs no network.

Run: python3 tests/test_mirror.py
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path, PurePosixPath
import sys
import tempfile
from typing import TYPE_CHECKING, Any

from freezebase.mgrs import MGRSGeoBox
import numpy as np
import pyarrow.parquet as pq
from rashid.catalog import CatalogGraph, Node
from rashid.data import DAT_ORDERING, DAT_ROWGROUP_STATS
from rashid.data.checks import (
    _MAX_ROW_GROUP_ROWS,
    _bbox_struct,
    _check_geoparquet,
    _check_mirror,
    _covering_bboxes,
    _geo_metadata,
    _is_spatially_ordered,
    _rowgroup_stat_defects,
)
from rashid.data.reader import Locator

from glace_catalog.stac.save import (
    HILBERT_ORDER,
    _write_mirror,
    concat_indexes,
    hilbert_index,
    write_stac_geoparquet,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rashid.data import DataDefect

GRID_SQUARES = ("MS", "MT", "NS", "NT")
"""Four adjacent 100 km MGRS squares over the Swiss Alps, 100 tiles each."""

COLLECTION_ID = "glace-ch-tiles"
MIRROR_KEY = "items"
BASE_URL = "https://data.source.coop/lqgentner/glace-ch"

# Enough groups for rashid's row-group metrics to apply (its floor is five) over
# a 400-row fixture. The store reaches the same place at the writer's default,
# 2 048 rows over some 24 000; a fixture that large would cost a minute to build.
SMALL_ROW_GROUP = 64


def _tile_codes() -> list[str]:
    """Build the 400 MGRS tile codes of the fixture grid."""
    return [
        f"32T{square}{easting}{northing}"
        for square in GRID_SQUARES
        for easting in range(10)
        for northing in range(10)
    ]


def _item_payload(tile: str, year: int) -> dict[str, Any]:
    """One synthetic tile item, as the store writes it into the mirror."""
    extent = MGRSGeoBox.from_mgrs(tile).geographic_extent
    return {
        "type": "Feature",
        "stac_version": "1.1.0",
        "id": f"{tile}-{year}",
        "collection": COLLECTION_ID,
        "geometry": extent.geom.__geo_interface__,
        "bbox": list(extent.boundingbox.bbox),
        "properties": {
            "datetime": f"{year}-09-01T00:00:00Z",
            "start_datetime": f"{year}-07-18T00:00:00Z",
            "end_datetime": f"{year}-10-16T00:00:00Z",
            "glace:mgrs_tile": tile,
        },
        "links": [],
        "assets": {
            "coh12_vv": {
                "href": f"tiles/{year}/{tile}/coh12_vv.tif",
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                "roles": ["data"],
            },
        },
    }


def _year_payloads(tiles: Sequence[str], year: int) -> list[dict[str, Any]]:
    """One year of items, in an order that carries no spatial information.

    Shuffled deterministically rather than left in tile-code order, which on
    this grid is already a column-by-column scan and would pass the ordering
    rule on its own. The mirror must be ordered because the writer orders it,
    not because the run happened to hand the items over in a helpful sequence.
    """
    order = np.random.default_rng(year).permutation(len(tiles))
    return [_item_payload(tiles[int(i)], year) for i in order]


def _locator(path: Path) -> Locator:
    """Point rashid's data checks at a local parquet file."""
    return Locator(is_remote=False, source=str(path))


def _graph(payloads: Sequence[dict[str, Any]], root: Path) -> tuple[CatalogGraph, Node]:
    """Build an in-memory catalog holding one collection and its items.

    rashid derives ownership from the file tree alone — an item belongs to the
    nearest collection above it — so the nodes need paths and parsed JSON, not
    files on disk.
    """
    collection_path = PurePosixPath("tiles/collection.json")
    collection = Node(
        path=collection_path,
        abs_path=root / collection_path,
        kind="collection",
        id=COLLECTION_ID,
        data={"type": "Collection", "id": COLLECTION_ID},
    )
    nodes = {collection_path: collection}
    for payload in payloads:
        tile = payload["properties"]["glace:mgrs_tile"]
        year = payload["properties"]["datetime"][:4]
        path = PurePosixPath(f"tiles/{year}/{tile}/item.json")
        nodes[path] = Node(
            path=path,
            abs_path=root / path,
            kind="item",
            id=payload["id"],
            data=payload,
        )
    return CatalogGraph(root_path=root, nodes=nodes), collection


def _rule_ids(defects: Sequence[DataDefect]) -> set[str]:
    """Rule ids of a check's findings, for asserting on what it reported."""
    return {defect.rule_id for defect in defects}


def _covering_paths(path: Path) -> dict[str, list[str]] | None:
    """Read the ``geo`` metadata's covering declaration for the geometry column."""
    geo = _geo_metadata(pq.ParquetFile(path)) or {}
    columns = geo.get("columns", {})
    return columns.get(geo.get("primary_column"), {}).get("covering", {}).get("bbox")


def _check_file(path: Path, checks: dict[str, bool], label: str) -> None:
    """Run rashid's GeoParquet rules over one mirror and record what they said."""
    parquet = pq.ParquetFile(path)
    geo = _geo_metadata(parquet) or {}
    boxes, stat_defects = _rowgroup_stat_defects(MIRROR_KEY, parquet, geo, subject=label)
    groups = parquet.metadata.num_row_groups
    rows = [parquet.metadata.row_group(i).num_rows for i in range(groups)]
    checks[f"{label}: rashid's GeoParquet rules pass"] = (
        _check_geoparquet(MIRROR_KEY, _locator(path)) == []
    )
    checks[f"{label}: the geo metadata names the bbox covering column"] = _covering_paths(
        path
    ) == {
        "xmin": ["bbox", "xmin"],
        "ymin": ["bbox", "ymin"],
        "xmax": ["bbox", "xmax"],
        "ymax": ["bbox", "ymax"],
    }
    checks[f"{label}: the covering column is a readable bbox struct"] = _bbox_struct(
        parquet,
        geo,
    ) == ("bbox", ["xmin", "ymin", "xmax", "ymax"])
    checks[f"{label}: every row group carries bbox min/max statistics"] = (
        stat_defects == [] and boxes is not None and len(boxes) == groups
    )
    checks[f"{label}: no row group exceeds the ceiling"] = max(rows) <= _MAX_ROW_GROUP_ROWS


def _shuffled_copy(path: Path, out_path: Path) -> None:
    """Rewrite a mirror with its rows shuffled, keeping everything else.

    The negative control for the ordering rule: same schema, same metadata, same
    row-group size, only the row order thrown away.
    """
    table = pq.read_table(path)
    order = np.random.default_rng(0).permutation(table.num_rows)
    with out_path.open("wb") as handle:
        pq.write_table(table.take(order), handle, row_group_size=SMALL_ROW_GROUP)


def _uncovered_copy(path: Path, out_path: Path) -> None:
    """Rewrite a mirror without its covering declaration.

    The negative control for the statistics rule: the ``bbox`` column stays, but
    nothing in the ``geo`` metadata says it covers the geometry, so a reader
    cannot use its statistics and rashid must say so.
    """
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    geo = json.loads(metadata[b"geo"])
    del geo["columns"]["geometry"]["covering"]
    metadata[b"geo"] = json.dumps(geo).encode()
    with out_path.open("wb") as handle:
        pq.write_table(table.replace_schema_metadata(metadata), handle)


def _hilbert_checks() -> dict[str, bool]:
    """Check the curve itself: it visits every cell once, in single steps."""
    order = 4
    side = 2**order
    cells = [(x, y) for x in range(side) for y in range(side)]
    indexes = {hilbert_index(x, y, order=order): (x, y) for x, y in cells}
    walk: list[tuple[int, int]] = [indexes[i] for i in sorted(indexes)]
    steps = [abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in itertools.pairwise(walk)]
    return {
        "the Hilbert index is a bijection over the grid": sorted(indexes) == list(range(side**2)),
        "consecutive Hilbert cells are neighbours": steps != [] and set(steps) == {1},
        "the curve starts at the origin": hilbert_index(0, 0, order=HILBERT_ORDER) == 0,
    }


def _year_of(item_id: str) -> str:
    """Read the year an item id ends with."""
    return item_id[-4:]


def main() -> int:
    """Run the gate."""
    checks = _hilbert_checks()
    tiles = _tile_codes()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = _year_payloads(tiles, 2023)
        second = _year_payloads(tiles, 2024)

        year_paths = []
        for year, payloads in ((2023, first), (2024, second)):
            path = root / f"{year}.parquet"
            write_stac_geoparquet(payloads, path)
            year_paths.append(path)
        _check_file(year_paths[0], checks, "one year")

        # The per-year file holds one row group at the writer's default, which is
        # right for 400 rows and leaves rashid measuring the rows themselves.
        # Regrouped small, the same rows go through the row-group metrics the
        # store-level mirror will be judged by.
        grouped = root / "grouped.parquet"
        _write_mirror(pq.read_table(year_paths[0]), grouped, row_group_size=SMALL_ROW_GROUP)
        _check_file(grouped, checks, "several row groups")
        parquet = pq.ParquetFile(grouped)
        boxes = _covering_bboxes(parquet, _geo_metadata(parquet) or {})
        checks["several row groups: the row groups are spatially ordered"] = (
            boxes is not None and len(boxes) >= 5 and _is_spatially_ordered(boxes)
        )

        shuffled = root / "shuffled.parquet"
        _shuffled_copy(grouped, shuffled)
        checks["shuffled rows fail the ordering rule"] = DAT_ORDERING in _rule_ids(
            _check_geoparquet(MIRROR_KEY, _locator(shuffled)),
        )
        uncovered = root / "uncovered.parquet"
        _uncovered_copy(year_paths[0], uncovered)
        checks["a mirror with no covering declaration fails the statistics rule"] = (
            DAT_ROWGROUP_STATS in _rule_ids(_check_geoparquet(MIRROR_KEY, _locator(uncovered)))
        )

        graph, collection = _graph(first, root)
        checks["one year: the mirror reproduces the collection's items"] = (
            _check_mirror(collection, MIRROR_KEY, _locator(year_paths[0]), graph) == []
        )

        store = root / "items.parquet"
        rows = concat_indexes(year_paths, store, base_url=BASE_URL)
        _check_file(store, checks, "two years concatenated")
        checks["two years concatenated: one row per item"] = rows == len(first) + len(second)
        both, both_collection = _graph([*first, *second], root)
        checks["two years concatenated: the mirror reproduces every item"] = (
            _check_mirror(both_collection, MIRROR_KEY, _locator(store), both) == []
        )
        table = pq.read_table(store)
        hrefs = {
            asset["href"]
            for assets in table.column("assets").to_pylist()
            for asset in assets.values()
        }
        checks["two years concatenated: the asset hrefs are rebased"] = all(
            href.startswith(f"{BASE_URL}/tiles/") for href in hrefs
        )
        # Ordered across the years, not concatenated in year order: a store-level
        # mirror that kept the parts end to end would change year exactly once.
        years = [_year_of(item_id) for item_id in table.column("id").to_pylist()]
        switches = sum(1 for a, b in itertools.pairwise(years) if a != b)
        checks["two years concatenated: the rows are ordered across the years"] = switches > 1

    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        return 1
    print(f"OK: {len(checks)} mirror check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
