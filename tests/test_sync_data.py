#!/usr/bin/env python3
"""Gate on what publication would upload, before it uploads anything.

``tools/sync_data.py`` is the only thing between the private store and a public
bucket, and its two decisions -- which objects move, and what they are served as
-- are both made from listings rather than from bytes. That makes them checkable
without a network, and this is where they are checked: the change detection over
a synthetic pair of listings, the content types, the ``write_prefix`` split, the
refusal of a local store root, and the dry run, which is the last thing a person
reads before typing ``--confirm``.

Both S3 clients are stand-ins recording their calls. Nothing here opens a
socket.

Run: python3 tests/test_sync_data.py
"""

from __future__ import annotations

from contextlib import redirect_stderr
from io import BytesIO, StringIO
import os
from pathlib import Path
import sys
import tempfile
from typing import IO, TYPE_CHECKING, Any, override

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

# glace_catalog.config resolves its locations at import, so importing the tool
# needs the two variables set. Local values keep every path in this gate off
# object storage; nothing here reads or writes either location.
os.environ.setdefault("GLACE_SOURCE_ROOT", str(ROOT / "does-not-exist" / "source"))
os.environ.setdefault("GLACE_STORE_ROOT", str(ROOT / "does-not-exist" / "store"))

import sync_data  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

TARGET = sync_data.Target(bucket="lqgentner", prefix="glace-ch", profile="source-coop")

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
    "mosaics/pmtiles/2024/coh12_rgb.pmtiles": 2_097_152,
}

# What the target already holds: the root catalog unchanged, the collection
# document rewritten at a different size, and nothing else.
PUBLISHED: dict[str, int] = {
    "catalog.json": 1_024,
    "tiles/collection.json": 1_500,
}


class FakeSource:
    """A source client returning canned bodies and recording every read."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]:  # noqa: N803
        """Record the read and hand back a body of the recorded size."""
        self.reads.append((Bucket, Key))
        return {"Body": BytesIO(b"x" * 8)}


class FakePaginator:
    """A paginator honouring ``Prefix``, in two pages to exercise the loop."""

    def __init__(self, listing: Mapping[str, int]) -> None:
        self.listing = listing

    def paginate(self, **kwargs: Any) -> Iterator[Mapping[str, Any]]:
        """Yield the keys under the requested prefix, split across two pages."""
        prefix = str(kwargs["Prefix"])
        contents = [
            {"Key": key, "Size": size}
            for key, size in sorted(self.listing.items())
            if key.startswith(prefix)
        ]
        return iter([{"Contents": contents[:1]}, {"Contents": contents[1:]}])


class FakeTarget:
    """A target client with a fixed listing, recording every upload."""

    def __init__(self, listing: Mapping[str, int]) -> None:
        self.listing = {f"{TARGET.prefix}/{name}": size for name, size in listing.items()}
        self.uploads: list[tuple[str, str, str]] = []

    def get_paginator(self, operation_name: str, /) -> FakePaginator:
        """Paginate the listing the way ``list_objects_v2`` does."""
        del operation_name
        return FakePaginator(self.listing)

    def upload_fileobj(
        self,
        Fileobj: IO[bytes],  # noqa: N803
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
        ExtraArgs: Mapping[str, Any],  # noqa: N803
    ) -> None:
        """Record the upload, and read the body the way boto3 would."""
        Fileobj.read()
        self.uploads.append((Bucket, Key, str(ExtraArgs["ContentType"])))


def check_change_detection() -> dict[str, bool]:
    """Classification of a listing against what is already published."""
    plan = sync_data.plan_sync(SOURCE, PUBLISHED)
    forced = sync_data.plan_sync(SOURCE, PUBLISHED, force=True)
    return {
        "an object the target lacks is new": plan.new
        == (
            "mosaics/pmtiles/2024/coh12_rgb.pmtiles",
            "tiles/2024/32TMS24/coh12_vv.tif",
            "tiles/2024/32TMS24/item.json",
            "tiles/items.parquet",
        ),
        "a different size is changed": plan.changed == ("tiles/collection.json",),
        "an equal size is unchanged": plan.unchanged == ("catalog.json",),
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


def check_content_types() -> dict[str, bool]:
    """Check the suffix-to-content-type table, and what it refuses to guess."""
    cases = {
        "tiles/2024/32TMS24/item.json": "application/json",
        "README.md": "text/markdown",
        "tiles/items.parquet": "application/vnd.apache.parquet",
        "tiles/2024/32TMS24/coh12_vv.tif": (
            "image/tiff; application=geotiff; profile=cloud-optimized"
        ),
        "mosaics/pmtiles/2024/coh12_rgb.pmtiles": "application/vnd.pmtiles",
        "mosaics/thumbnail.png": "image/png",
        "_assets/glace-wordmark.svg": "image/svg+xml",
        "tiles/2024/config.yaml": "application/yaml",
        "tiles/2024/32TMS24/COH12_VV.TIF": (
            "image/tiff; application=geotiff; profile=cloud-optimized"
        ),
        "notes.txt": "application/octet-stream",
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
    """Check the ``write_prefix`` split and what the repository's own file states."""
    checks = {
        "a bucket and a key prefix": sync_data.split_s3_uri("s3://lqgentner/glace-ch")
        == ("lqgentner", "glace-ch"),
        "a trailing slash is not part of the prefix": sync_data.split_s3_uri(
            "s3://lqgentner/glace-ch/",
        )
        == ("lqgentner", "glace-ch"),
        "a nested prefix stays nested": sync_data.split_s3_uri("s3://b/one/two")
        == ("b", "one/two"),
        "a bare bucket has no prefix": sync_data.split_s3_uri("s3://b") == ("b", ""),
        "a key is the prefix and the relative path": TARGET.key("catalog.json")
        == "glace-ch/catalog.json",
    }
    for uri in ("https://data.source.coop/lqgentner", "s3://"):
        try:
            sync_data.split_s3_uri(uri)
        except ValueError:
            checks[f"{uri} is refused"] = True
        else:
            checks[f"{uri} is refused"] = False

    target = sync_data.read_target()
    checks["catalog.publish.yaml names the account bucket"] = target.bucket == "lqgentner"
    checks["catalog.publish.yaml names the product prefix"] = target.prefix == "glace-ch"
    checks["catalog.publish.yaml names the profile"] = target.profile == "source-coop"

    with tempfile.TemporaryDirectory() as directory:
        incomplete = Path(directory) / "catalog.publish.yaml"
        incomplete.write_text("public_base: https://example.invalid/x\n")
        try:
            sync_data.read_target(incomplete)
        except ValueError:
            checks["a configuration without write_prefix is refused"] = True
        else:
            checks["a configuration without write_prefix is refused"] = False
    return checks


def check_local_root() -> dict[str, bool]:
    """Check that a local store root is refused: there is no staging mode."""
    checks: dict[str, bool] = {}
    try:
        sync_data.bucket_root(Path("/mnt/glace/glace-ch"))
    except ValueError as error:
        checks["a local store root is refused"] = True
        checks["the refusal names the variable"] = "GLACE_STORE_ROOT" in str(error)
    else:
        checks["a local store root is refused"] = False
        checks["the refusal names the variable"] = False
    root = sync_data.bucket_root(sync_data.UPath("s3://s1-bursts/data/glace-ch"))
    checks["a bucket root is accepted"] = sync_data.split_s3_uri(str(root)) == (
        "s1-bursts",
        "data/glace-ch",
    )
    return checks


def check_listing_and_transfer() -> dict[str, bool]:
    """Check the remote listing and one transfer per object with its content type."""
    target_client = FakeTarget(PUBLISHED)
    listed = sync_data.list_published(target_client, TARGET)
    plan = sync_data.plan_sync(SOURCE, listed)

    source_client = FakeSource()
    failed = sync_data.transfer(
        source_client,
        target_client,
        plan.upload,
        source_bucket="s1-bursts",
        source_prefix="data/glace-ch",
        target=TARGET,
        workers=2,
    )
    uploaded = {key: content_type for _, key, content_type in target_client.uploads}
    return {
        "the listing comes back store-relative": listed == PUBLISHED,
        "a subtree lists only its own keys, still store-relative": sync_data.list_published(
            target_client,
            TARGET,
            "tiles",
        )
        == {"tiles/collection.json": 1_500},
        "every read is keyed under the source prefix": sorted(source_client.reads)
        == sorted(("s1-bursts", f"data/glace-ch/{name}") for name in plan.upload),
        "every upload is keyed under the target prefix": sorted(uploaded)
        == sorted(f"glace-ch/{name}" for name in plan.upload),
        "no transfer failed": not failed,
        "the raster is served as a COG": uploaded["glace-ch/tiles/2024/32TMS24/coh12_vv.tif"]
        == "image/tiff; application=geotiff; profile=cloud-optimized",
        "the document is served as JSON": uploaded["glace-ch/tiles/collection.json"]
        == "application/json",
    }


def check_failure_is_reported() -> dict[str, bool]:
    """One unreadable object is reported, not raised, and the rest still move."""

    class Broken(FakeSource):
        @override
        def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]:
            """Fail on the raster, serve everything else."""
            if Key.endswith(".tif"):
                msg = "AccessDenied"
                raise RuntimeError(msg)
            return super().get_object(Bucket=Bucket, Key=Key)

    target_client = FakeTarget(PUBLISHED)
    plan = sync_data.plan_sync(SOURCE, PUBLISHED)
    # The tool reports the failure on stderr as it happens; held here so the
    # gate's own output stays a list of what it checked.
    reported = StringIO()
    with redirect_stderr(reported):
        failed = sync_data.transfer(
            Broken(),
            target_client,
            plan.upload,
            source_bucket="s1-bursts",
            source_prefix="data/glace-ch",
            target=TARGET,
            workers=2,
        )
    return {
        "the failed object is named": failed == ["tiles/2024/32TMS24/coh12_vv.tif"],
        "the reason reaches stderr": "AccessDenied" in reported.getvalue(),
        "the others still uploaded": len(target_client.uploads) == len(plan.upload) - 1,
    }


DRY_RUN = """\
source:  s3://s1-bursts/data/glace-ch
target:  s3://lqgentner/glace-ch  (profile source-coop)

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
    plan = sync_data.plan_sync(SOURCE, PUBLISHED)
    rendered = "\n".join(
        sync_data.summary_lines(plan, "s3://s1-bursts/data/glace-ch", TARGET),
    )
    if rendered != DRY_RUN:
        print("--- rendered ---")
        print(rendered)
    return {
        "the dry-run summary reads as recorded": rendered == DRY_RUN,
        "byte counts are binary": sync_data.human_bytes(1536) == "1.5 KiB",
        "small counts stay in bytes": sync_data.human_bytes(512) == "512 B",
    }


def main() -> int:
    """Run every check, printing one line per failure."""
    checks: dict[str, bool] = {}
    for group in (
        check_change_detection,
        check_content_types,
        check_target_config,
        check_local_root,
        check_listing_and_transfer,
        check_failure_is_reported,
        check_summary,
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
