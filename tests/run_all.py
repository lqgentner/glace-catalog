#!/usr/bin/env python3
"""Run every gate. Exit non-zero if any fails.

The list is explicit rather than globbed so that the gates are visible in one
place and adding one is a deliberate edit. Run from anywhere:

    python3 tests/run_all.py
"""

from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent

TESTS = [
    "test_lint.py",
    "test_data_format.py",
    "test_version.py",
    "test_grids.py",
    "test_styles.py",
    "test_manifest_contract.py",
    "test_items.py",
    "test_mosaic_stats.py",
    "test_mirror.py",
    "test_thumbnails.py",
    "test_tiler.py",
    "test_webmap.py",
    "test_links.py",
    "test_sync_data.py",
    "test_stac_valid.py",
    "test_conformance.py",
]

failed = []
for name in TESTS:
    path = HERE / name
    if not path.exists():
        continue
    print(f"\n=== {name} " + "=" * (60 - len(name)), flush=True)
    if subprocess.run([sys.executable, str(path)], check=False).returncode != 0:
        failed.append(name)

print()
if failed:
    print("FAILED: " + ", ".join(failed))
    raise SystemExit(1)
print("all gates passed")
