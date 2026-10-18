#!/usr/bin/env python3
"""Upload the built store to Source Cooperative.

The store at ``GLACE_STORE_ROOT``, a local directory or an ``s3://`` prefix, is
the complete published tree: ``build-year.py`` writes the items and rasters,
and ``finalize-catalog.py`` writes the collection documents. This script copies
that tree to the ``write_prefix`` in ``catalog.publish.yaml``, using the
``profile`` named there; the profile's AWS configuration supplies the endpoint.
Each object streams from source to target in chunks. Files whose name starts
with a dot are build state and stay local.

An object is uploaded when it is new or changed. Changes are judged from one
listing per side:

1. A different size means changed.
2. At equal size, when both sides list an MD5 ETag (single-part uploads), the
   ETags decide.
3. Otherwise, the object is uploaded when the source copy is newer.

``--force`` uploads every object, for example to replace a truncated object
that has the right size. Publishing only adds and overwrites objects; removing
one from the target is a separate, manual action.

    make publish-plan  # dry run
    make publish       # upload
    uv run --env-file .env tools/publish.py --prefix tiles/2024 --confirm
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from typing import TYPE_CHECKING, Any, Protocol, cast

from botocore.exceptions import BotoCoreError, ClientError
from freezebase.s3 import content_type_for as default_content_type_for
from freezebase.s3 import make_s3_upath
from tqdm import tqdm
from upath import UPath
import yaml

from glace_catalog.config import settings

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
PUBLISH_CONFIG = ROOT / "catalog.publish.yaml"

DEFAULT_WORKERS = 32
"""Concurrent transfers; most objects are tile COGs of about 1 MB, bound by request latency."""
COPY_CHUNK = 8 << 20
"""Bytes moved per read on a transfer; a COG is hundreds of megabytes."""
SINGLE_PART_ETAG = re.compile(r"^[0-9a-f]{32}$")
"""An ETag that is the object's MD5: one hex digest, no part count after it."""


def content_type_for(path: str) -> str:
    """Freezebase's content type for ``path``, plus ``image/webp`` for thumbnails."""
    if PurePosixPath(path).suffix.lower() == ".webp":
        return "image/webp"
    return default_content_type_for(path)


def read_target(path: Path = PUBLISH_CONFIG) -> UPath:
    """Open the publication target ``catalog.publish.yaml`` names.

    Parameters
    ----------
    path
        The configuration file.

    Returns
    -------
    UPath
        The ``write_prefix``, opened with ``profile``. The endpoint comes from
        that profile's AWS configuration. Checksums follow freezebase's
        ``when_required`` policy, which S3-compatible proxies such as Source
        Cooperative accept.

    Raises
    ------
    ValueError
        If ``write_prefix`` or ``profile`` is missing, or ``write_prefix`` is
        not an ``s3://`` URI.
    """
    config = yaml.safe_load(path.read_text()) or {}
    missing = [key for key in ("write_prefix", "profile") if not config.get(key)]
    if missing:
        msg = f"{path} sets no {' and no '.join(missing)}."
        raise ValueError(msg)
    uri = str(config["write_prefix"]).rstrip("/")
    if not uri.startswith("s3://") or not uri[len("s3://") :]:
        msg = f"{path}: write_prefix {uri!r} is not an s3:// URI naming a bucket."
        raise ValueError(msg)
    return make_s3_upath(uri, profile=str(config["profile"]))


def store_root() -> UPath:
    """Open the built store, on local disk or object storage, as the source root."""
    return UPath(settings.store_root)


def is_build_state(relative: str) -> bool:
    """Whether a store-relative path is build state, which stays local.

    Build state files, such as ``.build-state.json`` and ``.items.parquet``,
    are exactly the files whose name starts with a dot.
    """
    return PurePosixPath(relative).name.startswith(".")


@dataclass(frozen=True)
class Entry:
    """One listed object: its size, and its ETag and write time when listed."""

    size: int
    etag: str | None = None
    modified: datetime | None = None

    @property
    def md5(self) -> str | None:
        """The ETag when it is the MD5 of the bytes, else ``None``.

        A single-part upload lists its MD5. A multipart upload lists the MD5
        of its part digests followed by a dash and the part count, which says
        nothing about the content.
        """
        if self.etag is None:
            return None
        digest = self.etag.strip('"').lower()
        return digest if SINGLE_PART_ETAG.match(digest) else None


@dataclass(frozen=True)
class Plan:
    """What one upload would do, before it does any of it.

    Every field holds store-relative paths, sorted. ``upload`` lists the paths
    to transfer: ``new`` and ``changed``, plus ``unchanged`` when forced.
    """

    new: tuple[str, ...]
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    skipped: tuple[str, ...]
    upload: tuple[str, ...]
    sizes: Mapping[str, int]

    def bytes_of(self, names: Sequence[str]) -> int:
        """Total source size of the named paths."""
        return sum(self.sizes.get(name, 0) for name in names)


def is_unchanged(source: Entry, target: Entry) -> bool:
    """Whether a published object matches the store's copy, judged from the listings.

    The first rule that applies decides:

    1. Different sizes: changed.
    2. Both sides list an MD5 ETag: unchanged when the two match.
    3. Unchanged when the source is no newer than the published copy, since
       the build writes the store before publication copies it. When either
       side lists no time, the object counts as changed.
    """
    if source.size != target.size:
        return False
    if source.md5 is not None and target.md5 is not None:
        return source.md5 == target.md5
    if source.modified is None or target.modified is None:
        return False
    return source.modified <= target.modified


def plan_upload(
    source: Mapping[str, Entry],
    target: Mapping[str, Entry],
    *,
    force: bool = False,
) -> Plan:
    """Compare two listings and classify every object in the source one.

    Parameters
    ----------
    source
        Store-relative path to entry, from the store.
    target
        Store-relative path to entry, from Source Cooperative.
    force
        Upload every published object regardless of what the target holds.

    Returns
    -------
    Plan
        The four categories and the paths that would transfer.
    """
    new: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    skipped: list[str] = []
    for relative in sorted(source):
        if is_build_state(relative):
            skipped.append(relative)
            continue
        published = target.get(relative)
        if published is None:
            new.append(relative)
        elif is_unchanged(source[relative], published):
            unchanged.append(relative)
        else:
            changed.append(relative)
    upload = sorted([*new, *changed, *unchanged]) if force else sorted([*new, *changed])
    return Plan(
        new=tuple(new),
        changed=tuple(changed),
        unchanged=tuple(unchanged),
        skipped=tuple(skipped),
        upload=tuple(upload),
        sizes={name: entry.size for name, entry in source.items()},
    )


BINARY_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")
KIBI = 1024


def human_bytes(size: int) -> str:
    """Format a byte count with a binary unit, for the summary table."""
    value = float(size)
    for unit in BINARY_UNITS[:-1]:
        if value < KIBI:
            return f"{value:.0f} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= KIBI
    return f"{value:.1f} {BINARY_UNITS[-1]}"


def summary_lines(plan: Plan, source: str, target: UPath) -> list[str]:
    """Render the plan as the table printed before anything uploads."""
    rows = (
        ("new", plan.new),
        ("changed", plan.changed),
        ("unchanged", plan.unchanged),
        ("skipped", plan.skipped),
    )
    profile = target.storage_options.get("profile")
    lines = [
        f"source:  {source}",
        f"target:  {target}  (profile {profile})",
        "",
    ]
    lines.extend(
        f"  {name:<10}{len(names):>7}  {human_bytes(plan.bytes_of(names)):>10}"
        for name, names in rows
    )
    total = human_bytes(plan.bytes_of(plan.upload))
    lines.append("  " + "-" * 27)
    lines.append(f"  {'upload':<10}{len(plan.upload):>7}  {total:>10}")
    lines.append("")
    lines.append("nothing is ever deleted; removing a file from the store leaves it published")
    return lines


def listed_time(info: Mapping[str, Any]) -> datetime | None:
    """Read the write time off an fsspec listing entry, timezone-aware, or ``None``.

    s3fs passes ``LastModified`` through as an aware datetime; other
    filesystems name a float ``mtime`` or ``created``.
    """
    for key in ("LastModified", "mtime", "created"):
        value = info.get(key)
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=UTC)
    return None


def list_objects(directory: UPath) -> dict[str, Entry]:
    """List everything below a prefix as relative path to entry, in one call.

    Works like freezebase's ``list_object_sizes``, but each entry also keeps
    the ETag and write time that :func:`is_unchanged` uses. Returns an empty
    dict when the prefix is absent.
    """
    prefix = str(directory.path).rstrip("/") + "/"
    try:
        found = cast(
            "dict[str, dict[str, Any]]",
            directory.fs.find(str(directory.path), detail=True),
        )
    except FileNotFoundError:
        return {}
    entries: dict[str, Entry] = {}
    for info in found.values():
        if info.get("type") == "directory":
            continue
        name = str(info["name"])
        if not name.startswith(prefix):
            continue
        size = int(info.get("size") or info.get("Size") or 0)
        # A zero-byte "folder marker" key ends in a slash and lists as a file.
        if size == 0 and name.endswith("/"):
            continue
        etag = info.get("ETag") or info.get("etag")
        entries[name[len(prefix) :]] = Entry(size, str(etag) if etag else None, listed_time(info))
    return entries


def list_subtree(root: UPath, subtree: str) -> dict[str, Entry]:
    """List one subtree of a root as store-relative path to entry.

    Parameters
    ----------
    root
        Store root on either side.
    subtree
        Store-relative subtree to list, matching ``--prefix``. Empty lists all
        of it.

    Returns
    -------
    dict
        Store-relative path to entry. Paths are relative to ``root``, so both
        sides compare and copy by the same names.
    """
    listed = list_objects(root / subtree if subtree else root)
    if not subtree:
        return listed
    return {f"{subtree}/{name}": entry for name, entry in listed.items()}


class TwoPhaseFile(Protocol):
    """A write handle whose close uploads and whose commit publishes."""

    def write(self, data: bytes, /) -> int:
        """Buffer bytes, uploading a part when the buffer fills."""
        ...

    def close(self) -> None:
        """Upload what is left in the buffer."""
        ...

    def commit(self) -> None:
        """Complete the object."""
        ...

    def discard(self) -> None:
        """Abort the upload."""
        ...


def transfer_one(source_root: UPath, target_root: UPath, relative: str) -> None:
    """Stream one object from the store to Source Cooperative.

    The upload declares the object's content type, and Source Cooperative
    serves it back, so COGs open by range request and READMEs render in a
    browser.

    The upload is committed explicitly. An fsspec file closed by a ``with``
    block commits even when an exception is leaving the block, which would
    publish a truncated object. With ``autocommit=False``, ``close`` uploads
    the last part, ``commit`` completes the object and ``discard`` aborts it,
    so a failed transfer leaves the target as it was.
    """
    reader_path = source_root / relative
    writer_path = target_root / relative
    # The reader opens first: a source that cannot be read starts no upload.
    with reader_path.open("rb") as reader:
        # fsspec types the handle as a plain binary file; S3File, like every
        # AbstractBufferedFile, also provides commit and discard.
        writer = cast(
            "TwoPhaseFile",
            writer_path.open("wb", ContentType=content_type_for(relative), autocommit=False),
        )
        try:
            shutil.copyfileobj(reader, writer, COPY_CHUNK)
            writer.close()
            writer.commit()
        except BaseException:
            # fsspec's own order: close the file, then discard it.
            with suppress(Exception):
                writer.close()
            with suppress(Exception):
                writer.discard()
            raise


def transfer(
    source_root: UPath,
    target_root: UPath,
    relatives: Sequence[str],
    *,
    workers: int = DEFAULT_WORKERS,
) -> list[str]:
    """Stream every named object, returning the paths that failed.

    Each failure is printed to stderr, and the remaining objects still
    transfer.
    """

    def move(relative: str) -> str | None:
        try:
            transfer_one(source_root, target_root, relative)
        except Exception as error:  # noqa: BLE001 - one object must not stop the run
            print(f"error  {relative}: {error}", file=sys.stderr)
            return relative
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        moved = tqdm(
            pool.map(move, relatives),
            total=len(relatives),
            unit="obj",
            # Show the progress bar on an interactive terminal only.
            disable=not sys.stderr.isatty(),
        )
        return [name for name in moved if name is not None]


def positive_int(raw: str) -> int:
    """Parse a pool size, refusing zero and below.

    Raises
    ------
    argparse.ArgumentTypeError
        If ``raw`` is not a whole number of at least one.
    """
    try:
        value = int(raw)
    except ValueError:
        msg = f"{raw} is not a whole number."
        raise argparse.ArgumentTypeError(msg) from None
    if value < 1:
        msg = f"{value} is not a usable pool size; give at least 1."
        raise argparse.ArgumentTypeError(msg)
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Stream the built store to Source Cooperative.",
        epilog="Dry run by default. Never deletes.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="upload; without this flag, only print the plan",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="upload every object, changed or not",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="publish only this subtree of the store, e.g. tiles/2024",
    )
    parser.add_argument(
        "--workers",
        type=positive_int,
        default=DEFAULT_WORKERS,
        help=f"size of the transfer pool (default {DEFAULT_WORKERS})",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Plan the upload, print it, and run it when confirmed."""
    args = parse_args(argv)
    subtree = args.prefix.strip("/")

    # Configuration and listing errors (a missing profile, rejected
    # credentials, an unreachable endpoint) print as one-line messages.
    try:
        target = read_target()
        root = store_root()
        source = list_subtree(root, subtree)
        published = list_subtree(target, subtree)
    except (BotoCoreError, ClientError, OSError, ValueError) as error:
        print(f"error  {error}", file=sys.stderr)
        return 1

    listing_root = root / subtree if subtree else root
    plan = plan_upload(source, published, force=args.force)
    for line in summary_lines(plan, str(listing_root), target):
        print(line)
    if not source:
        print(f"note   {listing_root} holds no objects; check --prefix and the store root")

    if not args.confirm:
        print("\ndry run; re-run with --confirm to upload")
        return 0
    if not plan.upload:
        print("\nnothing to upload")
        return 0

    failed = transfer(root, target, plan.upload, workers=args.workers)
    if failed:
        print(f"\n{len(failed)} of {len(plan.upload)} object(s) failed:", file=sys.stderr)
        for name in failed:
            print(f"  {name}", file=sys.stderr)
        return 1
    print(f"\nuploaded {len(plan.upload)} object(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
