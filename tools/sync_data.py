#!/usr/bin/env python3
"""Stream the built store to Source Cooperative, object to object.

The store on the UZH gateway *is* the published tree: ``publish-year`` writes
its items and rasters there and ``finalize-catalog`` patches its collection
documents with the year links, the extent and the PMTiles registrations. So
publication copies that tree and nothing else -- no staging directory, no second
walk over the tracked ``catalog/``, which would put the unpatched documents over
the patched ones.

Bytes never touch local disk. A ``GetObject`` body on the private gateway is
handed straight to ``upload_fileobj`` on Source Cooperative, which streams it in
one part below the multipart threshold and in parallel parts above it.

Two clients, two credentials, and they are deliberately not the same session:
the source client comes from ``GLACIERS_S3_*`` (the private bucket, read-only
here) and the target client from the ``profile`` in ``catalog.publish.yaml``,
whose ``endpoint_url`` is the Source Cooperative S3 proxy.

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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import sys
from typing import IO, TYPE_CHECKING, Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from tqdm import tqdm
from upath import UPath
import yaml

from glace_catalog.config import settings
from glace_catalog.stac.store import list_objects_recursive

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
PUBLISH_CONFIG = ROOT / "catalog.publish.yaml"

DEFAULT_WORKERS = 8
DEFAULT_CONTENT_TYPE = "application/octet-stream"

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
as an opaque byte stream rather than as a guess.
"""


class SourceClient(Protocol):
    """The one call this makes against the private gateway."""

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]:  # noqa: N803
        """Open one object; ``["Body"]`` is a streaming file object."""
        ...


class Paginator(Protocol):
    """A botocore paginator over one listing operation."""

    def paginate(self, **kwargs: Any) -> Iterator[Mapping[str, Any]]:
        """Yield one response page per round trip."""
        ...


class TargetClient(Protocol):
    """The two calls this makes against Source Cooperative."""

    def get_paginator(self, operation_name: str, /) -> Paginator:
        """Paginator for ``list_objects_v2``."""
        ...

    def upload_fileobj(
        self,
        Fileobj: IO[bytes],  # noqa: N803
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
        ExtraArgs: Mapping[str, Any],  # noqa: N803
    ) -> None:
        """Stream a file object to a key, chunking it if it is large."""
        ...


@dataclass(frozen=True)
class Target:
    """Where publication writes, as ``catalog.publish.yaml`` states it."""

    bucket: str
    prefix: str
    profile: str

    def key(self, relative: str) -> str:
        """Full object key of a store-relative path."""
        return f"{self.prefix}/{relative}" if self.prefix else relative

    def __str__(self) -> str:
        """Render the target as the ``s3://`` URI it was configured from."""
        return f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"


def split_s3_uri(uri: str) -> tuple[str, str]:
    """Split an ``s3://bucket/key/prefix`` URI into its bucket and its key prefix.

    Parameters
    ----------
    uri
        An ``s3://`` URI. A trailing slash is not significant.

    Returns
    -------
    tuple of str
        Bucket, and the key prefix, which is empty for a bare bucket.

    Raises
    ------
    ValueError
        If ``uri`` does not start with ``s3://`` or names no bucket.
    """
    if not uri.startswith("s3://"):
        msg = f"{uri} is not an s3:// URI."
        raise ValueError(msg)
    bucket, _, prefix = uri[len("s3://") :].partition("/")
    if not bucket:
        msg = f"{uri} names no bucket."
        raise ValueError(msg)
    return bucket, prefix.strip("/")


def read_target(path: Path = PUBLISH_CONFIG) -> Target:
    """Read the publication target from ``catalog.publish.yaml``.

    Parameters
    ----------
    path
        The configuration file.

    Returns
    -------
    Target
        Bucket, key prefix and AWS profile.

    Raises
    ------
    ValueError
        If ``write_prefix`` or ``profile`` is missing.
    """
    config = yaml.safe_load(path.read_text()) or {}
    missing = [key for key in ("write_prefix", "profile") if not config.get(key)]
    if missing:
        msg = f"{path} sets no {' and no '.join(missing)}."
        raise ValueError(msg)
    bucket, prefix = split_s3_uri(str(config["write_prefix"]))
    return Target(bucket=bucket, prefix=prefix, profile=str(config["profile"]))


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


def summary_lines(plan: Plan, source: str, target: Target) -> list[str]:
    """Render the plan as the table printed before anything uploads."""
    rows = (
        ("new", plan.new),
        ("changed", plan.changed),
        ("unchanged", plan.unchanged),
        ("skipped", plan.skipped),
    )
    lines = [
        f"source:  {source}",
        f"target:  {target}  (profile {target.profile})",
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


def list_published(client: TargetClient, target: Target, subtree: str = "") -> dict[str, int]:
    """List what the target already holds, as store-relative path to size.

    Parameters
    ----------
    client
        Client for Source Cooperative.
    target
        Bucket and key prefix publication writes to.
    subtree
        Store-relative subtree to list, matching ``--prefix``. Empty lists all
        of it.

    Returns
    -------
    dict
        Store-relative path to size, for everything already published.
    """
    prefix = f"{target.prefix}/" if target.prefix else ""
    listed = f"{prefix}{subtree}/" if subtree else prefix
    published: dict[str, int] = {}
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=target.bucket,
        Prefix=listed,
    ):
        for entry in page.get("Contents", []):
            key = str(entry["Key"])
            if not key.startswith(listed):
                continue
            published[key[len(prefix) :]] = int(entry["Size"])
    return published


def transfer_one(
    source_client: SourceClient,
    target_client: TargetClient,
    relative: str,
    *,
    source_bucket: str,
    source_prefix: str,
    target: Target,
) -> None:
    """Stream one object from the private gateway to Source Cooperative."""
    key = f"{source_prefix}/{relative}" if source_prefix else relative
    body = source_client.get_object(Bucket=source_bucket, Key=key)["Body"]
    target_client.upload_fileobj(
        body,
        target.bucket,
        target.key(relative),
        {"ContentType": content_type_for(relative)},
    )


def transfer(
    source_client: SourceClient,
    target_client: TargetClient,
    relatives: Sequence[str],
    *,
    source_bucket: str,
    source_prefix: str,
    target: Target,
    workers: int = DEFAULT_WORKERS,
) -> list[str]:
    """Stream every named object, returning the paths that failed.

    A failure is reported rather than raised so that one unreadable object does
    not abandon the other twenty-six thousand.
    """

    def move(relative: str) -> str | None:
        try:
            transfer_one(
                source_client,
                target_client,
                relative,
                source_bucket=source_bucket,
                source_prefix=source_prefix,
                target=target,
            )
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


def gateway_config() -> Config:
    """Botocore configuration for the private gateway, from ``GLACIERS_S3_*``.

    All four ``GLACIERS_S3_*`` knobs have to reach the client, not just the
    profile and the endpoint: boto3 >= 1.36 defaults both checksum settings to
    ``when_supported``, which puts ``x-amz-checksum`` trailers on the wire and a
    ``ChecksumMode`` header on ``GetObject``, and the UZH Ceph gateway rejects
    them. ``config.py`` hands the same two values to ``make_s3_upath`` for the
    listing side, so this is what keeps the reads and the listing speaking the
    same dialect.
    """
    return Config(
        request_checksum_calculation=settings.s3.request_checksum_calculation,
        response_checksum_validation=settings.s3.response_checksum_validation,
    )


def source_client(session: Any = None) -> SourceClient:
    """Build the read client for the private gateway from ``GLACIERS_S3_*``.

    The same profile, endpoint and checksum settings the store's
    :class:`~upath.UPath` carries, so the listing and the reads cannot end up
    authenticated or encoded differently.

    Parameters
    ----------
    session
        Session to build the client from. Defaults to the configured profile's;
        the gate passes a recording stand-in rather than opening a socket.

    Returns
    -------
    SourceClient
        Client for ``GetObject`` against the private bucket.
    """
    if session is None:
        session = boto3.Session(profile_name=settings.s3.profile)
    return session.client(
        "s3",
        endpoint_url=settings.s3.endpoint_url,
        config=gateway_config(),
    )


def target_client(target: Target) -> TargetClient:
    """Build the write client for Source Cooperative.

    No ``endpoint_url`` is passed: the profile carries the S3 proxy's endpoint,
    and it is that endpoint which makes the bucket the account name and the key
    start with the product. No checksum overrides either -- those exist for the
    UZH gateway, and the proxy was verified against a client with boto3's
    defaults.
    """
    session = boto3.Session(profile_name=target.profile)
    return session.client("s3")


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
        source_bucket, source_prefix = split_s3_uri(str(root))
        listing_root = root / subtree if subtree else root
        listed = list_objects_recursive(listing_root)
        client = target_client(target)
        published = list_published(client, target, subtree)
    except (BotoCoreError, ClientError, OSError, ValueError) as error:
        print(f"error  {error}", file=sys.stderr)
        return 1

    source = {f"{subtree}/{name}" if subtree else name: size for name, size in listed.items()}
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

    failed = transfer(
        source_client(),
        client,
        plan.upload,
        source_bucket=source_bucket,
        source_prefix=source_prefix,
        target=target,
        workers=args.workers,
    )
    if failed:
        print(f"\n{len(failed)} of {len(plan.upload)} object(s) failed:", file=sys.stderr)
        for name in failed:
            print(f"  {name}", file=sys.stderr)
        return 1
    print(f"\nuploaded {len(plan.upload)} object(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
