#!/usr/bin/env python3
"""Gate on the canonical mosaic grids in ``glace_catalog.scopes``.

Every ``Scope.grid`` is a **hand-committed constant**, so nothing at build time
recomputes it and nothing at build time would notice if it drifted. The extent
is also load-bearing three times over: the VRT chain warps onto it, the mosaic
COG is written on it, and ``stac/items.py`` copies it into ``proj:shape`` and
``proj:transform``. A digit lost in a hand edit would move all three together
and stay invisible until a client tried to stack two years.

What is checked, per scope:

- the CRS is WebMercator and the resolution is the WebMercatorQuad resolution at
  :data:`~glace_catalog.scopes.MOSAIC_ZOOM`;
- the extent is already on the z13 tile grid, i.e. ``snap_to_tiles`` is the
  identity on it, and the shape is a whole number of 256 px tiles;
- the origin is additionally aligned at z12, so the first internal overview is
  still on the tile grid.

That last one is the property GDAL's COG driver spells ``ALIGNED_LEVELS=2``.
Checked against GDAL 3.12.2: feeding a deliberately misaligned source through
``-of COG -co TILING_SCHEME=GoogleMapsCompatible -co ZOOM_LEVEL=13`` reproduces
``snap_to_tiles``'s output exactly, down to the last digit of the ``ch`` left
bound (753363.350778698921204). So this gate holds the constants to the same
rule GDAL applies, without a GDAL dependency or a byte of raster.

Alignment below z12 is *not* required, and deliberately: the extent would have
to grow to whole tiles at the coarsest aligned zoom, which for the Alps at z4
means padding 69120x41984 out to 131072x131072 -- 5.9x the pixels, because one
z4 tile is wider than the whole mosaic.

Reads no data and needs no network.

Run: python3 tests/test_grids.py
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

The constants and ``snap_to_tiles`` reach the same number by the same
arithmetic, so they agree bit for bit today. A tolerance twelve orders of
magnitude below a pixel keeps the gate about the grid rather than about the last
bit of a float, while still catching any edit a human could make.
"""

MIN_ALIGNED_LEVELS = 2
"""Levels that must land on the tile grid: the mosaic itself, and z12 under it."""


def aligned_levels(grid: Grid) -> int:
    """Count resolution levels whose tile grid the grid's origin sits on.

    Counts the way GDAL's ``ALIGNED_LEVELS`` does, with the full-resolution
    level as the first: the return value is 1 when only the mosaic itself is
    aligned, 2 when its first overview is too, and so on. Only the origin is
    examined, since that is what decides whether a decimated level's blocks
    coincide with the tiles of the scheme.

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
    # --- the analysis grid: equal-area, and that is the whole requirement ---
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
    # No tile-alignment requirement here, deliberately: nothing tiles this grid.
    # M-25 keeps the web map on its own WebMercator grid rather than warping out
    # of this one, so an equal-area extent owes the XYZ pyramid nothing.
    print(
        f"OK: {key} analysis {analysis.width}x{analysis.height} px at "
        f"{ANALYSIS_RESOLUTION:g} m, {LAEA}",
    )

    # --- the display grid: has to be a tile pyramid ---
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
