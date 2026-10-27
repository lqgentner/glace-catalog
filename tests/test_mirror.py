#!/usr/bin/env python3
"""Checks the item index against the GeoParquet rules rashid enforces.

Each collection's ``items.parquet`` (the "mirror" in the code) holds one row
per item. Portolan requires spatially ordered rows (``PTL-DAT-006``),
per-row-group spatial statistics over a ``bbox`` covering column
(``PTL-DAT-007``), bounded row groups (``PTL-DAT-008``), and one row matching
each of the collection's items (``PTL-DAT-016``). This gate imports rashid's
own check functions and uses them as its assertions, so it follows rashid's
reading of the spec automatically.

The fixture is 400 synthetic tile items on a 20 x 20 MGRS grid, with the ids,
geometries and datetimes of real tile items; their tags are covered by
``tests/test_items.py``. 400 rows is enough for rashid's row-ordering
measurement to apply, and builds in about a second.

Negative controls show that the gate can fail: shuffled rows must fail the
ordering rule, a file without its covering declaration must fail the
statistics rule, and the writer must refuse to write such a file.

Reads no data and needs no network.

Run: uv run tests/test_mirror.py
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
import pyarrow as pa
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
    _write_mirror,
    concat_indexes,
    write_stac_geoparquet,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rashid.data import DataDefect

GRID_SQUARES = ("MS", "MT", "NS", "NT")
"""Four adjacent 100 km MGRS squares over the Swiss Alps, 100 tiles each."""

COLLECTION_ID = "glace-ch-tiles"
MIRROR_KEY = "items"
BASE_URL = "https://data.source.coop/giuz/glace-alps"

# Small row groups give the 400-row fixture enough groups (rashid needs at least
# five) for its row-group metrics to apply. The real store gets there with the
# writer's default of 2 048 rows per group over about 24 000 rows.
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

    Shuffled deterministically, because tile-code order is already a
    column-by-column scan of this grid and would pass the ordering rule by
    itself. The shuffle makes the writer responsible for the ordering.
    """
    order = np.random.default_rng(year).permutation(len(tiles))
    return [_item_payload(tiles[int(i)], year) for i in order]


def _locator(path: Path) -> Locator:
    """Point rashid's data checks at a local parquet file."""
    return Locator(is_remote=False, source=str(path))


def _graph(payloads: Sequence[dict[str, Any]], root: Path) -> tuple[CatalogGraph, Node]:
    """Build an in-memory catalog holding one collection and its items.

    rashid assigns each item to the nearest collection above it in the file
    tree, so the nodes need only paths and parsed JSON.
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
    column = geo.get("columns", {}).get("geometry", {})
    bboxes = parquet.read(columns=["bbox"]).column("bbox").to_pylist()
    checks[f"{label}: the geo metadata names the geometry types"] = column.get(
        "geometry_types"
    ) == ["Polygon"]
    checks[f"{label}: the geo metadata's bbox spans every row"] = column.get("bbox") == [
        min(box["xmin"] for box in bboxes),
        min(box["ymin"] for box in bboxes),
        max(box["xmax"] for box in bboxes),
        max(box["ymax"] for box in bboxes),
    ]
    checks[f"{label}: every column is zstd-compressed"] = all(
        parquet.metadata.row_group(group).column(index).compression == "ZSTD"
        for group in range(groups)
        for index in range(parquet.metadata.num_columns)
    )


def _shuffled_copy(path: Path, out_path: Path) -> None:
    """Rewrite a mirror with its rows shuffled, keeping everything else.

    The negative control for the ordering rule: same schema, same metadata, same
    row-group size; only the row order differs.
    """
    table = pq.read_table(path)
    order = np.random.default_rng(0).permutation(table.num_rows)
    with out_path.open("wb") as handle:
        pq.write_table(table.take(order), handle, row_group_size=SMALL_ROW_GROUP)


def _uncovered_copy(path: Path, out_path: Path) -> None:
    """Rewrite a mirror without its covering declaration.

    The negative control for the statistics rule: the copy keeps the ``bbox``
    column but removes its declaration as the geometry's covering from the
    ``geo`` metadata, so rashid must report the statistics as unusable.
    """
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    geo = json.loads(metadata[b"geo"])
    del geo["columns"]["geometry"]["covering"]
    metadata[b"geo"] = json.dumps(geo).encode()
    with out_path.open("wb") as handle:
        pq.write_table(table.replace_schema_metadata(metadata), handle)


def _year_of(item_id: str) -> str:
    """Read the year an item id ends with."""
    return item_id[-4:]


def _writer_checks(root: Path) -> dict[str, bool]:
    """Check what the writer does with input it should not accept quietly.

    Arrow infers ``int64`` for whole-number bboxes; the writer must store them
    as ``float64`` so that every year concatenates with the others. A table
    with no covering declaration must be refused, because its row groups would
    carry no usable spatial statistics.
    """
    payloads = [_item_payload(tile, 2023) for tile in _tile_codes()[:5]]
    for i, payload in enumerate(payloads):
        payload["bbox"] = [i, 0, i + 1, 1]
        payload["geometry"] = {
            "type": "Polygon",
            "coordinates": [[[i, 0], [i + 1, 0], [i + 1, 1], [i, 1], [i, 0]]],
        }
    whole = root / "whole-numbers.parquet"
    write_stac_geoparquet(payloads, whole)
    refused = False
    table = pq.read_table(whole)
    metadata = dict(table.schema.metadata or {})
    geo = json.loads(metadata[b"geo"])
    del geo["columns"]["geometry"]["covering"]
    metadata[b"geo"] = json.dumps(geo).encode()
    try:
        _write_mirror(table.replace_schema_metadata(metadata), root / "refused.parquet")
    except ValueError:
        refused = True
    return {
        "whole-number bboxes are still written as float64": pq.ParquetFile(whole)
        .schema_arrow.field("bbox")
        .type
        == pa.struct([(corner, pa.float64()) for corner in ("xmin", "ymin", "xmax", "ymax")]),
        "a table with no covering declaration is refused": refused,
    }


def main() -> int:
    """Run the gate."""
    checks: dict[str, bool] = {}
    tiles = _tile_codes()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        checks |= _writer_checks(root)
        first = _year_payloads(tiles, 2023)
        second = _year_payloads(tiles, 2024)
        # A tile missing one polarization leaves a null in that asset's struct
        # field for its row.
        for payload in second[1:]:
            payload["assets"]["coh12_vh"] = {
                **payload["assets"]["coh12_vv"],
                "href": payload["assets"]["coh12_vv"]["href"].replace("_vv", "_vh"),
            }

        year_paths = []
        for year, payloads in ((2023, first), (2024, second)):
            path = root / f"{year}.parquet"
            write_stac_geoparquet(payloads, path)
            year_paths.append(path)
        _check_file(year_paths[0], checks, "one year")

        # At the writer's default, a 400-row year is a single row group, so rashid
        # measures the rows themselves. Rewritten in small row groups, the same
        # rows also go through the row-group metrics that apply to the full index.
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
            if asset is not None
        }
        checks["two years concatenated: the asset hrefs are rebased"] = all(
            href.startswith(f"{BASE_URL}/tiles/") for href in hrefs
        )
        # Rows are sorted spatially across both years, so the year changes many
        # times along the file.
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
