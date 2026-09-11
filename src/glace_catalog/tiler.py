"""Raster PMTiles tiling, in this process.

Adapted from **rio-pmtiles 1.2.1** (``rio_pmtiles/worker.py`` and
``rio_pmtiles/scripts/cli.py``), Copyright (c) 2025 Protomaps LLC and 2014-2021
Mapbox, released under the MIT License; the full notice is in
``THIRD_PARTY_NOTICES.md`` at the repository root. rio-pmtiles is itself derived
from Sean Gillies's rio-mbtiles.

Why it lives here rather than behind the ``rio pmtiles`` command it came from:

- The per-tile worker upstream keeps the ``OVERVIEW_LEVEL`` it chose in a
  module-level dictionary and never clears it, so once a coarse zoom has picked
  an overview, every finer tile that follows in the same worker is read from
  that overview and upsampled — the max-zoom tiles of a source with overviews
  come out blurred. :func:`render_tile` chooses the level per call instead.
- The archive is written by :class:`pmtiles.writer.Writer`, the maintained
  writer of the PMTiles package, so the only code owned here is the warp of one
  tile and the enumeration of which tiles to warp.
- One :class:`~concurrent.futures.ProcessPoolExecutor` serves every layer of a
  year, instead of one subprocess per archive with its own worker pool, its own
  environment and a ``rio`` executable to find.

The tile bytes are the ones rio-pmtiles produces from the same source: the
reprojection call and the image creation options are kept as they were. Two
rules differ, both on purpose and both documented where they apply: the tile
image declares no nodata, and a tile is empty when its own footprint is.
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
"""PMTiles tile type per GDAL raster driver the tiles are encoded with.

Every tile carries alpha, which is why JPEG is not offered: GDAL writes a
four-band JPEG as CMYK, not RGBA.
"""

LOSSY_FORMATS = frozenset({"WEBP"})
"""The formats that take a ``QUALITY``; PNG rejects the option."""

EDGE_EPS = 1e-6
"""Tolerance for an edge to count as lying on a boundary, in tiles or in pixels."""

OVERSAMPLING_FACTOR = 4
"""Source pixels per output pixel a tile is warped from, at least.

An overview is only chosen when it still leaves this many pixels per tile
pixel for the resampling kernel to work on; from rio-pmtiles.
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
    quality: int = 80
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

    Ascending, which is the order a clustered archive wants them written in:
    by zoom, and along the Hilbert curve within one.
    """
    left, bottom, right, top = bounds
    ids: list[int] = []
    for zoom in zooms:
        span = MERCATOR_SPAN_M / 2**zoom
        last = 2**zoom - 1
        # An edge within a millionth of a tile of a boundary is on it: the
        # scope extents sit exactly on z13 tile edges, and at a coarser zoom
        # the division lands a rounding error either side of the integer.
        x0 = min(max(math.floor((left - MERCATOR_ORIGIN) / span + EDGE_EPS), 0), last)
        x1 = min(max(math.ceil((right - MERCATOR_ORIGIN) / span - EDGE_EPS) - 1, x0), last)
        y0 = min(max(math.floor((-MERCATOR_ORIGIN - top) / span + EDGE_EPS), 0), last)
        y1 = min(max(math.ceil((-MERCATOR_ORIGIN - bottom) / span - EDGE_EPS) - 1, y0), last)
        ids.extend(zxy_to_tileid(zoom, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))
    return sorted(ids)


_datasets: dict[tuple[str, int | None], rasterio.DatasetReader] = {}
"""Open sources of this worker process, keyed by path and overview level.

Kept open across tiles: every tile would otherwise reopen the file and re-read
its header, twice, as the upstream worker does.
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

    The coarsest overview that still oversamples the tile by
    :data:`OVERSAMPLING_FACTOR`; rio-pmtiles's rule, chosen here per tile.
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

    The footprint is rounded outward to whole source pixels. rio-pmtiles tests
    a window with a one-pixel margin instead, which keeps a ring of fully
    transparent tiles around the data; on the aligned display grid the
    footprint itself is exact.
    """
    west, south, east, north = transform_bounds(WEB_MERCATOR, src.crs, *bounds)
    window = window_from_bounds(west, south, east, north, transform=src.transform)
    # Outward, but not across a pixel edge the footprint merely sits on to
    # within rounding: that would read one column of the neighbouring tile.
    col0, row0 = math.floor(window.col_off + EDGE_EPS), math.floor(window.row_off + EDGE_EPS)
    col1 = math.ceil(window.col_off + window.width - EDGE_EPS)
    row1 = math.ceil(window.row_off + window.height - EDGE_EPS)
    footprint = Window(col0, row0, col1 - col0, row1 - row0)
    full = Window(0, 0, src.width, src.height)
    if not intersect(footprint, full):
        return True
    # Band 4 read as data rather than `read_masks`, which only consults it when
    # the file declares it as alpha; the warp reads it as alpha unconditionally,
    # and the two must agree on what is empty.
    return not src.read(4, window=footprint.intersection(full)).any()


def render_tile(path: str, tile_id: int, *, spec: TileSpec) -> tuple[int, bytes | None]:
    """Warp one tile out of ``path`` and encode it; ``None`` if the tile is empty.

    Runs in a worker process. The source under the tile's footprint is read
    first to decide whether it holds any valid pixel, then reprojected onto
    the tile's own 3857 grid with ``spec.resampling``.
    """
    zoom, x, y = tileid_to_zxy(tile_id)
    level = _overview_level(_open(path, None).overviews(1), zoom, spec.max_zoom)
    src = _open(path, level)
    left, bottom, right, top = tile_bounds(zoom, x, y)

    if spec.exclude_empty and _footprint_is_empty(src, (left, bottom, right, top)):
        return tile_id, None

    # No destination nodata: the alpha band says where the data is. rio-pmtiles
    # sets 255, which makes the warper move pure white to 254 to keep it apart
    # from the sentinel — a change the source never asked for.
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
    with MemoryFile() as memfile:
        with memfile.open(**profile) as tile:
            reproject(
                rasterio.band(src, [1, 2, 3]),
                rasterio.band(tile, [1, 2, 3]),
                src_alpha=4,
                dst_alpha=4,
                num_threads=1,
                resampling=spec.resampling,
            )
        return tile_id, memfile.read()


WORKER_GDAL_CACHE_MB = 256
"""GDAL block cache of one tiler worker, in megabytes.

GDAL sizes its cache at 5 % of physical memory *per process* unless told
otherwise, so fifteen spawned workers on a 60 GB machine claimed about 45 GB
between them and the 2022 Swiss build ran the machine to 57 GB (2026-09-11). A
worker reads 256 px tiles from one local raster; a few hundred megabytes of
cache is more than it can use.
"""


def _close_datasets() -> None:
    """Close the worker's open sources before the interpreter tears GDAL down."""
    for dataset in _datasets.values():
        dataset.close()
    _datasets.clear()


def _configure_worker(cache_mb: int) -> None:
    """Bound GDAL's cache before the worker touches a raster.

    Runs in the spawned process before any GDAL call, which is when the
    environment variable is read. The sources the worker keeps open are closed
    at exit rather than left to interpreter shutdown, where GDAL may already be
    gone when the reader objects are collected.
    """
    os.environ.setdefault("GDAL_CACHEMAX", str(cache_mb))
    atexit.register(_close_datasets)


def worker_gdal_cache(_: int = 0) -> str:
    """Report the GDAL cache a worker runs with; mapped over a pool by the gate."""
    return os.environ.get("GDAL_CACHEMAX", "unset")


def spawn_pool(
    workers: int | None = None,
    *,
    cache_mb: int = WORKER_GDAL_CACHE_MB,
) -> ProcessPoolExecutor:
    """Start a worker pool for :func:`write_archive`, one process per core by default.

    Spawned rather than forked: the caller may be running the warps of other
    layers in threads, and forking a multi-threaded process is unsafe. Each
    worker's GDAL cache is bounded to ``cache_mb``.
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
        # Results arrive in submission order, so the archive comes out
        # clustered. One tile per task: a tile's cost varies from a mask read
        # to a full warp, and batching them was measured 3.5x slower on a
        # Swiss layer (22.5 s at 32 per task against 6.5 s at one).
        for tile_id, payload in executor.map(render, tiles):
            if payload is None:
                continue
            writer.write_tile(tile_id, payload)
            written += 1
        if not written:
            msg = f"{name}: no tile between z{min_zoom} and z{max_zoom} holds any data"
            raise ValueError(msg)
        # The bounds a viewer reads. Clamped to the projection's own limits, as
        # rio-pmtiles does; the mosaics never reach them.
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
