"""Where the store's inputs are read from and where its bytes are written to.

Three locations, deliberately separate, because they have different owners and
different failure modes:

- **source** — the composite archive a run produced, on the private UZH bucket.
  Read-only here; this repository never writes into it. It is named per run, as
  ``publish-year.py --src``, because one year's archive is one prefix and a
  store may draw on more than one.
- **store** — where the published tree is assembled: a prefix on the private UZH
  bucket or a local directory. The store *is* what gets published, object for
  object, and ``tools/sync_data.py`` streams it from either.
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
from typing import TYPE_CHECKING

from freezebase.settings import S3Settings
from freezebase.utils import get_cache_dir
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from upath import UPath

type AnyDir = Path | UPath


class StoreSettings(BaseSettings):
    """Locations from ``GLACE_*`` environment variables, held as raw strings.

    ``store_root`` is where the published tree is assembled, normally an
    ``s3://`` prefix on the private bucket. A local path resolves the same way
    and is written the same way; only publication insists on the bucket.
    """

    model_config = SettingsConfigDict(env_prefix="GLACE_", extra="ignore")

    store_root: str
    """Root the published tree is assembled under. ``s3://...`` or a local path."""


class Settings:
    """Resolved locations for one build.

    Directory values resolve to :class:`~pathlib.Path` when local and to
    :class:`~upath.UPath` carrying the S3 profile and endpoint when they start
    with ``s3://``, so downstream code does not branch on the storage backend.

    The S3 side is :class:`freezebase.settings.S3Settings` under the
    ``GLACE_S3_`` prefix: authentication is by named profile only, so no access
    key passes through the environment; ``GLACE_S3_ENDPOINT_URL`` is optional
    when the profile's config section carries the endpoint; and the two
    checksum policies default to ``when_required``, which the UZH Ceph gateway
    needs.
    """

    def __init__(self) -> None:
        self.s3 = S3Settings.from_env("GLACE_S3_")
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
            If ``raw`` is an ``s3://`` URI and ``GLACE_S3_PROFILE`` is unset.
        """
        return self.s3.resolve_path(raw)

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
