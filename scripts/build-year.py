"""Build one year of GLACE composites into the store, with :mod:`glace_catalog.build_year`.

Usage:
    make build YEAR=2023
    uv run --env-file .env scripts/build-year.py 2023
"""

from glace_catalog.build_year import main

if __name__ == "__main__":
    main()
