#!/usr/bin/env python3
"""Gate on what publication would upload, before it uploads anything.

``tools/sync_data.py`` is the only thing between the private store and a public
bucket, and its two decisions -- which objects move, and what they are served as
-- are both made from listings rather than from bytes. That makes them checkable
without a network, and this is where they are checked: the change detection over
a synthetic pair of listings, the content types, the target the publication
file names, publication from a local store root, and the dry run, which is the
last thing a person reads before typing ``--confirm``.

Both sides of a transfer are in-memory filesystems, because the tool only ever
asks a root for a listing, a reader and a writer. Nothing here opens a socket.

Run: uv run tests/test_sync_data.py
"""

from __future__ import annotations

from argparse import ArgumentTypeError
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import BytesIO, StringIO
import os
from pathlib import Path
import sys
import tempfile
from typing import TYPE_CHECKING, Any, Self, cast, override
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

# glace_catalog.config resolves its locations at import, so importing the tool
# needs the variable set. A local value keeps every path in this gate off
# object storage; nothing here reads or writes the location.
os.environ.setdefault("GLACE_STORE_ROOT", str(ROOT / "does-not-exist" / "store"))

from botocore.exceptions import ProfileNotFound  # noqa: E402
from fsspec.implementations.memory import MemoryFileSystem  # noqa: E402
import sync_data  # noqa: E402
from upath import UPath  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Mapping

Entry = sync_data.Entry

BUILT = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
"""When the store was written, in the synthetic listings."""
PUBLISHED_AT = BUILT + timedelta(hours=6)
"""When it was last synced: after the build, the way publication always is."""

# One year of one collection, cut down: a document, a raster, an archive, the
# item mirror, and the two dotfiles that are build state rather than content.
SOURCE: dict[str, int] = {
    "catalog.json": 1_024,
    "tiles/collection.json": 2_048,
    "tiles/items.parquet": 8_192,
    "tiles/2024/.build-state.json": 512,
    "tiles/2024/.items.parquet": 4_096,
    "tiles/2024/32TMS24/item.json": 3_072,
    "tiles/2024/32TMS24/coh12_vv.tif": 1_048_576,
    "mosaics/2024/coh12_rgb_viz.pmtiles": 2_097_152,
}

# What the target already holds: the root catalog unchanged, the collection
# document rewritten at a different size, and nothing else.
PUBLISHED: dict[str, int] = {
    "catalog.json": 1_024,
    "tiles/collection.json": 1_500,
}


def memory_root(listing: Mapping[str, int], written: datetime = BUILT) -> UPath:
    """Make a fresh in-memory root holding one object of the listed size per path.

    Every object is stamped ``written``, so a test says which side is newer
    instead of depending on the order it built the roots in.
    """
    root = UPath(f"memory://{uuid4().hex}")
    for relative, size in listing.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
        touch(root, relative, written)
    return root


def touch(root: UPath, relative: str, when: datetime) -> None:
    """Set the listed timestamp of one in-memory object."""
    store = cast("MemoryFileSystem", root.fs).store
    store[f"{root.path}/{relative}"].created = when


def entries(sizes: Mapping[str, int], written: datetime = BUILT) -> dict[str, Entry]:
    """Make a listing without ETags, every object stamped ``written``."""
    return {name: Entry(size, modified=written) for name, size in sizes.items()}


def sizes_of(listing: Mapping[str, Entry]) -> dict[str, int]:
    """Reduce a listing to its sizes."""
    return {name: entry.size for name, entry in listing.items()}


class TwoRoots:
    """Point the tool's source and target at two roots, for a run of ``main``."""

    def __init__(self, source_root: UPath, target_root: UPath) -> None:
        self.patches = (
            patch.object(sync_data, "read_target", self._returning(target_root)),
            patch.object(sync_data, "store_root", self._returning(source_root)),
        )

    @staticmethod
    def _returning(root: UPath) -> Any:
        def opened(*_: object) -> UPath:
            return root

        return opened

    def __enter__(self) -> Self:
        """Apply both patches."""
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Undo both patches."""
        for item in self.patches:
            item.stop()


def check_change_detection() -> dict[str, bool]:
    """Classification of a listing against what is already published."""
    source, published = entries(SOURCE), entries(PUBLISHED, PUBLISHED_AT)
    plan = sync_data.plan_sync(source, published)
    forced = sync_data.plan_sync(source, published, force=True)
    return {
        "an object the target lacks is new": plan.new
        == (
            "mosaics/2024/coh12_rgb_viz.pmtiles",
            "tiles/2024/32TMS24/coh12_vv.tif",
            "tiles/2024/32TMS24/item.json",
            "tiles/items.parquet",
        ),
        "a different size is changed": plan.changed == ("tiles/collection.json",),
        "an equal size published after the build is unchanged": plan.unchanged
        == ("catalog.json",),
        "a dotfile is skipped whatever its size": plan.skipped
        == ("tiles/2024/.build-state.json", "tiles/2024/.items.parquet"),
        "a skipped dotfile never uploads": not any(
            "/." in name for name in (*plan.upload, *forced.upload)
        ),
        "the upload set is the new and the changed": plan.upload
        == tuple(sorted([*plan.new, *plan.changed])),
        "--force uploads every published object": forced.upload
        == tuple(sorted([*plan.new, *plan.changed, *plan.unchanged])),
        "sizes come from the source listing": plan.bytes_of(plan.upload)
        == sum(SOURCE[name] for name in plan.upload),
    }


MD5_A = "9e107d9d372bb6826bd81d3542a419d6"
MD5_B = "e4d909c290d0fb1ca068ffaddf22cbd0"
MULTIPART = f"{MD5_A}-3"
"""What a multipart upload lists as: an MD5 of the part digests, then the part count."""

SIZE = 2_048
REBUILT = PUBLISHED_AT + timedelta(hours=1)
"""A build after the last publication: what a rewritten store object carries."""


def check_same_size_detection() -> dict[str, bool]:
    """Check what decides a same-size object: two MD5s, else which side is newer.

    A single-part ETag is the MD5, so two of them settle it from the listings
    whatever the timestamps say. Anything else falls to the rule ``aws s3
    sync`` uses: the build writes the store before publication copies it, so a
    source object newer than its published copy was rewritten since.
    """
    source = {
        "etag/equal.json": Entry(SIZE, MD5_A, REBUILT),
        "etag/differs.json": Entry(SIZE, MD5_A, BUILT),
        "time/older.tif": Entry(SIZE, MULTIPART, BUILT),
        "time/same.tif": Entry(SIZE, None, PUBLISHED_AT),
        "time/newer.tif": Entry(SIZE, MULTIPART, REBUILT),
        "time/one-md5-newer.json": Entry(SIZE, MD5_A, REBUILT),
        "time/unknown-source.tif": Entry(SIZE, None, None),
        "time/unknown-target.tif": Entry(SIZE, None, BUILT),
        "time/quoted-etags.json": Entry(SIZE, f'"{MD5_A}"', REBUILT),
    }
    target = {
        "etag/equal.json": Entry(SIZE, MD5_A, PUBLISHED_AT),
        "etag/differs.json": Entry(SIZE, MD5_B, PUBLISHED_AT),
        "time/older.tif": Entry(SIZE, MULTIPART, PUBLISHED_AT),
        "time/same.tif": Entry(SIZE, None, PUBLISHED_AT),
        "time/newer.tif": Entry(SIZE, MULTIPART, PUBLISHED_AT),
        "time/one-md5-newer.json": Entry(SIZE, MULTIPART, PUBLISHED_AT),
        "time/unknown-source.tif": Entry(SIZE, None, PUBLISHED_AT),
        "time/unknown-target.tif": Entry(SIZE, None, None),
        "time/quoted-etags.json": Entry(SIZE, f'"{MD5_A.upper()}"', PUBLISHED_AT),
    }
    plan = sync_data.plan_sync(source, target)
    return {
        "two equal MD5s are unchanged even when the source is newer": {
            "etag/equal.json",
            "time/quoted-etags.json",
        }
        <= set(plan.unchanged),
        "two different MD5s are changed even when the source is older": "etag/differs.json"
        in plan.changed,
        "without two MD5s, a source older than its copy is unchanged": "time/older.tif"
        in plan.unchanged,
        "without two MD5s, a source as old as its copy is unchanged": "time/same.tif"
        in plan.unchanged,
        "without two MD5s, a newer source is changed": {
            "time/newer.tif",
            "time/one-md5-newer.json",
        }
        <= set(plan.changed),
        "a side without a timestamp cannot clear an object": {
            "time/unknown-source.tif",
            "time/unknown-target.tif",
        }
        <= set(plan.changed),
        "every object landed in exactly one category": sorted(
            [*plan.new, *plan.changed, *plan.unchanged, *plan.skipped],
        )
        == sorted(source),
    }


def check_content_types() -> dict[str, bool]:
    """Check the content types the store's suffixes are served as, via freezebase."""
    cases = {
        "tiles/2024/32TMS24/item.json": "application/json",
        "README.md": "text/markdown",
        "tiles/items.parquet": "application/vnd.apache.parquet",
        "tiles/2024/32TMS24/coh12_vv.tif": (
            "image/tiff; application=geotiff; profile=cloud-optimized"
        ),
        "mosaics/2024/coh12_rgb_viz.pmtiles": "application/vnd.pmtiles",
        "mosaics/thumbnail.webp": "image/webp",
        "_assets/glace-wordmark.svg": "image/svg+xml",
        "tiles/2024/config.yaml": "application/yaml",
        "tiles/2024/32TMS24/COH12_VV.TIF": (
            "image/tiff; application=geotiff; profile=cloud-optimized"
        ),
        "notes.txt": "text/plain",
        "LICENSE": "application/octet-stream",
    }
    wrong = {
        name: sync_data.content_type_for(name)
        for name, expected in cases.items()
        if sync_data.content_type_for(name) != expected
    }
    for name, got in wrong.items():
        print(f"error  {name} is served as {got}")
    return {f"{len(cases)} content types": not wrong}


def check_target_config() -> dict[str, bool]:
    """Check the target the repository's own publication file opens.

    Building the path opens no connection, so this can read the real file: the
    profile only has to exist once a request is signed.
    """
    target = sync_data.read_target()
    options = target.storage_options
    checks = {
        "the target is an S3 path": target.protocol == "s3",
        "catalog.publish.yaml names the account bucket and the product prefix": str(target)
        == "s3://giuz/glace-alps",
        "the profile is the one catalog.publish.yaml names": options.get("profile")
        == "source-coop",
        "no endpoint is passed; the profile's config section carries it": options.get(
            "endpoint_url",
        )
        is None,
        "the retry budget is freezebase's": options.get("config_kwargs", {}).get("retries")
        == {"max_attempts": 10, "mode": "adaptive"},
    }

    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "catalog.publish.yaml"
        config.write_text("write_prefix: s3://lqgentner/glace-ch/\nprofile: source-coop\n")
        checks["a trailing slash is not part of the prefix"] = (
            str(sync_data.read_target(config)) == "s3://lqgentner/glace-ch"
        )
        refused = {
            "a configuration without write_prefix": "public_base: https://x.invalid/\n",
            "a configuration without profile": "write_prefix: s3://lqgentner/glace-ch\n",
            "an https write_prefix": (
                "write_prefix: https://data.source.coop/lqgentner\nprofile: source-coop\n"
            ),
            "a write_prefix naming no bucket": "write_prefix: s3://\nprofile: source-coop\n",
        }
        for name, text in refused.items():
            config.write_text(text)
            try:
                sync_data.read_target(config)
            except ValueError:
                checks[f"{name} is refused"] = True
            else:
                checks[f"{name} is refused"] = False
    return checks


def check_local_root() -> dict[str, bool]:
    """Check that a store built on local disk publishes like one on the bucket.

    The store root comes from the real configuration here, not from
    :class:`TwoRoots`, so the resolution of a plain path is what is tested.
    """
    checks: dict[str, bool] = {}
    target_root = memory_root({})
    payload = {"catalog.json": b"{}", "tiles/2023/31TFJ68/coh12_vv.tif": b"x" * 4096}
    with tempfile.TemporaryDirectory() as tmp:
        for name, data in payload.items():
            path = Path(tmp) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (Path(tmp) / "tiles/2023/.build-state.json").write_text("{}")
        locations = sync_data.settings.locations
        original = locations.store_root
        locations.store_root = tmp
        try:
            with (
                patch.object(sync_data, "read_target", lambda: target_root),
                redirect_stdout(StringIO()),
            ):
                code = sync_data.main(["--confirm"])
        finally:
            locations.store_root = original
    checks["a local store root publishes"] = code == 0
    checks["every object arrives with its bytes"] = all(
        (target_root / name).exists() and (target_root / name).read_bytes() == data
        for name, data in payload.items()
    )
    checks["build state stays behind"] = not (
        target_root / "tiles/2023/.build-state.json"
    ).exists()
    return checks


class RecordingWrites:
    """Record the keyword arguments every in-memory write is opened with."""

    def __init__(self) -> None:
        self.opened: dict[str, dict[str, Any]] = {}
        self._original = MemoryFileSystem._open  # noqa: SLF001 - the seam under test

    def __enter__(self) -> Self:
        """Wrap the filesystem's opener for the duration of the block."""
        original = self._original
        opened = self.opened

        def record(fs: MemoryFileSystem, path: str, mode: str = "rb", **kwargs: Any) -> Any:
            if "w" in mode:
                opened[path] = dict(kwargs)
            return original(fs, path, mode, **kwargs)

        MemoryFileSystem._open = record  # noqa: SLF001 # pyrefly: ignore[bad-assignment]
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Restore the opener."""
        MemoryFileSystem._open = self._original  # noqa: SLF001


def check_listing_and_transfer() -> dict[str, bool]:
    """Check both listings and one transfer per object with its content type."""
    source_root = memory_root(SOURCE)
    target_root = memory_root(PUBLISHED, PUBLISHED_AT)
    source = sync_data.list_subtree(source_root, "")
    published = sync_data.list_subtree(target_root, "")
    plan = sync_data.plan_sync(source, published)
    # Listed before the transfer adds the year's objects under tiles/.
    subtree = sync_data.list_subtree(target_root, "tiles")

    with RecordingWrites() as writes:
        failed = sync_data.transfer(source_root, target_root, plan.upload, workers=2)
    written = {
        path.removeprefix(f"{target_root.path}/"): kwargs for path, kwargs in writes.opened.items()
    }
    after = sync_data.list_subtree(target_root, "")
    return {
        "the source listing comes back store-relative": sizes_of(source) == SOURCE,
        "the target listing comes back store-relative": sizes_of(published) == PUBLISHED,
        "a side without ETags lists them as absent": all(
            entry.etag is None for entry in source.values()
        ),
        "the listing carries the write time, timezone-aware": all(
            entry.modified == BUILT for entry in source.values()
        ),
        "a subtree lists only its own keys, still store-relative": sizes_of(subtree)
        == {"tiles/collection.json": 1_500},
        "an unpublished target lists as empty": sync_data.list_subtree(
            UPath(f"memory://{uuid4().hex}"),
            "",
        )
        == {},
        "no transfer failed": not failed,
        "every planned object was written under the target root": sorted(written)
        == sorted(plan.upload),
        "the bytes arrived whole": all(after[name].size == SOURCE[name] for name in plan.upload),
        "the unchanged object was left alone": after["catalog.json"].size
        == PUBLISHED["catalog.json"],
        "the raster is served as a COG": written["tiles/2024/32TMS24/coh12_vv.tif"].get(
            "ContentType",
        )
        == "image/tiff; application=geotiff; profile=cloud-optimized",
        "the document is served as JSON": written["tiles/collection.json"].get("ContentType")
        == "application/json",
    }


def check_failure_is_reported() -> dict[str, bool]:
    """One unreadable object is reported, not raised, and the rest still move."""
    source_root = memory_root(SOURCE)
    target_root = memory_root(PUBLISHED)
    plan = sync_data.plan_sync(entries(SOURCE), entries(PUBLISHED, PUBLISHED_AT))
    # The raster is not on the source at all, which is what a rejected read
    # looks like from the tool's side.
    (source_root / "tiles/2024/32TMS24/coh12_vv.tif").unlink()
    # The tool reports the failure on stderr as it happens; held here so the
    # gate's own output stays a list of what it checked.
    reported = StringIO()
    with redirect_stderr(reported):
        failed = sync_data.transfer(source_root, target_root, plan.upload, workers=2)
    after = sync_data.list_subtree(target_root, "")
    return {
        "the failed object is named": failed == ["tiles/2024/32TMS24/coh12_vv.tif"],
        "the reason reaches stderr": "coh12_vv.tif" in reported.getvalue(),
        "the others still uploaded": all(
            name in after for name in plan.upload if name not in failed
        ),
        "the failed object was not written": "tiles/2024/32TMS24/coh12_vv.tif" not in after,
    }


class TwoPhaseWriter(BytesIO):
    """A write handle with s3fs's lifecycle: close uploads, commit publishes.

    ``memory://`` commits at open and closes as a no-op, so it cannot show
    whether the tool commits by hand and discards on failure. This records the
    order of the calls and only stores the bytes on ``commit``.
    """

    def __init__(self, store: dict[str, bytes], path: str, **kwargs: Any) -> None:
        super().__init__()
        self.store = store
        self.path = path
        self.kwargs = kwargs
        self.calls: list[str] = []

    @override
    def close(self) -> None:
        """Record the close; the buffer stays readable for ``commit``."""
        self.calls.append("close")

    def commit(self) -> None:
        """Publish the bytes."""
        self.calls.append("commit")
        self.store[self.path] = self.getvalue()

    def discard(self) -> None:
        """Abort the upload."""
        self.calls.append("discard")


class TwoPhaseTarget:
    """Route every write on the in-memory target through :class:`TwoPhaseWriter`."""

    def __init__(self, failing_read: str | None = None) -> None:
        self.store: dict[str, bytes] = {}
        self.writers: dict[str, TwoPhaseWriter] = {}
        self.failing_read = failing_read
        self._original = MemoryFileSystem._open  # noqa: SLF001 - the seam under test

    def __enter__(self) -> Self:
        """Wrap the opener: two-phase writers, and one reader that breaks."""
        original = self._original
        target = self

        def opener(fs: MemoryFileSystem, path: str, mode: str = "rb", **kwargs: Any) -> Any:
            if "w" in mode:
                writer = TwoPhaseWriter(target.store, path, **kwargs)
                target.writers[path] = writer
                return writer
            handle = original(fs, path, mode, **kwargs)
            if target.failing_read and path.endswith(target.failing_read):
                return BrokenReader(handle.getvalue())
            return handle

        MemoryFileSystem._open = opener  # noqa: SLF001 # pyrefly: ignore[bad-assignment]
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Restore the opener."""
        MemoryFileSystem._open = self._original  # noqa: SLF001


class BrokenReader(BytesIO):
    """A reader that serves its first chunk and fails on the second."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.reads = 0

    @override
    def read(self, size: int | None = -1, /) -> bytes:
        """Fail the way a dropped connection does, after some bytes moved."""
        self.reads += 1
        if self.reads > 1:
            msg = "connection reset"
            raise OSError(msg)
        return super().read(size)


def check_commit_is_explicit() -> dict[str, bool]:
    """Check that a transfer commits after closing and a failed one only discards.

    An fsspec file closed by a ``with`` block commits whatever it holds even on
    an exception, which would publish a truncated COG under the right name.
    """
    small = {"tiles/collection.json": 2_048, "tiles/2024/32TMS24/coh12_vv.tif": 3 * 8 << 20}
    source_root = memory_root(small)
    target_root = memory_root({})
    checks: dict[str, bool] = {}

    with TwoPhaseTarget() as target:
        failed = sync_data.transfer(source_root, target_root, tuple(small), workers=1)
    good = target.writers[f"{target_root.path}/tiles/collection.json"]
    checks["a good transfer does not fail"] = not failed
    checks["the writer is opened without autocommit"] = good.kwargs.get("autocommit") is False
    checks["the content type still travels with the write"] = (
        good.kwargs.get("ContentType") == "application/json"
    )
    checks["a good transfer closes, then commits, and never discards"] = good.calls == [
        "close",
        "commit",
    ]
    checks["the committed bytes are whole"] = all(
        len(target.store[f"{target_root.path}/{name}"]) == size for name, size in small.items()
    )

    reported = StringIO()
    with TwoPhaseTarget(failing_read="coh12_vv.tif") as target, redirect_stderr(reported):
        failed = sync_data.transfer(source_root, target_root, tuple(small), workers=1)
    broken = target.writers[f"{target_root.path}/tiles/2024/32TMS24/coh12_vv.tif"]
    checks["a read that breaks half-way is reported"] = failed == [
        "tiles/2024/32TMS24/coh12_vv.tif",
    ]
    checks["the reason reaches stderr"] = "connection reset" in reported.getvalue()
    checks["the broken transfer is closed and discarded, never committed"] = broken.calls == [
        "close",
        "discard",
    ]
    checks["nothing is published under the broken object's name"] = (
        f"{target_root.path}/tiles/2024/32TMS24/coh12_vv.tif" not in target.store
    )
    checks["the other object still committed"] = (
        f"{target_root.path}/tiles/collection.json" in target.store
    )
    return checks


def _no_profile(path: Path = sync_data.PUBLISH_CONFIG) -> UPath:
    """Fail the way an absent ``source-coop`` profile does."""
    del path
    raise ProfileNotFound(profile="source-coop")


def check_setup_failures_are_messages() -> dict[str, bool]:
    """Check that a missing profile is one line and exit 1, not a traceback.

    Every failure between the configuration and the plan -- an absent profile,
    a rejected credential, an unreachable endpoint -- reaches a person who has
    to fix a credential file, so none of them may arrive as a stack trace.
    """
    checks: dict[str, bool] = {}
    original = sync_data.read_target
    sync_data.read_target = _no_profile
    reported = StringIO()
    try:
        with redirect_stderr(reported):
            code = sync_data.main([])
    finally:
        sync_data.read_target = original
    message = reported.getvalue()
    checks["a botocore failure exits non-zero"] = code == 1
    checks["it prints one line naming the profile"] = (
        len(message.strip().splitlines()) == 1 and "source-coop" in message
    )
    return checks


def check_workers_floor() -> dict[str, bool]:
    """Check that a pool of zero threads is refused at the command line."""
    checks: dict[str, bool] = {}
    for raw in ("0", "-4", "many"):
        try:
            sync_data.positive_int(raw)
        except ArgumentTypeError:
            checks[f"--workers {raw} is refused"] = True
        else:
            checks[f"--workers {raw} is refused"] = False
    checks["--workers 4 is a pool of four"] = sync_data.positive_int("4") == 4
    return checks


DRY_RUN = """\
source:  s3://s1-bursts/data/glace-alps
target:  s3://giuz/glace-alps  (profile source-coop)

  new             4     3.0 MiB
  changed         1     2.0 KiB
  unchanged       1     1.0 KiB
  skipped         2     4.5 KiB
  ---------------------------
  upload          5     3.0 MiB

nothing is ever deleted; removing a file from the store leaves it published\
"""


def check_summary() -> dict[str, bool]:
    """Check the table a person reads before typing ``--confirm``."""
    plan = sync_data.plan_sync(entries(SOURCE), entries(PUBLISHED, PUBLISHED_AT))
    rendered = "\n".join(
        sync_data.summary_lines(plan, "s3://s1-bursts/data/glace-alps", sync_data.read_target()),
    )
    if rendered != DRY_RUN:
        print("--- rendered ---")
        print(rendered)
    return {
        "the dry-run summary reads as recorded": rendered == DRY_RUN,
        "byte counts are binary": sync_data.human_bytes(1536) == "1.5 KiB",
        "small counts stay in bytes": sync_data.human_bytes(512) == "512 B",
    }


def check_dry_run_sees_a_same_size_edit() -> dict[str, bool]:
    """Check the P-17 case: a collection document re-stamped at an identical length.

    On 2026-09-09 ``tiles/collection.json`` changed its digests and kept its
    size, and a size-only plan called it unchanged. The dry run over a store
    published whole and then rebuilt in that one document has to say
    ``changed``, on the strength of the rewrite alone: the in-memory roots
    list no ETags, so this is the timestamp rule the ETags fall back to.
    """
    source_root = memory_root(SOURCE)
    target_root = memory_root(SOURCE, PUBLISHED_AT)
    (source_root / "tiles/collection.json").write_bytes(b"y" * SOURCE["tiles/collection.json"])
    touch(source_root, "tiles/collection.json", REBUILT)
    printed = StringIO()
    with TwoRoots(source_root, target_root), redirect_stdout(printed):
        code = sync_data.main([])
    lines = printed.getvalue().splitlines()
    return {
        "the dry run exits zero": code == 0,
        "the same-size edit counts as changed": any(
            line.split() == ["changed", "1", "2.0", "KiB"] for line in lines
        ),
        "everything else is unchanged": any(
            line.split()[:2] == ["unchanged", "5"] for line in lines
        ),
    }


def main() -> int:
    """Run every check, printing one line per failure."""
    checks: dict[str, bool] = {}
    for group in (
        check_change_detection,
        check_same_size_detection,
        check_content_types,
        check_target_config,
        check_local_root,
        check_listing_and_transfer,
        check_failure_is_reported,
        check_commit_is_explicit,
        check_setup_failures_are_messages,
        check_workers_floor,
        check_summary,
        check_dry_run_sees_a_same_size_edit,
    ):
        checks.update(group())
    failed = [name for name, ok in checks.items() if not ok]
    for name in failed:
        print(f"error  {name}")
    if failed:
        print(f"FAILED: {len(failed)} of {len(checks)} sync check(s)")
        return 1
    print(f"OK: {len(checks)} sync check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
