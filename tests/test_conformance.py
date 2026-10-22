#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright the portolan-catalog-template authors
#
# Taken from portolan-sdi/portolan-catalog-template and modified. See
# THIRD_PARTY_NOTICES.md; the deviations are marked where they occur.
"""Portolan conformance gate, via rashid.

Fails on any error-severity finding whose rule is not in ACCEPTED.

Every entry in ACCEPTED needs a row in docs/conformance.md giving the rule,
where it fires, why it is accepted, and the issue tracking its removal.

Runs with --no-data, which keeps CI fast and independent of remote hosts. Run
the full check, which reads every asset, yourself before publishing:

    rashid check catalog/

Also fails when rashid is missing or its version is outside the supported
range, so a passing run always means the catalog was checked. The failure
message says how to install a suitable rashid.

Run: uv run tests/test_conformance.py
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Findings caused by the tracked tree holding no built items. They are recorded
# in docs/conformance.md and disappear when the gate runs against a built tree
# before publication.
#
#   PTL-COL-005  each collection lists its items.parquet index, and a fresh
#                clone has no items to index. The tracked collection.json keeps
#                the entry so that it matches the published document.
#   PTL-AST-007  each collection's data assets are on its items, and a fresh
#                clone has none.
ACCEPTED: set[str] = {"PTL-COL-005", "PTL-AST-007"}

# Local deviation from upstream: the catalog directory is fixed here.
target = ROOT / "catalog"

# 0.3.0 validates every declared extension schema and requires each
# collection to refer to its primary data (PORTO-CORE-082). The upper bound
# means a new 0.4 rule set is reviewed before this gate adopts it.
MIN_VERSION = (0, 3, 0)
MAX_VERSION = (0, 4, 0)
SPEC = "rashid>=0.3.0,<0.4.0"
INSTALL = f"python -m pip install '{SPEC}'"


def fail(message: str) -> None:
    """Report the problem, name the fix, and exit non-zero."""
    print(f"error  {message}")
    print(f"       install a usable rashid with: {INSTALL}")
    raise SystemExit(1)


def rashid_version() -> tuple[int, ...]:
    """The version that rashid reports, as a tuple of integers."""
    try:
        proc = subprocess.run(
            ["rashid", "--version"], capture_output=True, text=True
        )
    except OSError as exc:
        fail(f"rashid --version did not run ({exc})")
    if proc.returncode != 0:
        fail(f"rashid --version exited {proc.returncode}")
    # rashid prints e.g. "rashid, version 0.1.6". Take the first X.Y.Z, and
    # fail when the output has none.
    text = (proc.stdout + proc.stderr).strip()
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if match is None:
        fail(f"rashid --version printed no readable version: {text!r}")
    return tuple(int(part) for part in match.groups())


if shutil.which("rashid") is None:
    fail("rashid is not installed, so this gate checks nothing")

version = rashid_version()
shown = ".".join(str(part) for part in version)
if not MIN_VERSION <= version < MAX_VERSION:
    fail(f"rashid {shown} is outside the required range {SPEC}")

result = subprocess.run(
    ["rashid", "check", str(target), "--no-data", "--json"],
    capture_output=True,
    text=True,
)

try:
    report = json.loads(result.stdout)
except json.JSONDecodeError:
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    raise SystemExit("rashid produced no JSON report")

findings = report.get("findings", [])
blocking = [
    f for f in findings
    if f.get("severity") == "error" and f.get("rule_id") not in ACCEPTED
]

for finding in blocking:
    where = finding.get("path", "?")
    print(f"error  {finding.get('rule_id')}  {where}: {finding.get('message')}")
    if finding.get("fix_hint"):
        print(f"       hint: {finding['fix_hint']}")

waived = [f for f in findings if f.get("rule_id") in ACCEPTED]
if waived:
    print(f"\n{len(waived)} accepted finding(s); see docs/conformance.md")

if blocking:
    raise SystemExit(1)
print(
    f"OK: rashid {shown} found no blocking errors in {target.name}/"
)
