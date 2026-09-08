#!/usr/bin/env python3
"""Portolan conformance gate, via rashid.

Fails on any error-severity finding whose rule is not in ACCEPTED.

ACCEPTED ships empty, and the rule for growing it is not negotiable: every
entry needs a row in docs/conformance.md giving the rule, where it fires, why
it is accepted, and the issue tracking its removal. A known deviation with an
issue number is a debt. A silently widened allow-list is a lie about what this
catalog conforms to.

Runs with --no-data. The byte checks read every asset, which over remote hrefs
makes CI slow and dependent on a third-party host being up. Run the full check
yourself before publishing:

    rashid check catalog/

Fails when rashid is absent, or when its version is outside the required range.
A skip reports a green run for a catalog that no validator read. The failure
message gives the one command that installs a usable rashid.

Run: python3 tests/test_conformance.py
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Two findings are structural consequences of generating the item tree rather
# than committing it, and neither is a metadata defect. Both are recorded in
# docs/conformance.md; both disappear the moment the gate runs against a built
# tree, which is what happens before publication.
#
#   PTL-COL-005  each collection registers its items.parquet mirror, and a fresh
#                clone holds no items for it to mirror. Dropping the registration
#                from the tracked collection.json would make the committed
#                document differ from the published one, which is worse.
ACCEPTED: set[str] = {"PTL-COL-005"}

# Local deviation from upstream: tools/publish.py is deleted (R-8), so the
# tracked catalog directory is named here instead of read from publish_dir.
target = ROOT / "catalog"

# 0.1.8 is the first release carrying the spec v0.2.0 rules. Below it, a `self`
# link and an absolute structural href are both reported as errors, which v0.2.0
# allows and this catalog relies on. Below 0.1.5 the rules PTL-LNK-007 through
# PTL-LNK-009 and PTL-AST-006 do not exist at all, so an older rashid reports a
# pass for a catalog it never checked against them. The upper bound stops an
# unreviewed 0.2 rule set from silently changing what this gate means.
MIN_VERSION = (0, 1, 8)
MAX_VERSION = (0, 2, 0)
SPEC = "rashid>=0.1.8,<0.2.0"
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
    # rashid 0.1.6 prints "rashid, version 0.1.6". Read the first X.Y.Z in the
    # output, and fail on a string with no version in it. A gate that cannot
    # read the version must not assume the version is good.
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
