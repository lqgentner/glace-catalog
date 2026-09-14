#!/usr/bin/env python3
"""Format gates for the JSON and YAML this repository tracks.

Python has ``ruff format``; JSON and YAML have nothing equivalent that is worth
a dependency, so the checks are written directly against the parsers.

**JSON** — every tracked ``.json`` under ``catalog/`` must already be in the
canonical form the generator writes: two-space indent, keys in the order the
document declares them, one trailing newline, non-ASCII left as itself. Most of
these documents are written by ``scripts/`` and the rest are hand-edited beside
them; without a canonical form, a hand edit and a regeneration produce a diff
full of reindentation that hides the one line that actually changed.

**YAML** — every tracked ``.yaml`` / ``.yml`` must parse, and must not use the
constructs that make YAML surprising. ``yaml.safe_load`` rejects arbitrary
object construction; the tab check catches the single most common YAML error,
which reports as a parse failure far from its cause.

Neither gate reads asset bytes, so both run offline and in CI.

Run: uv run tests/test_data_format.py
"""

import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent.parent


def tracked(*patterns: str) -> list[Path]:
    """Files matching ``patterns`` that git tracks, so ignored output is skipped."""
    result = subprocess.run(
        ["git", "ls-files", "-z", *patterns],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print("error  git ls-files failed; run this from inside the repository")
        raise SystemExit(1)
    return [ROOT / name for name in result.stdout.split("\0") if name]


problems: list[str] = []

json_files = tracked("*.json")
for path in json_files:
    raw = path.read_text()
    rel = path.relative_to(ROOT)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        problems.append(f"{rel}: does not parse: {error}")
        continue
    canonical = json.dumps(parsed, indent=2, ensure_ascii=False) + "\n"
    if raw != canonical:
        problems.append(
            f"{rel}: not in canonical form; rewrite it with "
            "json.dumps(obj, indent=2, ensure_ascii=False) + a trailing newline",
        )

yaml_files = tracked("*.yaml", "*.yml")
try:
    import yaml
except ImportError:
    print("error  PyYAML is not installed; install it with: uv sync --all-groups")
    raise SystemExit(1) from None

for path in yaml_files:
    raw = path.read_text()
    rel = path.relative_to(ROOT)
    for number, line in enumerate(raw.splitlines(), start=1):
        if "\t" in line.split("#", 1)[0]:
            problems.append(f"{rel}:{number}: tab in indentation; YAML forbids it")
    try:
        yaml.safe_load(raw)
    except yaml.YAMLError as error:
        problems.append(f"{rel}: does not parse: {error}")

if problems:
    for problem in problems:
        print(f"error  {problem}")
    print(f"FAILED: {len(problems)} problem(s)")
    raise SystemExit(1)

print(f"OK: {len(json_files)} JSON and {len(yaml_files)} YAML file(s) well-formed")
