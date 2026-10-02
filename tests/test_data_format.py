#!/usr/bin/env python3
"""Format gates for the JSON and YAML this repository tracks.

These checks use the JSON and YAML parsers directly.

**JSON**: every tracked ``.json`` file must already be in the form the
generator writes: two-space indent, keys in their existing order, one trailing
newline, non-ASCII characters kept as they are. With one fixed form, a hand
edit and a regeneration produce the same bytes, and a diff shows only the lines
that really changed.

**YAML**: every tracked ``.yaml`` / ``.yml`` file must parse with
``yaml.safe_load``, which refuses to build arbitrary Python objects. Tabs in
indentation are reported by line, because YAML forbids them and the parser
reports them as an error somewhere else.

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
