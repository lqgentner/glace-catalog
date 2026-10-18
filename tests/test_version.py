#!/usr/bin/env python3
"""Version gate: one release string, in every place that has to carry it.

``DATASET_VERSION`` in ``src/glace_catalog/stac/items.py`` is the source of
truth. The ``version`` field of both tracked ``collection.json`` files and of
``CITATION.cff`` repeat it, because STAC clients and GitHub each read only
their own copy. This gate reads all four and, on a mismatch, lists each place
and its value. It also checks that the ``doi`` of ``CITATION.cff`` is the one
``metadata/catalog.yaml`` lists for this version.

The string must be ``MAJOR.MINOR`` with an optional patch, so that the release
tag ``v{version}`` is well formed.

Run: uv run tests/test_version.py
"""

import json
from pathlib import Path
import re
import subprocess

from glace_catalog.stac.items import DATASET_VERSION

try:
    import yaml
except ImportError:
    print("error  PyYAML is not installed; install it with: uv sync --all-groups")
    raise SystemExit(1) from None

ROOT = Path(__file__).resolve().parent.parent
RELEASE_PATTERN = re.compile(r"^\d+\.\d+(\.\d+)?$")
COLLECTIONS = ("catalog/mosaics/collection.json", "catalog/tiles/collection.json")


def tracked_collections() -> list[Path]:
    """Check that git tracks exactly the two collection documents, and return them.

    The list is fixed, so an ignore rule or a rename that hides a collection
    fails the gate.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "catalog"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print("error  git ls-files failed; run this from inside the repository")
        raise SystemExit(1)
    found = sorted(name for name in result.stdout.split("\0") if name.endswith("/collection.json"))
    if found != sorted(COLLECTIONS):
        print(f"error  expected git to track exactly {sorted(COLLECTIONS)}, found {found}")
        raise SystemExit(1)
    return [ROOT / name for name in found]


def read_citation(path: Path) -> dict[str, object]:
    """Read ``CITATION.cff`` as a mapping."""
    parsed = yaml.safe_load(path.read_text())
    if not isinstance(parsed, dict):
        print(f"error  {path.relative_to(ROOT)}: does not parse as a CFF mapping")
        raise SystemExit(1)
    return parsed


def listed_version_doi() -> object:
    """Read the DOI ``metadata/catalog.yaml`` lists for ``DATASET_VERSION``, or None."""
    catalog = yaml.safe_load((ROOT / "metadata" / "catalog.yaml").read_text())
    return (catalog.get("doi") or {}).get("versions", {}).get(DATASET_VERSION)


places: list[tuple[str, object]] = [
    ("src/glace_catalog/stac/items.py DATASET_VERSION", DATASET_VERSION),
]

collections = tracked_collections()
if not collections:
    print("error  no tracked collection.json found under catalog/")
    raise SystemExit(1)
for path in collections:
    document = json.loads(path.read_text())
    places.append((f"{path.relative_to(ROOT)} version", document.get("version")))

citation = ROOT / "CITATION.cff"
if not citation.exists():
    print("error  CITATION.cff is missing; the release metadata lives there")
    raise SystemExit(1)
cff = read_citation(citation)
places.append(("CITATION.cff version", cff.get("version")))

problems: list[str] = []

if cff.get("doi") != listed_version_doi():
    problems.append(
        f"CITATION.cff doi {cff.get('doi')!r} is not the DOI metadata/catalog.yaml lists "
        f"for version {DATASET_VERSION}, {listed_version_doi()!r}"
    )

# Compare reprs, so that a YAML list or mapping under `version:` (unhashable)
# fails with this gate's message.
if len({repr(value) for _, value in places}) != 1:
    problems.append("the release version differs between the places that carry it")

for name, value in places:
    if not isinstance(value, str) or not RELEASE_PATTERN.fullmatch(value):
        problems.append(f"{name} is not a MAJOR.MINOR[.PATCH] release string")

if problems:
    for problem in problems:
        print(f"error  {problem}")
    print("the places and their values:")
    for name, value in places:
        print(f"  {name} = {value!r}")
    print(
        "DATASET_VERSION is the source of truth; the collections and "
        "CITATION.cff follow it, and the tag is v{version}.",
    )
    print(f"FAILED: {len(problems)} problem(s)")
    raise SystemExit(1)

print(f"OK: version {DATASET_VERSION} in {len(places)} place(s)")
