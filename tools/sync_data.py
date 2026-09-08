#!/usr/bin/env python3
"""Stream the built store to Source Cooperative, object to object.

The store on the UZH gateway *is* the published tree: ``publish-year`` writes
its items and rasters there and ``finalize-catalog`` patches its collection
documents with the year links, the extent and the PMTiles registrations. So
publication copies that tree and nothing else -- no staging directory, no second
walk over the tracked ``catalog/``, which would put the unpatched documents over
the patched ones.

Both sides are :class:`~upath.UPath` roots. The source is the store root the
build wrote to, with the ``GLACE_S3_*`` profile, endpoint and checksum settings
every other step uses. The target is the ``write_prefix`` in
``catalog.publish.yaml`` opened with the ``profile`` named there, and nothing
else: that profile's config section carries the Source Cooperative endpoint,
and botocore reads it from there. Bytes never touch local disk. Each object is
read from one root and written to the other in chunks, and the retry budget
``freezebase`` puts on every S3 path covers both sides.

Change detection is size against size, from one listing per side. That is a weak
content check and deliberately all this does: an ETag is an MD5 for a
single-part upload and something else for a multipart one, so keying on it would
constrain how anything may ever write. ``--force`` is the way out when a
truncated object of the right size has to be replaced. Nothing is ever deleted:
removing a file from the store leaves the object published.

    uv run --env-file .env python tools/sync_data.py                 # dry run
    uv run --env-file .env python tools/sync_data.py --confirm       # upload
    uv run --env-file .env python tools/sync_data.py --prefix tiles/2024 --confirm
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import shutil
import sys
from typing import TYPE_CHECKING, Protocol, cast

from botocore.exceptions import BotoCoreError, ClientError
from freezebase.s3 import make_s3_upath
from tqdm import tqdm
from upath import UPath
import yaml

from glace_catalog.config import settings
from glace_catalog.stac.store import list_objects_recursive

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
PUBLISH_CONFIG = ROOT / "catalog.publish.yaml"

DEFAULT_WORKERS = 8
DEFAULT_CONTENT_TYPE = "application/octet-stream"
COPY_CHUNK = 8 << 20
"""Bytes moved per read on a transfer; a COG is hundreds of megabytes."""

CONTENT_TYPES: dict[str, str] = {
    ".json": "application/json",
    ".md": "text/markdown",
    ".parquet": "application/vnd.apache.parquet",
    ".tif": "image/tiff; application=geotiff; profile=cloud-optimized",
    ".pmtiles": "application/vnd.pmtiles",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".yaml": "application/yaml",
}
"""Content type per suffix, as the object is served to a browser or a client.

Source Cooperative serves what the upload declared, so this table is what makes
a COG open by range request instead of downloading, a README render instead of
saving, and the item mirror recognisable as parquet. Anything unlisted goes up
as an opaque byte stream rather than as a guess: s3fs would otherwise consult
``mimetypes``, which knows none of the cloud-native suffixes.
"""


def read_target(path: Path = PUBLISH_CONFIG) -> UPath:
    """Open the publication target ``catalog.publish.yaml`` names.

    Parameters
    ----------
    path
        The configuration file.

    Returns
    -------
    UPath
        The ``write_prefix``, authenticated with the ``profile``. No endpoint is
        passed: the profile's config section carries it. The checksum policy
        is freezebase's ``when_required``, which sends no ``x-amz-checksum``
        trailers, the setting an S3-compatible proxy is least likely to
        reject; boto3's own default, ``when_supported``, was what the proxy
        was originally tried with.

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


def bucket_root(root: Path | UPath) -> UPath:
    """Check that the store root is on object storage, and return it.

    There is no staging step: what publication reads is the bucket the build
    wrote to, so a local root is a configuration mistake rather than a mode.

    Parameters
    ----------
    root
        Resolved store root.

    Returns
    -------
    UPath
        The same root, as an S3 path.

    Raises
    ------
    ValueError
        If the root is not an ``s3://`` location.
    """
    path = UPath(root)
    if path.protocol != "s3":
        msg = (
            f"GLACE_STORE_ROOT is {root}, which is not on object storage. "
            "Publication streams from the bucket the build wrote to; set it to "
            "the s3:// prefix that holds the store."
        )
        raise ValueError(msg)
    return path


def content_type_for(relative: str) -> str:
    """Content type of a store-relative path, by its suffix."""
    return CONTENT_TYPES.get(PurePosixPath(relative).suffix.lower(), DEFAULT_CONTENT_TYPE)


def is_build_state(relative: str) -> bool:
    """Whether a store-relative path is build state rather than published content.

    ``.build-state.json`` and ``.items.parquet`` sit beside the documents they
    were built from. The leading dot is what marks them, and it is the only
    thing that does.
    """
    return PurePosixPath(relative).name.startswith(".")


@dataclass(frozen=True)
class Plan:
    """What one sync would do, before it does any of it.

    Every field holds store-relative paths, sorted. ``upload`` is what actually
    transfers: ``new`` and ``changed``, or everything published when the run
    was forced.
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


def plan_sync(
    source: Mapping[str, int],
    target: Mapping[str, int],
    *,
    force: bool = False,
) -> Plan:
    """Compare two listings and classify every object in the source one.

    Parameters
    ----------
    source
        Store-relative path to size, from the private gateway.
    target
        Store-relative path to size, from Source Cooperative.
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
        elif published != source[relative]:
            changed.append(relative)
        else:
            unchanged.append(relative)
    upload = sorted([*new, *changed, *unchanged]) if force else sorted([*new, *changed])
    return Plan(
        new=tuple(new),
        changed=tuple(changed),
        unchanged=tuple(unchanged),
        skipped=tuple(skipped),
        upload=tuple(upload),
        sizes=dict(source),
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


def list_subtree(root: UPath, subtree: str) -> dict[str, int]:
    """List one subtree of a root as store-relative path to size.

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
        Store-relative path to size. Relative to ``root``, not to the subtree,
        so the two sides compare and copy by the same names.
    """
    listed = list_objects_recursive(root / subtree if subtree else root)
    if not subtree:
        return listed
    return {f"{subtree}/{name}": size for name, size in listed.items()}


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
    """Stream one object from the private gateway to Source Cooperative.

    The content type goes with the write: s3fs hands ``ContentType`` to the
    upload it starts. The write is committed by hand rather than on close,
    because an fsspec file closed by a ``with`` block commits whatever it has
    even when the block is leaving on an exception: a read that failed
    half-way through a COG would otherwise complete the multipart upload and
    publish a truncated object under the right name. With ``autocommit`` off,
    closing only uploads the last part; ``commit`` completes the object and
    ``discard`` aborts it, so a failure leaves nothing behind.
    """
    reader_path = source_root / relative
    writer_path = target_root / relative
    # The reader opens first: a source that cannot be read starts no upload.
    with reader_path.open("rb") as reader:
        # fsspec types the handle as a plain binary file; the two-phase calls
        # are what every AbstractBufferedFile, and so every S3File, provides.
        writer = cast(
            "TwoPhaseFile",
            writer_path.open("wb", ContentType=content_type_for(relative), autocommit=False),
        )
        try:
            shutil.copyfileobj(reader, writer, COPY_CHUNK)
            writer.close()
            writer.commit()
        except BaseException:
            # The library's own order: a file is closed before it is discarded.
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

    A failure is reported rather than raised so that one unreadable object does
    not abandon the other twenty-six thousand.
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
            # A publication run is long enough to need a progress bar and gets
            # read from a terminal; in a log or a gate it would be noise.
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
    parser.add_argument("--confirm", action="store_true", help="actually upload")
    parser.add_argument("--force", action="store_true", help="upload every object again")
    parser.add_argument(
        "--prefix",
        default="",
        help="sync only this subtree of the store, e.g. tiles/2024",
    )
    parser.add_argument(
        "--workers",
        type=positive_int,
        default=DEFAULT_WORKERS,
        help=f"size of the transfer pool (default {DEFAULT_WORKERS})",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Plan the sync, print it, and run it when confirmed."""
    args = parse_args(argv)
    subtree = args.prefix.strip("/")

    # Everything up to the plan is configuration and two remote listings, and
    # every way they fail -- an absent profile, a rejected credential, an
    # unreachable endpoint -- is a message for the person running this, not a
    # traceback.
    try:
        target = read_target()
        root = bucket_root(settings.store_root)
        source = list_subtree(root, subtree)
        published = list_subtree(target, subtree)
    except (BotoCoreError, ClientError, OSError, ValueError) as error:
        print(f"error  {error}", file=sys.stderr)
        return 1

    listing_root = root / subtree if subtree else root
    plan = plan_sync(source, published, force=args.force)
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
