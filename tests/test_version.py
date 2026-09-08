#!/usr/bin/env python3
"""Version gate: one release string, in every place that has to carry it.

A release is one version string (R-16). ``DATASET_VERSION`` in
``src/glace_catalog/stac/items.py`` is the source of truth; the STAC
``version`` field of both tracked ``collection.json`` and the ``version`` of
``CITATION.cff`` restate it, because a STAC client, a Zenodo record and the git
tag are each read by someone who never sees the other two. Restated values
drift, so this gate reads all four and fails naming each place and what it
holds.

The string itself must look like a release: ``MAJOR.MINOR`` with an optional
patch. That rules out the values a hand edit produces -- ``1``, ``v1.0``,
``1.0-rc1`` -- any of which would make the tag ``v{version}`` wrong.

Run: python3 tests/test_version.py
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


def tracked_collections() -> list[Path]:
    """Every ``collection.json`` that git tracks, so generated output is skipped."""
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
    return [
        ROOT / name
        for name in result.stdout.split("\0")
        if name and name.endswith("/collection.json")
    ]


def citation_version(path: Path) -> object:
    """Read the ``version`` value of ``CITATION.cff``, or None when it is absent."""
    parsed = yaml.safe_load(path.read_text())
    if not isinstance(parsed, dict):
        print(f"error  {path.relative_to(ROOT)}: does not parse as a CFF mapping")
        raise SystemExit(1)
    return parsed.get("version")


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
places.append(("CITATION.cff version", citation_version(citation)))

problems: list[str] = []

values = {value for _, value in places}
if len(values) != 1:
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
