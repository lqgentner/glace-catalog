#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright the portolan-catalog-template authors
#
# Taken from portolan-sdi/portolan-catalog-template and modified. See
# THIRD_PARTY_NOTICES.md; the deviations are marked where they occur.
"""STAC validity, via stac-check.

Validates every STAC object in the catalog against the official STAC 1.1.0
schemas. stac-check asks whether the JSON is valid STAC; rashid asks whether
the catalog conforms to Portolan. A catalog can pass one and fail the other, so
both run.

How it runs:

- One file at a time, with recursive=False. This handles relative links in a
  directory tree and gives a clear error for each file.
- Best-practice notes are printed as warnings only. Some contradict Portolan by
  design: stac-check recommends a rel:'self' link, which Portolan forbids so
  that a static catalog can be mirrored or moved. rashid is the gate; this
  check is advisory.
- One known stac-check crash is exempted until it is fixed upstream. See below
  and docs/conformance.md.

## The exempted crash

stac-validator always validates with the JSON Schema 2020-12 dialect, whatever
`$schema` a schema declares. The Portolan profile schema is draft-07 and uses
the draft-07 list form of `items` in `valid_bbox`. Read as 2020-12, that list
reaches code that expects a single schema, which raises:

    'list' object has no attribute 'get'
    [Schema: https://schemas.portolan-sdi.org/portolan/vX.Y.Z/schema.json].
    Error in Extensions.

The schema is valid draft-07, and rashid validates it cleanly. Tracked upstream
at https://github.com/stac-utils/stac-check/issues/159.

The exemption covers exactly that message, and only when the failing schema is
a Portolan profile schema. Every other stac-check error fails the gate.

## When it expires

The gate also fails once the crash stops on every object that can trigger it.
That means stac-check is fixed, and the exemption should be removed.

Only collections and items can trigger it, because stac-validator applies
extension schemas to those types alone (validate.py,
`stac_upper == "ITEM" or stac_upper == "COLLECTION"`). Catalogs are checked
against the core schema only. Until a collection or item declares the profile,
the expiry check stays quiet.

CI installs the newest stac-check, so the first fixed release makes this gate
fail and prompts the cleanup.

Fails when stac-check is missing, so a passing run always means the objects
were checked. The failure message gives the install command.

Run: uv run tests/test_stac_valid.py
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INSTALL = "python -m pip install stac-check"


def fail(message: str) -> None:
    """Report the problem, name the fix, and exit non-zero."""
    print(f"error  {message}")
    print(f"       install stac-check with: {INSTALL}")
    raise SystemExit(1)


# stac-check is unpinned so that CI always takes the newest release: the
# exemption below expires as soon as a release fixes the crash.
try:
    from stac_check.lint import Linter
except ImportError:
    fail("stac-check is not installed, so this gate checks nothing")

# Local deviation from upstream: the catalog directory is fixed here.
BASE = ROOT / "catalog"

STAC_TYPES = {"Catalog", "Collection", "Feature"}

# stac-validator applies declared extension schemas to these types only.
EXTENSION_VALIDATED = {"Collection", "Feature"}

UPSTREAM_ISSUE = "https://github.com/stac-utils/stac-check/issues/159"

DIALECT_CRASH = "'list' object has no attribute 'get'"

# Matches any profile version, so the exemption survives a profile version bump.
PROFILE_SCHEMA = re.compile(
    r"https://schemas\.portolan-sdi\.org/portolan/v\d+\.\d+\.\d+/schema\.json"
)


def is_dialect_crash(message: str) -> bool:
    """The known stac-validator dialect crash on a Portolan profile schema."""
    return DIALECT_CRASH in message and bool(PROFILE_SCHEMA.search(message))


def declares_profile(doc: dict) -> bool:
    extensions = doc.get("stac_extensions")
    if not isinstance(extensions, list):
        return False
    return any(
        isinstance(uri, str) and PROFILE_SCHEMA.fullmatch(uri.rstrip("#"))
        for uri in extensions
    )


errors: list[str] = []
exempted: list[str] = []
reachable: list[str] = []
checked = 0

for path in sorted(BASE.rglob("*.json")):
    rel = path.relative_to(BASE)
    if any(part.startswith(".") for part in rel.parts):
        continue
    if path.name.endswith(".style.json") or "styles" in path.parts:
        continue
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        errors.append(f"{rel}: invalid JSON ({exc})")
        continue
    if not isinstance(doc, dict) or doc.get("type") not in STAC_TYPES:
        continue

    checked += 1
    if doc["type"] in EXTENSION_VALIDATED and declares_profile(doc):
        reachable.append(str(rel))

    linter = Linter(str(path), recursive=False)
    if not linter.valid_stac:
        if is_dialect_crash(linter.error_msg):
            exempted.append(str(rel))
            continue
        errors.append(f"{rel}: {linter.error_msg}")
        continue
    for note in linter.best_practices_msg[1:]:
        if note.strip():
            print(f"note   {rel}: {note.strip()}")

if exempted:
    for rel_str in exempted:
        print(f"exempt {rel_str}: known stac-check dialect crash")
    print(
        f"exempt {len(exempted)} object(s) carried the exemption for {UPSTREAM_ISSUE}"
    )

# Objects that can trigger the crash validated cleanly: stac-check is fixed.
expired = bool(reachable) and not exempted
if expired:
    print(
        f"error  stac-check no longer emits the Portolan dialect crash on "
        f"{len(reachable)} reachable object(s). Upstream is fixed. Delete the "
        f"exemption in tests/test_stac_valid.py and the section in "
        f"docs/conformance.md. Tracking: {UPSTREAM_ISSUE}"
    )

if errors:
    print("\n".join(f"error  {e}" for e in errors))

if errors or expired:
    raise SystemExit(1)
print(f"OK: {checked} STAC object(s) valid")
