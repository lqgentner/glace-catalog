"""Write what spans the built years, with :mod:`glace_catalog.finalize_catalog`.

Usage:
    make finalize
    uv run --env-file .env scripts/finalize-catalog.py
"""

from glace_catalog.finalize_catalog import main

if __name__ == "__main__":
    main()
