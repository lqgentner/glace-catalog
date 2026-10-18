#!/usr/bin/env python3
"""Checks ``tests/test_links.py --remote`` against a local HTTP server.

The remote mode runs once per upload, against Source Cooperative. This gate
publishes a small store from a temporary directory over ``http.server`` and
runs the mode against it: complete, with an object missing, and with an
object of the wrong size. The fixture also links to a host that does not
resolve, which the mode must not request.

Writes only into a temporary directory and needs no network beyond loopback.

Run: uv run tests/test_links_remote.py
"""

from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, override

LINKS = Path(__file__).resolve().parent / "test_links.py"

ASSET = b"COG!!"
"""Bytes of the one item asset."""

INDEX = b"PAR1 index PAR1"
"""Bytes of the collection's item index, linked by an absolute href."""


class QuietHandler(SimpleHTTPRequestHandler):
    """Serves files without logging each request."""

    @override
    def log_message(self, format: str, *args: Any) -> None:
        """Drop the request log."""


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def _store(published: Path, base_url: str) -> None:
    """Write a catalog, a collection and one item with its asset."""
    _write_json(
        published / "catalog.json",
        {
            "type": "Catalog",
            "links": [
                {"rel": "self", "href": f"{base_url}/catalog.json"},
                {"rel": "describedby", "href": "./README.md"},
                {"rel": "child", "href": "./tiles/collection.json"},
                {"rel": "related", "href": "https://unresolvable.invalid/spec.pdf"},
            ],
        },
    )
    (published / "README.md").write_text("# Store\n")
    _write_json(
        published / "tiles" / "collection.json",
        {
            "type": "Collection",
            "links": [
                {"rel": "parent", "href": "../catalog.json"},
                {"rel": "item", "href": "./2020/T1/item.json"},
            ],
            "assets": {
                "items": {
                    "href": f"{base_url}/tiles/items.parquet",
                    "file:size": len(INDEX),
                },
            },
        },
    )
    (published / "tiles" / "items.parquet").write_bytes(INDEX)
    _write_json(
        published / "tiles" / "2020" / "T1" / "item.json",
        {
            "type": "Feature",
            "links": [{"rel": "collection", "href": "../../collection.json"}],
            "assets": {"data": {"href": "./data.tif", "file:size": len(ASSET)}},
        },
    )
    (published / "tiles" / "2020" / "T1" / "data.tif").write_bytes(ASSET)


def _mirror(published: Path, mirror: Path) -> None:
    """Copy the documents only, as tools/mirror_docs.py does."""
    for path in published.rglob("*"):
        if path.suffix in {".json", ".md"}:
            target = mirror / path.relative_to(published)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def _run(mirror: Path, base_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LINKS), "--remote", str(mirror), "--base-url", base_url],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def main() -> int:
    """Run every check; print the failures."""
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, directory=str(root / "served"))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}/glace"
        published = root / "served" / "glace"
        mirror = root / "mirror"
        try:
            _store(published, base_url)
            _mirror(published, mirror)

            complete = _run(mirror, base_url)
            checks["a complete store passes"] = complete.returncode == 0
            checks["every published object is requested, the foreign link is not"] = (
                "HEAD 6 URL(s)" in complete.stdout
            )

            (published / "tiles" / "2020" / "T1" / "data.tif").unlink()
            missing = _run(mirror, base_url)
            checks["a missing asset fails"] = missing.returncode == 1
            checks["the failure names the asset and its status"] = (
                f"{base_url}/tiles/2020/T1/data.tif: HTTP 404" in missing.stdout
            )
            (published / "tiles" / "2020" / "T1" / "data.tif").write_bytes(ASSET)

            (published / "tiles" / "items.parquet").write_bytes(INDEX[:4])
            truncated = _run(mirror, base_url)
            checks["a truncated absolute href fails"] = truncated.returncode == 1
            checks["the failure names both sizes"] = (
                f"items.parquet: 4 bytes, expected {len(INDEX)}" in truncated.stdout
            )
        finally:
            server.shutdown()
            server.server_close()

    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        print(f"       last output:\n{complete.stdout}{complete.stderr}")
        print(f"FAILED: {len(failed)} of {len(checks)} check(s)")
        return 1
    print(f"OK: {len(checks)} remote link check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
