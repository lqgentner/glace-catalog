"""Where a build reads its inputs and writes its outputs.

Three locations are kept separate on purpose:

- **source**: the composite archive of an upstream run, on the private UZH
  bucket. It is only ever read, and is passed per run as
  ``publish-year.py --src``.
- **store**: where the published tree is built, an ``s3://`` prefix or a local
  directory (``GLACE_STORE_ROOT``). ``tools/sync_data.py`` uploads it as is.
- **publish target**: Source Cooperative, configured in ``catalog.publish.yaml``
  and read only by ``tools/sync_data.py``. Only the upload therefore holds
  credentials for the public bucket.

Settings come from the environment only, so run commands with
``--env-file .env``. Required settings are checked at import time, so a
misconfigured build fails before it touches any data.
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
    """Locations from ``GLACE_*`` environment variables, as raw strings."""

    model_config = SettingsConfigDict(env_prefix="GLACE_", extra="ignore")

    store_root: str
    """Root the published tree is assembled under. ``s3://...`` or a local path."""


class Settings:
    """Resolved locations for one build.

    Directory values resolve to :class:`~pathlib.Path` when local and to
    :class:`~upath.UPath` carrying the S3 profile and endpoint when they start
    with ``s3://``, so downstream code treats both backends alike.

    S3 access uses :class:`freezebase.settings.S3Settings` with the
    ``GLACE_S3_`` prefix. It authenticates with a named AWS profile only.
    ``GLACE_S3_ENDPOINT_URL`` may be left
    unset when the profile defines the endpoint. Both checksum policies default
    to ``when_required``, which the UZH Ceph gateway needs.
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
        """Scratch directory for the VRT chain and other unpublished build files.

        Honours ``FREEZEBASE_CACHE``, falling back to the platform cache
        directory.
        """
        return get_cache_dir()


settings = Settings()
