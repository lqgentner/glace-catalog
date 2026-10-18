#!/usr/bin/env python3
"""Checks the mosaic grids defined in ``glace_catalog.scopes``.

Each scope's grids are constants written by hand, and the build uses them as
given. The display grid matters in three places: the VRT chain warps onto it,
the mosaic COG is written on it, and ``stac/items.py`` copies it into
``proj:shape`` and ``proj:transform``. A typo would shift all three together,
and would only show when someone compared two years.

Checked for each scope:

- the analysis grid uses the equal-area CRS at the analysis resolution, with
  its bounds on the analysis alignment;
- the display grid is WebMercator at the WebMercatorQuad resolution for
  :data:`~glace_catalog.scopes.MOSAIC_ZOOM`;
- its extent lies on the z13 tile grid (``snap_to_tiles`` leaves it unchanged)
  and its size is a whole number of 256 px tiles;
- its origin is also aligned at z12, so the first overview stays on the tile
  grid. GDAL's COG driver calls this ``ALIGNED_LEVELS=2``.

``snap_to_tiles`` matches GDAL's own snapping to the last digit (checked with
GDAL 3.12.2 and ``-of COG -co TILING_SCHEME=GoogleMapsCompatible -co
ZOOM_LEVEL=13``), so this gate applies GDAL's rule using plain arithmetic.

Two aligned levels keep the extent close to the data. Aligning coarser levels
means padding the extent to whole tiles at that zoom: for the Alps at z4, one
tile is wider than the whole mosaic, and the padding would multiply the pixel
count by 5.9.

Reads no data and needs no network.

Run: uv run tests/test_grids.py
"""

from glace_catalog.scopes import (
    ANALYSIS_ALIGNMENT,
    ANALYSIS_RESOLUTION,
    LAEA,
    MERCATOR_ORIGIN,
    MOSAIC_ZOOM,
    SCOPES,
    TILE_PX,
    WEB_MERCATOR,
    Grid,
    snap_to_tiles,
    zoom_resolution,
)

TOLERANCE_M = 1e-6
"""Slack allowed when comparing two extents, in metres.

Far below a pixel, so it ignores floating-point noise but catches any hand edit.
"""

MIN_ALIGNED_LEVELS = 2
"""Levels that must land on the tile grid: the mosaic itself, and z12 under it."""


def aligned_levels(grid: Grid) -> int:
    """Count resolution levels whose tile grid the grid's origin sits on.

    Counts the way GDAL's ``ALIGNED_LEVELS`` does, with the full-resolution
    level as the first: the return value is 1 when only the mosaic itself is
    aligned, 2 when its first overview is too, and so on. Only the origin is
    checked, because it decides whether each overview's blocks line up with the
    tiles of the scheme.

    Parameters
    ----------
    grid
        Grid to measure.

    Returns
    -------
    int
        Number of consecutive aligned levels, starting at ``MOSAIC_ZOOM``.
    """
    left, _, _, top = grid.bounds
    levels = 0
    for zoom in range(MOSAIC_ZOOM, -1, -1):
        step = zoom_resolution(zoom) * TILE_PX
        indices = ((left - MERCATOR_ORIGIN) / step, (top - MERCATOR_ORIGIN) / step)
        if any(abs(index - round(index)) * step > TOLERANCE_M for index in indices):
            break
        levels += 1
    return levels


problems: list[str] = []

for key, scope in sorted(SCOPES.items()):
    # --- the analysis grid: equal-area, on the analysis alignment ---
    analysis = scope.analysis_grid
    if analysis.crs != LAEA:
        problems.append(f"{key}: analysis crs is {analysis.crs}, not {LAEA}")
    if analysis.resolution != ANALYSIS_RESOLUTION:
        problems.append(
            f"{key}: analysis resolution is {analysis.resolution!r}, not {ANALYSIS_RESOLUTION!r}",
        )
    for name, value in zip(("left", "bottom", "right", "top"), analysis.bounds, strict=True):
        if (
            abs(value / ANALYSIS_ALIGNMENT - round(value / ANALYSIS_ALIGNMENT))
            * ANALYSIS_ALIGNMENT
            > TOLERANCE_M
        ):
            problems.append(
                f"{key}: analysis {name} bound {value!r} is not on the "
                f"{ANALYSIS_ALIGNMENT:g} m alignment",
            )
    # Equal-area alignment is all this grid needs: the web map is built on the
    # separate WebMercator display grid below.
    print(
        f"OK: {key} analysis {analysis.width}x{analysis.height} px at "
        f"{ANALYSIS_RESOLUTION:g} m, {LAEA}",
    )

    # --- the display grid: must form a tile pyramid ---
    grid = scope.display_grid

    if grid.crs != WEB_MERCATOR:
        problems.append(f"{key}: display crs is {grid.crs}, not {WEB_MERCATOR}")

    expected_resolution = zoom_resolution(MOSAIC_ZOOM)
    if grid.resolution != expected_resolution:
        problems.append(
            f"{key}: display resolution is {grid.resolution!r}, not the WebMercatorQuad "
            f"resolution at z{MOSAIC_ZOOM} ({expected_resolution!r})",
        )

    snapped = snap_to_tiles(grid.bounds, MOSAIC_ZOOM)
    drift = [abs(a - b) for a, b in zip(grid.bounds, snapped, strict=True)]
    if max(drift) > TOLERANCE_M:
        problems.append(
            f"{key}: bounds are not on the z{MOSAIC_ZOOM} tile grid; "
            f"snap_to_tiles moves them by up to {max(drift):.6g} m, to {snapped!r}",
        )

    for name, size in (("width", grid.width), ("height", grid.height)):
        if size % TILE_PX:
            problems.append(
                f"{key}: {name} is {size} px, not a whole number of "
                f"{TILE_PX} px tiles ({size / TILE_PX:.4f})",
            )

    levels = aligned_levels(grid)
    if levels < MIN_ALIGNED_LEVELS:
        problems.append(
            f"{key}: only {levels} level(s) land on the tile grid; "
            f"z{MOSAIC_ZOOM - levels} is the first overview off it, and "
            f"{MIN_ALIGNED_LEVELS} are required",
        )
    else:
        print(
            f"OK: {key} {grid.width}x{grid.height} px "
            f"({grid.width // TILE_PX}x{grid.height // TILE_PX} tiles) at "
            f"z{MOSAIC_ZOOM}, aligned down to z{MOSAIC_ZOOM - levels + 1}",
        )

if problems:
    for problem in problems:
        print(f"error  {problem}")
    print(f"FAILED: {len(problems)} problem(s)")
    raise SystemExit(1)

print(
    f"OK: {len(SCOPES)} scope(s), each with an equal-area analysis grid and a "
    f"WebMercatorQuad display grid"
)
