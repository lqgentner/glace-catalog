"""Tile a raster into a PMTiles archive.

Adapted from **rio-pmtiles 1.2.1** (``rio_pmtiles/worker.py`` and
``rio_pmtiles/scripts/cli.py``), Copyright (c) 2025 Protomaps LLC and 2014-2021
Mapbox, released under the MIT License; the full notice is in
``THIRD_PARTY_NOTICES.md`` at the repository root. rio-pmtiles is itself derived
from Sean Gillies's rio-mbtiles.

The tiles of every layer in a year are rendered in one shared
:class:`~concurrent.futures.ProcessPoolExecutor`, and archives are written by
:class:`pmtiles.writer.Writer` from the PMTiles package. This module warps
single tiles and lists which tiles to warp. :func:`render_tile` chooses the
overview level separately for each tile.

The reprojection call and image creation options follow rio-pmtiles, with
three differences, each explained where it applies: tile images declare no
nodata value, a tile is empty when its own footprint holds no data, and alpha
is binary with the bands zeroed beneath it. These rules keep the value codes
in the bands (:mod:`glace_catalog.encoding`) intact.
"""

from __future__ import annotations

import atexit
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
import functools
import logging
import math
import multiprocessing
import os
from typing import TYPE_CHECKING, Any

import numpy as np
from pmtiles.tile import Compression, TileType, tileid_to_zxy, zxy_to_tileid
from pmtiles.writer import Writer
import rasterio
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, intersect
from rasterio.windows import from_bounds as window_from_bounds

from glace_catalog.scopes import MERCATOR_ORIGIN, MERCATOR_SPAN_M, TILE_PX, WEB_MERCATOR

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

logger = logging.getLogger(__name__)

WRITER = "glace-catalog tiler, adapted from rio-pmtiles 1.2.1"
"""The ``writer`` field of every archive's metadata."""

TILE_TYPES: dict[str, TileType] = {
    "WEBP": TileType.WEBP,
    "PNG": TileType.PNG,
}
"""PMTiles tile type for each supported GDAL image driver.

Both formats store the alpha band that every tile carries.
"""

LOSSY_FORMATS = frozenset({"WEBP"})
"""The formats that accept a ``QUALITY`` option."""

EDGE_EPS = 1e-6
"""Tolerance for an edge to count as lying on a boundary, in tiles or in pixels."""

OVERSAMPLING_FACTOR = 4
"""Minimum number of source pixels per tile pixel, along each axis.

A tile is read from an overview only if the overview still provides this many
source pixels per tile pixel (the rule rio-pmtiles uses).
"""


@dataclass(frozen=True)
class TileSpec:
    """How one tile is rendered: its size, encoding and resampling.

    Parameters
    ----------
    max_zoom
        Zoom the source's full resolution serves. Coarser tiles may be read
        from an overview of the source, if it carries any.
    tile_size
        Side of one tile, in pixels.
    image_format
        GDAL driver the tile is encoded with: ``WEBP`` or ``PNG``.
    quality
        Encoder quality for the lossy formats.
    resampling
        Warp kernel.
    exclude_empty
        Skip a tile whose source window holds no valid pixel at all.
    """

    max_zoom: int
    tile_size: int = TILE_PX
    image_format: str = "WEBP"
    quality: int = 90
    resampling: Resampling = Resampling.average
    exclude_empty: bool = True


def tile_bounds(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Extent of one WebMercatorQuad tile as ``(left, bottom, right, top)``, in metres."""
    span = MERCATOR_SPAN_M / 2**zoom
    left = MERCATOR_ORIGIN + x * span
    top = -MERCATOR_ORIGIN - y * span
    return left, top - span, left + span, top


def tiles_covering(
    bounds: tuple[float, float, float, float],
    zooms: Iterable[int],
) -> list[int]:
    """Tile ids of every tile touching ``bounds`` (WebMercator metres) at each zoom.

    The ids are sorted ascending: by zoom, then along the Hilbert curve. This
    is the order a clustered archive is written in.
    """
    left, bottom, right, top = bounds
    ids: list[int] = []
    for zoom in zooms:
        span = MERCATOR_SPAN_M / 2**zoom
        last = 2**zoom - 1
        # Treat an edge within a millionth of a tile of a boundary as on it.
        # The scope extents lie on z13 tile edges, and the division at coarser
        # zooms is off by a rounding error.
        x0 = min(max(math.floor((left - MERCATOR_ORIGIN) / span + EDGE_EPS), 0), last)
        x1 = min(max(math.ceil((right - MERCATOR_ORIGIN) / span - EDGE_EPS) - 1, x0), last)
        y0 = min(max(math.floor((-MERCATOR_ORIGIN - top) / span + EDGE_EPS), 0), last)
        y1 = min(max(math.ceil((-MERCATOR_ORIGIN - bottom) / span - EDGE_EPS) - 1, y0), last)
        ids.extend(zxy_to_tileid(zoom, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))
    return sorted(ids)


_datasets: dict[tuple[str, int | None], rasterio.DatasetReader] = {}
"""Open sources of this worker process, keyed by path and overview level.

They stay open across tiles, so each worker reads a file's header once.
"""


def _open(path: str, overview_level: int | None) -> rasterio.DatasetReader:
    """Open ``path`` at one overview level, once per worker process."""
    key = (path, overview_level)
    if key not in _datasets:
        options = {} if overview_level is None else {"OVERVIEW_LEVEL": overview_level}
        _datasets[key] = rasterio.open(path, **options)
    return _datasets[key]


def _overview_level(overviews: list[int], zoom: int, max_zoom: int) -> int | None:
    """Overview to read a tile at ``zoom`` from, or ``None`` for full resolution.

    Picks the coarsest overview that still oversamples the tile by
    :data:`OVERSAMPLING_FACTOR`.
    """
    if not overviews or zoom >= max_zoom:
        return None
    target = 2 ** (max_zoom - zoom) / OVERSAMPLING_FACTOR
    candidates = [
        (abs(factor - target), i) for i, factor in enumerate(overviews) if factor <= target
    ]
    return min(candidates)[1] if candidates else None


def _footprint_is_empty(
    src: rasterio.DatasetReader, bounds: tuple[float, float, float, float]
) -> bool:
    """Whether no source pixel under a tile's footprint carries any alpha.

    The footprint is rounded outward to whole source pixels. On the aligned
    display grid it is exact, so tiles just outside the data count as empty.
    """
    west, south, east, north = transform_bounds(WEB_MERCATOR, src.crs, *bounds)
    window = window_from_bounds(west, south, east, north, transform=src.transform)
    # Round outward, but treat an edge within rounding error of a pixel edge
    # as on it, so the window stays inside the tile's own pixels.
    col0, row0 = math.floor(window.col_off + EDGE_EPS), math.floor(window.row_off + EDGE_EPS)
    col1 = math.ceil(window.col_off + window.width - EDGE_EPS)
    row1 = math.ceil(window.row_off + window.height - EDGE_EPS)
    footprint = Window(col0, row0, col1 - col0, row1 - row0)
    full = Window(0, 0, src.width, src.height)
    if not intersect(footprint, full):
        return True
    # Read band 4 directly, as the warp does: it always treats band 4 as
    # alpha, whereas `read_masks` uses it only when the file declares it so.
    return not src.read(4, window=footprint.intersection(full)).any()


def render_tile(path: str, tile_id: int, *, spec: TileSpec) -> tuple[int, bytes | None]:
    """Warp one tile out of ``path`` and encode it; ``None`` if the tile is empty.

    Runs in a worker process. The source under the tile's footprint is first
    checked for valid pixels, then reprojected onto the tile's EPSG:3857 grid
    with ``spec.resampling``.

    The bands hold value codes, with code 0 for nodata. Wherever the warped
    alpha is 0 the bands are set to 0, and alpha is set to either 0 or 255,
    because browsers premultiply alpha when decoding and would corrupt codes
    under partial alpha. WebP stores the alpha band losslessly, so nodata
    pixels stay nodata despite compression artefacts at a data edge.
    """
    zoom, x, y = tileid_to_zxy(tile_id)
    level = _overview_level(_open(path, None).overviews(1), zoom, spec.max_zoom)
    src = _open(path, level)
    left, bottom, right, top = tile_bounds(zoom, x, y)

    if spec.exclude_empty and _footprint_is_empty(src, (left, bottom, right, top)):
        return tile_id, None

    # The alpha band marks the data, so the tile declares no nodata value and
    # the warper keeps every code as it is.
    profile: dict[str, Any] = {
        "driver": spec.image_format,
        "dtype": "uint8",
        "width": spec.tile_size,
        "height": spec.tile_size,
        "count": 4,
        "crs": WEB_MERCATOR,
        "transform": transform_from_bounds(
            left, bottom, right, top, spec.tile_size, spec.tile_size
        ),
    }
    if spec.image_format in LOSSY_FORMATS:
        profile["QUALITY"] = spec.quality
    bands = np.zeros((4, spec.tile_size, spec.tile_size), dtype="uint8")
    reproject(
        rasterio.band(src, [1, 2, 3, 4]),
        bands,
        src_transform=src.transform,
        src_crs=src.crs,
        dst_transform=profile["transform"],
        dst_crs=WEB_MERCATOR,
        src_alpha=4,
        dst_alpha=4,
        num_threads=1,
        resampling=spec.resampling,
    )
    valid = bands[3] > 0
    bands[:3, ~valid] = 0
    bands[3] = np.where(valid, 255, 0)
    with MemoryFile() as memfile:
        with memfile.open(**profile) as tile:
            tile.write(bands)
        return tile_id, memfile.read()


WORKER_GDAL_CACHE_MB = 256
"""GDAL block cache of one tiler worker, in megabytes.

Set in each worker at start-up. A worker reads 256 px tiles from one local
raster, so this keeps it well under a gigabyte.
"""


def _close_datasets() -> None:
    """Close the worker's open sources before the interpreter tears GDAL down."""
    for dataset in _datasets.values():
        dataset.close()
    _datasets.clear()


def _configure_worker(cache_mb: int) -> None:
    """Bound GDAL's cache before the worker touches a raster.

    Runs in the spawned process before the first GDAL call, which reads the
    environment variable. An exit handler closes the worker's open sources
    while GDAL is still loaded.
    """
    os.environ.setdefault("GDAL_CACHEMAX", str(cache_mb))
    atexit.register(_close_datasets)


def worker_gdal_cache(_: int = 0) -> str:
    """Report a worker's GDAL cache setting; tests map it over a pool."""
    return os.environ.get("GDAL_CACHEMAX", "unset")


def spawn_pool(
    workers: int | None = None,
    *,
    cache_mb: int = WORKER_GDAL_CACHE_MB,
) -> ProcessPoolExecutor:
    """Start a worker pool for :func:`write_archive`, one process per core by default.

    Workers are spawned, because the caller may run other layers' warps in
    threads and forking a multi-threaded process is unsafe. Each worker's GDAL
    cache is bounded to ``cache_mb``.
    """
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_configure_worker,
        initargs=(cache_mb,),
    )


def write_archive(
    src: Path,
    out_path: Path,
    *,
    min_zoom: int,
    max_zoom: int,
    spec: TileSpec,
    name: str,
    attribution: str = "",
    description: str = "",
    extra_metadata: dict[str, Any] | None = None,
    pool: ProcessPoolExecutor | None = None,
) -> int:
    """Tile an RGBA raster into a PMTiles archive.

    Parameters
    ----------
    src
        Local four-band uint8 raster; band 4 is the alpha the empty-tile rule
        and the warp both read.
    out_path
        Archive written. Replaced if it exists.
    min_zoom, max_zoom
        Pyramid levels written, inclusive.
    spec
        Tile rendering.
    name, attribution, description
        Archive metadata, as a viewer displays it.
    extra_metadata
        Further keys of the archive's metadata, such as its value encoding.
    pool
        Worker pool the tiles are rendered in. Defaults to a fresh
        :func:`spawn_pool` for the duration of the call; pass one to share it
        across archives.

    Returns
    -------
    int
        Number of tiles written.

    Raises
    ------
    ValueError
        If the zoom range is inverted, the format unknown, or no tile of the
        range holds any data.
    """
    if min_zoom > max_zoom:
        msg = f"min_zoom {min_zoom} exceeds max_zoom {max_zoom}"
        raise ValueError(msg)
    if spec.image_format not in TILE_TYPES:
        msg = f"Unknown tile format {spec.image_format!r}; one of {sorted(TILE_TYPES)}"
        raise ValueError(msg)

    with rasterio.open(src) as dataset:
        mercator = transform_bounds(dataset.crs, WEB_MERCATOR, *dataset.bounds)
        west, south, east, north = transform_bounds(dataset.crs, "EPSG:4326", *dataset.bounds)
    tiles = tiles_covering(mercator, range(min_zoom, max_zoom + 1))
    logger.info(
        "Rendering %d candidate tiles of %s at z%d-%d", len(tiles), name, min_zoom, max_zoom
    )

    render = functools.partial(render_tile, str(src), spec=spec)
    out_path.unlink(missing_ok=True)
    written = 0
    with out_path.open("wb") as handle, nullcontext(pool) if pool else spawn_pool() as executor:
        writer = Writer(handle)
        # Results arrive in submission order, so the archive is written
        # clustered. Each task renders one tile, because tile costs vary widely
        # (from a mask read to a full warp) and single-tile tasks balance best.
        for tile_id, payload in executor.map(render, tiles):
            if payload is None:
                continue
            writer.write_tile(tile_id, payload)
            written += 1
        if not written:
            msg = f"{name}: no tile between z{min_zoom} and z{max_zoom} holds any data"
            raise ValueError(msg)
        # The bounds a viewer reads, clamped to the projection's limits.
        eps = 1e-10
        west, east = max(-180 + eps, west), min(180 - eps, east)
        south, north = max(-85.051129, south), min(85.051129, north)
        header: dict[str, Any] = {
            "version": 3,
            "tile_type": TILE_TYPES[spec.image_format],
            "tile_compression": Compression.NONE,
            "min_lon_e7": int(west * 1e7),
            "min_lat_e7": int(south * 1e7),
            "max_lon_e7": int(east * 1e7),
            "max_lat_e7": int(north * 1e7),
            "center_zoom": min_zoom,
            "center_lon_e7": int((west + east) / 2 * 1e7),
            "center_lat_e7": int((south + north) / 2 * 1e7),
        }
        metadata = {
            "name": name,
            "type": "overlay",
            "description": description or name,
            "writer": WRITER,
            "attribution": attribution or None,
            "tileSize": spec.tile_size,
            **(extra_metadata or {}),
        }
        writer.finalize(header, metadata)
    return written


__all__ = [
    "EDGE_EPS",
    "LOSSY_FORMATS",
    "OVERSAMPLING_FACTOR",
    "TILE_TYPES",
    "WRITER",
    "TileSpec",
    "render_tile",
    "spawn_pool",
    "tile_bounds",
    "tiles_covering",
    "worker_gdal_cache",
    "write_archive",
]
