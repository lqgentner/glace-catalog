"""GDAL/`osgeo`-based S3 helpers, kept out of the core library.

``freezebase`` reads and writes rasters on S3 through rasterio alone (see
:func:`freezebase.s3.s3_env`), which covers every current need. This module is a
self-contained fallback for the rare case that raw ``osgeo.gdal`` I/O is needed:
:func:`to_gdal_uri` builds the ``/vsis3/`` URI GDAL expects, and
:func:`gdal_s3_env` mirrors the S3 credentials into both the rasterio and the
system-GDAL environments so ``gdal.*`` calls see them too.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING

from freezebase.s3 import (
    GDAL_HTTP_MAX_RETRY,
    GDAL_HTTP_RETRY_CODES,
    GDAL_HTTP_RETRY_DELAY_S,
    aws_session,
)
import rasterio
import rasterio.env
from upath import UPath

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

type AnyPath = str | Path | UPath


def to_gdal_uri(path: AnyPath) -> str:
    """Convert a path to a GDAL-readable URI, using ``/vsis3/`` for S3.

    Parameters
    ----------
    path : str | Path | UPath
        Local path or S3 ``UPath``.

    Returns
    -------
    str
        The local path unchanged, or a ``/vsis3/bucket/key`` URI for S3.

    Raises
    ------
    ValueError
        If ``path`` uses a protocol other than local (``""``) or ``"s3"``.
    """
    path = UPath(path)
    match path.protocol:
        case "":
            return str(path)
        case "s3":
            return f"/vsis3/{path.path}"
        case p:
            msg = f"Unsupported protocol '{p}'."
            raise ValueError(msg)


def subprocess_s3_env(path: AnyPath) -> dict[str, str]:
    """GDAL S3 settings for a child process, as environment variables.

    A subprocess gets none of :func:`gdal_s3_env`, which configures this process
    only. GDAL's ``/vsis3`` in the child honours ``AWS_PROFILE`` against
    ``~/.aws/credentials``, so the child needs the profile name and the endpoint
    and never an access key -- which keeps the project rule that no key passes
    through the environment.

    Parameters
    ----------
    path : str | Path | UPath
        An S3 ``UPath`` whose storage options carry the profile and endpoint.
        A local path yields an empty mapping.

    Returns
    -------
    dict[str, str]
        Variables to merge into the child's environment.
    """
    path = UPath(path)
    if path.protocol != "s3":
        return {}
    options = path.storage_options
    env: dict[str, str] = {}
    if profile := options.get("profile"):
        env["AWS_PROFILE"] = str(profile)
    if endpoint_url := options.get("endpoint_url"):
        env["AWS_S3_ENDPOINT"] = str(endpoint_url).removeprefix("https://").removeprefix("http://")
        env["AWS_VIRTUAL_HOSTING"] = "FALSE"
        if str(endpoint_url).startswith("http://"):
            env["AWS_HTTPS"] = "NO"
    return env


@contextmanager
def gdal_s3_env(path: UPath) -> Generator[None]:
    """Set up rasterio and system-GDAL environments for S3-compatible storage.

    Extracts credentials and endpoint configuration from the fsspec storage
    options attached to ``path`` and applies them to rasterio for the duration
    of the ``with`` block. When the optional ``osgeo`` bindings are installed,
    the same configuration is mirrored into the system GDAL so that raw
    ``gdal.*`` I/O paths (via :func:`to_gdal_uri`) see it too; rasterio-only
    consumers work without ``osgeo``.

    Parameters
    ----------
    path : UPath
        A UPath with ``protocol="s3"`` whose ``storage_options`` carry either
        fsspec/s3fs credentials (``key``, ``secret``, ``token``), a named
        ``profile``, or ``anon=True``, plus an optional
        ``client_kwargs["region_name"]`` and an optional custom ``endpoint_url``.
    """
    try:
        from osgeo import gdal  # noqa: PLC0415
    except ImportError:
        gdal = None

    non_aws_options: dict[str, str] = {
        # /vsis3/ has no native random-write support.
        "CPL_VSIL_USE_TEMP_FILE_FOR_RANDOM_WRITE": "YES",
        # Retry transient HTTP failures on the /vsis3 read/write path.
        "GDAL_HTTP_MAX_RETRY": str(GDAL_HTTP_MAX_RETRY),
        "GDAL_HTTP_RETRY_DELAY": str(GDAL_HTTP_RETRY_DELAY_S),
        "GDAL_HTTP_RETRY_CODES": GDAL_HTTP_RETRY_CODES,
        # Don't probe for sidecar files on open.
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    }
    so = path.storage_options
    # Cached upstream, so a named profile is resolved once per process rather
    # than on every call -- and signed identically to `freezebase.s3.s3_env`.
    session = aws_session(path)
    # rasterio freezes the session's credentials here (or reports "sign nothing"
    # for an anonymous path). Mirror that result into the system-GDAL config,
    # which only understands AWS_* options, so raw gdal.* calls sign exactly
    # like rasterio does. Re-read per call: refreshable credentials rotate.
    aws_options: dict[str, str] = dict(session.get_credential_options())

    if endpoint_url := so.get("endpoint_url"):
        non_aws_options["AWS_S3_ENDPOINT"] = endpoint_url.removeprefix("https://").removeprefix(
            "http://",
        )
        non_aws_options["AWS_VIRTUAL_HOSTING"] = "FALSE"
        if endpoint_url.startswith("http://"):
            non_aws_options["AWS_HTTPS"] = "NO"

    rio_env = rasterio.env.Env(session=session, **non_aws_options)
    gdal_env = (
        gdal.config_options({**non_aws_options, **aws_options})
        if gdal is not None
        else nullcontext()
    )
    with rio_env, gdal_env:
        yield
