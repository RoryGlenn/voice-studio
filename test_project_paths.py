"""Check clean-checkout defaults and portable local configuration."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import project_paths


class PathChecks(unittest.TestCase):
    """Keep source, models, and private runtime data in distinct locations."""

    def test_python_preserves_virtual_environment_symlink(self) -> None:
        """Do not turn a configured venv interpreter into its base executable."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            executable = root / "venv/bin/python"
            executable.parent.mkdir(parents=True)
            executable.symlink_to(Path(sys.executable).resolve())
            config = root / "local.toml"
            config.write_text('python = "venv/bin/python"\n')
            with patch.dict(
                os.environ, {"VOICE_STUDIO_CONFIG": str(config)}, clear=True
            ):
                self.assertEqual(project_paths.python_executable(), executable)

    @unittest.skipUnless(sys.platform == "darwin", "macOS launcher")
    def test_macos_launcher_resolves_relative_python_from_config(self) -> None:
        """Let the Python resolver handle relative environment paths from Finder."""
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    str(project_paths.REPOSITORY / "Launch Voice Studio.command"),
                    "--help",
                ],
                cwd=temporary,
                env=dict(
                    os.environ,
                    PATH="/usr/bin:/bin:/usr/sbin:/sbin",
                    VOICE_STUDIO_CONFIG=str(project_paths.REPOSITORY / "local.toml"),
                    VOICE_STUDIO_PYTHON=".venv/bin/python",
                ),
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--data-dir", result.stdout)

    @unittest.skipUnless(sys.platform == "darwin", "macOS launcher")
    def test_macos_launcher_with_finder_path(self) -> None:
        """Bootstrap the configured runtime even with macOS system Python on PATH."""
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    str(project_paths.REPOSITORY / "Launch Voice Studio.command"),
                    "--help",
                ],
                cwd=temporary,
                env=dict(os.environ, PATH="/usr/bin:/bin:/usr/sbin:/sbin"),
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--data-dir", result.stdout)

    def test_relative_config_paths_and_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = root / "local.toml"
            config.write_text(
                'workspace_root = "media"\nstudio_data_dir = "voices"\npython = "runtime/bin/python"\n'
            )
            with patch.dict(
                os.environ, {"VOICE_STUDIO_CONFIG": str(config)}, clear=True
            ):
                self.assertEqual(project_paths.workspace_root(), root / "media")
                self.assertEqual(project_paths.studio_data_dir(), root / "voices")
                self.assertEqual(
                    project_paths.python_executable(), root / "runtime/bin/python"
                )
                with patch.dict(
                    os.environ, {"VOICE_STUDIO_DATA_ROOT": str(root / "override")}
                ):
                    self.assertEqual(project_paths.workspace_root(), root / "override")

    def test_missing_config_does_not_create_private_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.toml"
            with patch.dict(
                os.environ, {"VOICE_STUDIO_CONFIG": str(missing)}, clear=True
            ):
                self.assertEqual(
                    project_paths.workspace_root(), project_paths.REPOSITORY / "data"
                )
                self.assertEqual(
                    project_paths.studio_data_dir(),
                    project_paths.REPOSITORY / "data/studio",
                )
                self.assertFalse(missing.exists())

    def test_invalid_path_type_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "bad.toml"
            config.write_text("workspace_root = 12\n")
            with patch.dict(
                os.environ, {"VOICE_STUDIO_CONFIG": str(config)}, clear=True
            ):
                with self.assertRaisesRegex(ValueError, "nonempty path"):
                    project_paths.workspace_root()
