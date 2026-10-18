#!/usr/bin/env python3
"""Format, lint and type gates for the Python in this repository.

Three checks, all reproducible from a clean checkout:

- ``ruff format --check``: code must already be formatted, so a diff shows
  only real changes.
- ``ruff check``: the rule set is in ``pyproject.toml`` and matches the one
  the sibling repositories use.
- ``pyrefly check`` with the ``strict`` preset, over the paths listed in
  ``[tool.pyrefly] project-includes``.

Each tool is looked up in the current Python environment first
(``python -m ruff``), then on PATH. A missing tool fails the gate, so a passing
run always means the code was checked.

Run: uv run tests/test_lint.py
"""

from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ("src", "scripts", "tools", "tests")


def resolve(tool: str) -> list[str] | None:
    """Resolve the command that runs ``tool``, preferring this interpreter's copy."""
    probe = subprocess.run(
        [sys.executable, "-m", tool, "--version"],
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return [sys.executable, "-m", tool]
    found = shutil.which(tool)
    return [found] if found else None


def run(name: str, tool: str, args: list[str], *, targets: bool = True) -> str | None:
    """Run one gate, returning a failure description or None when it passed."""
    command = resolve(tool)
    if command is None:
        return f"{tool} is not installed; install it with: uv sync --all-groups"
    # pyrefly takes its paths from `project-includes` and `project-excludes` in
    # pyproject.toml, which keep the vendored template code out of scope.
    existing = [t for t in TARGETS if (ROOT / t).exists()] if targets else []
    result = subprocess.run(
        [*command, *args, *existing],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        print(f"--- {name} ---\n{output}\n")
        return f"{name} failed"
    print(f"OK: {name}")
    return None


failures = [
    problem
    for problem in (
        run("ruff format", "ruff", ["format", "--check"]),
        run("ruff check", "ruff", ["check"]),
        run("pyrefly", "pyrefly", ["check"], targets=False),
    )
    if problem is not None
]

if failures:
    print("FAILED: " + ", ".join(failures))
    raise SystemExit(1)
