#!/usr/bin/env python3
"""Format, lint and type gates for the Python in this repository.

Three checks, all of which must be reproducible from a clean checkout:

- ``ruff format --check`` — formatting is not a matter of taste here, it is a
  property the diff depends on. A reformatting commit that touches a file it
  did not mean to change costs a reviewer more than it saves a writer.
- ``ruff check`` — the lint rule set is in ``pyproject.toml`` and is the same
  one deep-glacier-mapping uses, so code moving between the two repositories
  does not change shape on the way.
- ``mypy`` over ``src/``, ``scripts/`` and ``tools/``.

Each tool is resolved from the active interpreter's environment first
(``python -m ruff``), then from PATH. A tool that is absent **fails** rather
than skips: a skipped gate reports a green run for code nothing read.

Run: python3 tests/test_lint.py
"""

from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ("src", "scripts", "tools", "tests")


def resolve(tool: str) -> list[str] | None:
    """The command that runs ``tool``, preferring this interpreter's copy."""
    probe = subprocess.run(
        [sys.executable, "-m", tool, "--version"],
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return [sys.executable, "-m", tool]
    found = shutil.which(tool)
    return [found] if found else None


def run(name: str, tool: str, args: list[str]) -> str | None:
    """Run one gate. Returns a failure description, or None when it passed."""
    command = resolve(tool)
    if command is None:
        return f"{tool} is not installed; install it with: uv sync --all-groups"
    existing = [t for t in TARGETS if (ROOT / t).exists()]
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
        run("mypy", "mypy", []),
    )
    if problem is not None
]

if failures:
    print("FAILED: " + ", ".join(failures))
    raise SystemExit(1)
