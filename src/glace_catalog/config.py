"""Where the store's inputs are read from and where its bytes are written to.

Three locations, deliberately separate, because they have different owners and
different failure modes:

- **source** — the composite archive a run produced, on the private UZH bucket.
  Read-only here; this repository never writes into it.
- **store** — where the published tree is assembled: a prefix on the private UZH
  bucket. The store *is* what gets published, object for object, so a local
  ``GLACE_STORE_ROOT`` is a build that cannot be published —
  ``tools/sync_data.py`` refuses one. A local path still resolves here, which is
  what tests and throwaway builds use.
- **publish target** — Source Cooperative, which this module does *not* know
  about. It lives in ``catalog.publish.yaml`` and is read by
  ``tools/sync_data.py`` alone, so a credential for the public bucket can never
  be picked up by a build step that only meant to read a composite.

Configuration is read from the environment only, so every command needs
``--env-file .env``. Fields without a default are required at import time: a
build that cannot say where its data is should fail before it opens a file, not
half-way through a year.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from freezebase.s3 import make_s3_upath
from freezebase.utils import get_cache_dir
from pydantic_settings import BaseSettings, SettingsConfigDict
from upath import UPath

type AnyDir = Path | UPath


class S3Settings(BaseSettings):
    """S3 profile and botocore tuning from ``GLACIERS_S3_*`` environment variables.

    Authentication is by named AWS profile only: ``GLACIERS_S3_PROFILE`` selects
    a section of ``~/.aws/credentials`` (or of ``AWS_SHARED_CREDENTIALS_FILE``),
    so no access key ever passes through the environment. Selecting a profile
    explicitly also drops boto's environment-variable provider from the
    credential chain, so a stray ``AWS_ACCESS_KEY_ID`` in a shell cannot sign
    these requests. The endpoint is not read from the AWS config file and must
    stay in ``GLACIERS_S3_ENDPOINT_URL``.
    """

    model_config = SettingsConfigDict(env_prefix="GLACIERS_S3_", extra="ignore")

    endpoint_url: str | None = None
    profile: str | None = None

    # boto3 >=1.36 defaults to "when_supported", which adds x-amz-checksum-*
    # trailers that the UZH Ceph gateway rejects. Typed as the two values
    # botocore accepts, so a typo in the environment is refused here rather
    # than by a signing failure half-way through a copy, and so the values can
    # be handed to `botocore.config.Config` without a cast.
    request_checksum_calculation: Literal["when_required", "when_supported"] = "when_required"
    response_checksum_validation: Literal["when_required", "when_supported"] = "when_required"


class StoreSettings(BaseSettings):
    """Locations from ``GLACE_*`` environment variables, held as raw strings.

    ``store_root`` is where the published tree is assembled, normally an
    ``s3://`` prefix on the private bucket. A local path resolves the same way
    and is written the same way; only publication insists on the bucket.
    """

    model_config = SettingsConfigDict(env_prefix="GLACE_", extra="ignore")

    source_root: str
    """Prefix holding the composite archives, one directory per year."""

    store_root: str
    """Root the published tree is assembled under. ``s3://...`` or a local path."""


class Settings:
    """Resolved locations for one build.

    Directory values resolve to :class:`~pathlib.Path` when local and to
    :class:`~upath.UPath` carrying the S3 profile and endpoint when they start
    with ``s3://``, so downstream code does not branch on the storage backend.
    """

    def __init__(self) -> None:
        self.s3 = S3Settings()
        self.locations = StoreSettings()

    def resolve_path(self, raw: str) -> AnyDir:
        """Resolve a local path or an ``s3://`` URI with the configured credentials.

        Parameters
        ----------
        raw
            A filesystem path, possibly ``~``-prefixed, or an ``s3://`` URI.

        Returns
        -------
        Path or UPath
            The resolved location.

        Raises
        ------
        ValueError
            If ``raw`` is an ``s3://`` URI and the S3 endpoint or profile is unset.
        """
        if not raw.startswith("s3://"):
            return Path(raw).expanduser()
        if not (self.s3.endpoint_url and self.s3.profile):
            msg = "An s3:// path needs GLACIERS_S3_ENDPOINT_URL and GLACIERS_S3_PROFILE."
            raise ValueError(msg)
        return make_s3_upath(
            raw,
            profile=self.s3.profile,
            endpoint_url=self.s3.endpoint_url,
            request_checksum_calculation=self.s3.request_checksum_calculation,
            response_checksum_validation=self.s3.response_checksum_validation,
        )

    @property
    def source_root(self) -> AnyDir:
        """Prefix holding the composite archives this store publishes."""
        return self.resolve_path(self.locations.source_root)

    @property
    def store_root(self) -> AnyDir:
        """Root the published tree is assembled under."""
        return self.resolve_path(self.locations.store_root)

    @property
    def cache_dir(self) -> Path:
        """Scratch directory for the VRT chain and other build artefacts.

        Honours ``FREEZEBASE_CACHE``, falling back to the platform cache
        directory. Nothing here is published; a chain is rebuilt in seconds.
        """
        return get_cache_dir()


settings = Settings()
