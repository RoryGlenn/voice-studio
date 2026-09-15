"""Resolve private runtime locations independently of the source checkout."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent


def setting_path(
    key: str, environment: str, default: Path, *, preserve_symlinks: bool = False
) -> Path:
    """Resolve an environment or local TOML path against its configuration file.

    Parameters
    ----------
    key, environment : str
        Local configuration key and overriding environment variable.
    default : Path
        Location used when neither setting is present.
    preserve_symlinks : bool
        Retain a virtual environment interpreter's symlink instead of selecting
        the base Python executable and losing that environment's packages.

    Returns
    -------
    pathlib.Path
        Expanded absolute path without creating directories or reading media.
    """
    config_path = (
        Path(os.environ.get("VOICE_STUDIO_CONFIG", REPOSITORY / "local.toml"))
        .expanduser()
        .resolve()
    )
    config = tomllib.loads(config_path.read_text()) if config_path.is_file() else {}
    value = os.environ.get(environment, config.get(key))
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{key} must be a nonempty path string")
    path = Path(value).expanduser() if value is not None else default
    absolute = path if path.is_absolute() else config_path.parent / path
    return Path(os.path.abspath(absolute)) if preserve_symlinks else absolute.resolve()


def workspace_root() -> Path:
    """Return the external root containing models and audiobook job data."""
    return setting_path("workspace_root", "VOICE_STUDIO_DATA_ROOT", REPOSITORY / "data")


def studio_data_dir() -> Path:
    """Return the profile, references, state, and render directory."""
    return setting_path(
        "studio_data_dir", "VOICE_STUDIO_PROFILE_DIR", workspace_root() / "studio"
    )


def python_executable() -> Path:
    """Return the configured existing runtime or the checkout's uv environment."""
    return setting_path(
        "python",
        "VOICE_STUDIO_PYTHON",
        REPOSITORY / ".venv/bin/python",
        preserve_symlinks=True,
    )
