"""Unit tests for frozen executable command resolution, brand identifiers, and icon generation."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from config.settings import (
    APP_USER_MODEL_ID,
    RUN_REG_VALUE_NAME,
    SHOW_EVENT_NAME,
    SINGLETON_MUTEX_NAME,
    WATCHER_MUTEX_NAME,
)
from scripts.icon_builder import build_ico
from src.core.watcher import BUILD_ENTRY, resolve_launch_command, resolve_startup_command


class TestPackagingAndFrozen(unittest.TestCase):
    def test_brand_identifiers_use_gravitypulse(self) -> None:
        self.assertEqual(APP_USER_MODEL_ID, "GravityPulse.Desktop.App")
        self.assertEqual(SINGLETON_MUTEX_NAME, "Local\\GravityPulseAppSingleton")
        self.assertEqual(WATCHER_MUTEX_NAME, "Local\\GravityPulseWatcherSingleton")
        self.assertEqual(SHOW_EVENT_NAME, "Local\\GravityPulseShowEvent")
        self.assertEqual(RUN_REG_VALUE_NAME, "GravityPulseWatcher")

    def test_resolve_launch_and_startup_commands_for_source_and_frozen_modes(self) -> None:
        src_cmd = resolve_launch_command("--background", frozen=False)
        self.assertEqual(src_cmd[1], str(BUILD_ENTRY))
        self.assertEqual(src_cmd[2], "--background")

        frozen_cmd = resolve_launch_command("--background", frozen=True)
        self.assertEqual(frozen_cmd, [str(Path(sys.executable).resolve()), "--background"])

        frozen_ui_cmd = resolve_launch_command(None, frozen=True)
        self.assertEqual(frozen_ui_cmd, [str(Path(sys.executable).resolve())])

        src_reg = resolve_startup_command(frozen=False)
        self.assertIn(str(BUILD_ENTRY), src_reg)
        self.assertTrue(src_reg.endswith("--watch"))

        frozen_reg = resolve_startup_command(frozen=True)
        self.assertEqual(frozen_reg, f'"{Path(sys.executable).resolve()}" --watch')

    def test_build_ico_creates_valid_multi_size_icon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = build_ico(Path(tmp) / "gravitypulse.ico")
            self.assertTrue(out.exists())
            raw = out.read_bytes()
            self.assertEqual(raw[:4], b"\x00\x00\x01\x00")
            self.assertGreater(len(raw), 1024)


if __name__ == "__main__":
    unittest.main()
