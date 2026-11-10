#!/usr/bin/env python3
"""Check the repository's prose with Vale and the Portolan prose rules.

The rules are those of ``portolan-sdi/portolan-ops`` at the tag ``v1``: the
Portolan terms, mechanics and voice layers, and the Google, Microsoft,
Readability and ai-tells packages. ai-tells reports the patterns of machine
writing: hype words, figurative language, filler and formulaic structure.

Two kinds of prose are checked:

- the Markdown documents: the root README.md and AGENTS.md, ``docs/``, the
  READMEs and agent guides in ``catalog/``, and the last year's README and
  agent guide of the tiles year sub-catalog, which the build renders into the
  store;
- every ``title`` and ``description`` in the STAC documents of ``catalog/``,
  and the descriptions the build writes into the year catalogs, mosaic items
  and year styles, rendered from ``metadata/``. Vale reads Markdown only, so
  each document's strings are written to a Markdown file under
  ``.cache/prose/stac/``, and each finding is reported against its document
  and field.

Everything in ``catalog/`` is rendered: fix a finding in ``metadata/``, then
run ``make render``. A finding that is checked and kept goes into
:data:`ACCEPTED` with its reason. The check fails on an error, or on an
accepted entry that no longer matches.

Vale and the rules download into ``.cache/prose/`` on the first run. The rules
are fetched again on every run, because ``v1`` moves.

    make prose                         # warnings and errors
    make prose ARGS="--only ai-tells"  # one rule family
    make prose ARGS="--level suggestion"
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
from typing import Any
import urllib.request

import yaml

from glace_catalog import documents, metadata

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "prose"

VALE_VERSION = "3.19.0"
VALE_SHA256 = "c8f9d6c8055442bc7e9c121b2498e6f0e3fb670f4665e6ee577f1897f7665cf6"
VALE_URL = (
    f"https://github.com/vale-cli/vale/releases/download/v{VALE_VERSION}/"
    f"vale_{VALE_VERSION}_Linux_64-bit.tar.gz"
)
OPS_URL = "https://github.com/portolan-sdi/portolan-ops.git"
OPS_REF = "v1"

LEVELS = ("suggestion", "warning", "error")

STAC_FIELDS = ("title", "description")
"""Keys whose string values are checked as prose, at any depth of a document."""

_LIST = "A plain list of separate items, not a rhetorical triple."
_JOINED = "The 'and' joins two words inside one list item, not the last item."
_COVERAGE = "The 2015 coverage is checked against every year's source manifest."

ACCEPTED: dict[tuple[str, str], str] = {
    (
        "ai-tells.VerbTricolon",
        (
            ": to add a missing one, rerun the upstream year, which skips complete tiles "
            "and describes and hashes their files"
        ),
    ): "One instruction, then what the rerun does.",
    (
        "ai-tells.VerbTricolon",
        "building a data cube, opening tiles in QGIS, and working with mosaics",
    ): "The titles of three guide sections.",
    (
        "ai-tells.VerbTricolon",
        ": CRS of the item's rasters, which is the square's UTM zone, such as **********",
    ): "A definition and one example.",
    (
        "ai-tells.VerbTricolon",
        (
            ": a ****** asset, a ************** link, and the style ****************, whose "
            "stepped fill gives the Portolan browser a legend"
        ),
    ): "The three registrations PORTO-FMT-011 asks for.",
    (
        "ai-tells.VerbTricolon",
        (
            ": the PMTiles header, the item, and the style's ******** contain every field a "
            "client needs"
        ),
    ): _LIST,
    (
        "ai-tells.VerbTricolon",
        ": year links, temporal and spatial extent, the latest year's style",
    ): _LIST,
    (
        "ai-tells.VerbTricolon",
        (
            "must be in the canonical form the generator writes: ********* indent, "
            "declaration order, one trailing newline"
        ),
    ): _LIST,
    ("ai-tells.CataphoricForecasting", "The six decibel"): (
        "The count separates the decibel layers from the other six."
    ),
    ("ai-tells.NounString", "20 m mosaic pixel averages"): (
        "'20 m mosaic pixel' is the subject; 'averages' is its verb."
    ),
    ("ai-tells.UniversalSubject", "Every other year covers all"): _COVERAGE,
    ("ai-tells.UniversalObject", "covers all squares"): _COVERAGE,
    ("ai-tells.StrawmanContrast", ", not one per"): (
        "A design record: per-year collections are the common alternative."
    ),
    ("ai-tells.NegatedSubject", "Neither collection declares"): (
        "States that the projection extension is absent, and why."
    ),
    ("ai-tells.BareNames", "the band names their"): "'band names' is a noun, not a verb.",
    ("Portolan-Mechanics.Oxford", "outlines, plain and "): _JOINED,
    ("Portolan-Mechanics.Oxford", "RAM, source and "): _JOINED,
    ("Portolan-Mechanics.Oxford", "them, 32TNT14 and "): _JOINED,
    ("Portolan-Mechanics.Oxford", "archives, backscatter and "): _JOINED,
    ("Portolan-Mechanics.Oxford", "diff, rashid and "): _JOINED,
    ("Portolan-Mechanics.Oxford", "links, temporal and "): _JOINED,
    ("Portolan-Terms.Casing", "pmtiles"): "The archive suffix, inside a code span.",
    ("Microsoft.HeadingAcronyms", "LAEA"): "The grid's name, ETRS89-LAEA.",
    ("Microsoft.HeadingAcronyms", "MGRS"): "The grid's name.",
    ("Microsoft.HeadingAcronyms", "QGIS"): "The application's name.",
    ("Google.Spelling", "colour"): "The product name 'Scientific colour maps'.",
    ("Microsoft.Percentages", "valid percent"): "The name of a manifest column.",
}
"""Findings checked by hand and kept, by rule and matched text, with the reason.

The matched text has its whitespace collapsed, so rewrapping a paragraph keeps
an entry. An entry applies wherever its text appears.
"""

ACCEPTED_PATTERNS: dict[tuple[str, str], str] = {
    ("Portolan-Mechanics.Headings", r"GLACE .*"): (
        "GLACE is an acronym; the rest of the heading is in sentence case."
    ),
    ("Microsoft.Negative", r"-\d+(\.\d+)?"): (
        "A value a reader copies into code keeps the ASCII hyphen-minus."
    ),
}
"""Kept findings whose matched text varies, by rule and a full-match regex."""


def accepted(finding: Finding) -> tuple[str, str] | None:
    """Return the ACCEPTED or ACCEPTED_PATTERNS key that keeps ``finding``, if any."""
    key = (finding.rule, finding.match)
    if key in ACCEPTED:
        return key
    for rule, pattern in ACCEPTED_PATTERNS:
        if rule == finding.rule and re.fullmatch(pattern, finding.match):
            return (rule, pattern)
    return None


@dataclass(frozen=True)
class Finding:
    """One Vale alert, located in the file a person edits."""

    location: str
    level: str
    rule: str
    message: str
    match: str


def ensure_vale() -> Path:
    """Return the pinned Vale binary, downloading and verifying it on first use.

    Raises
    ------
    SystemExit
        If the download does not match the pinned checksum.
    """
    vale = CACHE / f"vale-{VALE_VERSION}"
    if vale.is_file():
        return vale
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / "vale.tar.gz"
    with urllib.request.urlopen(VALE_URL, timeout=60) as response:
        archive.write_bytes(response.read())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != VALE_SHA256:
        archive.unlink()
        msg = f"Vale {VALE_VERSION} has checksum {digest}, expected {VALE_SHA256}"
        raise SystemExit(msg)
    with tarfile.open(archive) as tar:
        member = tar.getmember("vale")
        source = tar.extractfile(member)
        if source is None:
            msg = f"the Vale {VALE_VERSION} archive holds no vale binary"
            raise SystemExit(msg)
        vale.write_bytes(source.read())
    vale.chmod(0o755)
    archive.unlink()
    return vale


def ensure_rules(vale: Path, attempts: int = 3) -> Path:
    """Fetch the ops rules at ``OPS_REF`` and their style packages; return the config.

    GitHub sometimes answers a release download with an error, so the package
    sync is retried.
    """
    ops = CACHE / "portolan-ops"
    shutil.rmtree(ops, ignore_errors=True)
    clone = ["git", "-c", "advice.detachedHead=false", "clone", "-q", "--depth", "1"]
    # Fixed argv lists, never a shell.
    subprocess.run([*clone, "--branch", OPS_REF, OPS_URL, str(ops)], check=True)  # noqa: S603
    config = ops / ".vale.ini"
    for attempt in range(1, attempts + 1):
        result = subprocess.run(  # noqa: S603
            [str(vale), "--config", str(config), "sync"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return config
        if attempt == attempts:
            sys.stderr.write(result.stdout + result.stderr)
            msg = f"vale sync failed after {attempts} attempts"
            raise SystemExit(msg)
        time.sleep(5 * attempt)
    return config


def markdown_documents() -> list[Path]:
    """List the Markdown documents to check, relative to the repository."""
    found = [ROOT / "README.md", ROOT / "AGENTS.md"]
    found += sorted((ROOT / "docs").rglob("*.md"))
    found += sorted((ROOT / "catalog").rglob("*.md"))
    return [path.relative_to(ROOT) for path in found if path.is_file()]


def prose_fields(document: Any, path: str = "") -> list[tuple[str, str]]:
    """Return ``(field path, text)`` for each title and description in a STAC document."""
    found: list[tuple[str, str]] = []
    if isinstance(document, dict):
        for key, value in document.items():
            here = f"{path}.{key}" if path else str(key)
            if key in STAC_FIELDS and isinstance(value, str):
                found.append((here, value))
            else:
                found.extend(prose_fields(value, here))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            found.extend(prose_fields(value, f"{path}[{index}]"))
    return found


def extract_stac(target: Path) -> dict[Path, list[tuple[int, int, str]]]:
    """Write the prose of each STAC document and of CITATION.cff to Markdown under ``target``.

    Each field becomes one paragraph. Returns, per written file, the first and
    last line of each paragraph and the ``document: field`` it came from.
    """
    shutil.rmtree(target, ignore_errors=True)
    documents: dict[str, list[tuple[str, str]]] = {}
    for source in sorted((ROOT / "catalog").rglob("*.json")):
        if "styles" not in source.parts:
            documents[str(source.relative_to(ROOT))] = prose_fields(json.loads(source.read_text()))
    documents["build-time prose"] = build_time_prose()
    citation = yaml.safe_load((ROOT / "CITATION.cff").read_text())
    documents["CITATION.cff"] = [
        (field, str(citation[field])) for field in ("title", "abstract") if citation.get(field)
    ]
    spans: dict[Path, list[tuple[int, int, str]]] = {}
    for index, (name, fields) in enumerate(documents.items()):
        if not fields:
            continue
        out = target / f"{index:03d}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        located: list[tuple[int, int, str]] = []
        for field, text in fields:
            first = len(lines) + 1
            lines.extend(text.splitlines() or [""])
            located.append((first, len(lines), f"{name}: {field}"))
            lines.append("")
        out.write_text("\n".join(lines) + "\n")
        spans[out.relative_to(ROOT)] = located
    return spans


def build_time_prose() -> list[tuple[str, str]]:
    """Render the prose the build writes into documents that are not tracked.

    The year catalogs, the mosaic items and the year styles exist only in the
    store. Their descriptions are rendered here from ``metadata/`` for the last
    year, under the template's name.
    """
    vocabulary = metadata.vocabulary()
    year = vocabulary.catalog.last_year
    return [
        ("collections.tiles.year_description", vocabulary.year_description(year)),
        ("collections.mosaics.item_description", vocabulary.mosaic_item_description(year)),
        ("collections.mosaics.style_description (year)", vocabulary.style_description(year)),
        ("collections.mosaics.style_description", vocabulary.style_description()),
    ]


def run_vale(
    vale: Path, config: Path, files: list[Path], level: str
) -> dict[str, list[dict[str, Any]]]:
    """Run Vale over ``files`` and return its alerts per file."""
    result = subprocess.run(  # noqa: S603
        [
            str(vale),
            *("--config", str(config), "--minAlertLevel", level, "--output", "JSON"),
            *(str(path) for path in files),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if not result.stdout.strip():
        sys.stderr.write(result.stderr)
        msg = f"vale produced no report (exit {result.returncode})"
        raise SystemExit(msg)
    report: dict[str, list[dict[str, Any]]] = json.loads(result.stdout)
    return report


def render_year_documents(target: Path) -> dict[Path, str]:
    """Render the last year's README.md and AGENTS.md of the tiles year sub-catalog.

    The build writes them into the store only. Returns each written file,
    relative to the repository, with the template it came from.
    """
    vocabulary = metadata.vocabulary()
    year = vocabulary.catalog.last_year
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    written: dict[Path, str] = {}
    for name, text in zip(
        ("README.md", "AGENTS.md"), documents.render_year_prose(vocabulary, year), strict=True
    ):
        out = target / name
        out.write_text(text)
        written[out.relative_to(ROOT)] = f"metadata/templates/tiles/year/{name}.j2 ({year})"
    return written


def locate(
    file: str,
    alert: dict[str, Any],
    spans: dict[Path, list[tuple[int, int, str]]],
    rendered: dict[Path, str],
) -> str:
    """Name where a person fixes an alert: the Markdown line, or the STAC field."""
    line = int(alert["Line"])
    for first, last, field in spans.get(Path(file), []):
        if first <= line <= last:
            return field
    return f"{rendered.get(Path(file), file)}:{line}"


def sort_key(finding: Finding) -> tuple[str, str]:
    """Order findings by where they are fixed, then by rule."""
    return (finding.location, finding.rule)


def main() -> int:
    """Check the prose and print the findings; return 1 if any is an error."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--level",
        choices=LEVELS,
        default="warning",
        help="lowest alert level to report (default: warning)",
    )
    parser.add_argument(
        "--only",
        metavar="PREFIX",
        help="report only rules whose name starts with this, such as ai-tells",
    )
    args = parser.parse_args()

    vale = ensure_vale()
    config = ensure_rules(vale)
    spans = extract_stac(CACHE / "stac")
    rendered = render_year_documents(CACHE / "year")
    files = markdown_documents() + sorted(rendered) + sorted(spans)
    report = run_vale(vale, config, files, args.level)

    alerts = [
        Finding(
            location=locate(file, alert, spans, rendered),
            level=str(alert["Severity"]),
            rule=str(alert["Check"]),
            message=str(alert["Message"]),
            match=re.sub(r"\s+", " ", str(alert["Match"])),
        )
        for file, file_alerts in report.items()
        for alert in file_alerts
        if args.only is None or str(alert["Check"]).startswith(args.only)
    ]
    findings = [f for f in alerts if accepted(f) is None]
    used = {accepted(f) for f in alerts}
    stale = [
        key
        for key in [*ACCEPTED, *ACCEPTED_PATTERNS]
        if key not in used
        and LEVELS.index(args.level) <= LEVELS.index("warning")
        and (args.only is None or key[0].startswith(args.only))
    ]
    findings.sort(key=sort_key)
    for finding in findings:
        print(f"{finding.location}  {finding.level}  {finding.rule}: {finding.message}")
    for rule, match in stale:
        print(f"ACCEPTED no longer matches: {rule}: {match!r}; remove the entry")
    counts = {level: sum(f.level == level for f in findings) for level in LEVELS}
    print(
        f"\n{len(files)} file(s): "
        + ", ".join(f"{counts[level]} {level}(s)" for level in reversed(LEVELS))
        + f", {len(alerts) - len(findings)} accepted"
    )
    return 1 if counts["error"] or stale else 0


if __name__ == "__main__":
    sys.exit(main())
