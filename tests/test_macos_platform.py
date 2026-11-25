"""Unit tests for macOS platform adaptation: ICNS, LaunchAgent, process match, locale, scroll, IPC."""
from __future__ import annotations

from pathlib import Path
import plistlib
import struct
import tempfile
import tkinter as tk
import unittest
from unittest import mock

from config.settings import INK, IS_MACOS, IS_WINDOWS, MACOS_LAUNCH_AGENT_LABEL
from scripts.icon_builder import _create_macos_desktop_shortcut, _find_macos_app_bundle, build_icns
from src.core.platform_ipc import SingletonIPC, is_named_lock_held
from src.core.updater_pkg import open_macos_dmg
from src.core.watcher import (
    build_macos_launch_agent_plist,
    install_macos_launch_agent,
    is_antigravity_process_output,
    uninstall_macos_launch_agent,
)
from src.ui.desktop_app import GravityPulseApp
from src.ui.hero_view import compute_mousewheel_units
from src.ui.i18n import parse_macos_apple_languages
from src.ui.macos_tray import render_macos_menubar_template_png
from src.ui.primitives import FlatDialogButton, make_dialog_button


class TestMacOSPlatform(unittest.TestCase):
    def test_build_icns_creates_valid_multi_res_apple_icon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = build_icns(Path(tmp) / "gravitypulse.icns")
            self.assertTrue(out.exists())
            raw = out.read_bytes()
            self.assertEqual(raw[:4], b"icns")
            total_len = struct.unpack(">I", raw[4:8])[0]
            self.assertEqual(total_len, len(raw))
            tags = set()
            pos = 8
            while pos + 8 <= len(raw):
                tag = raw[pos : pos + 4]
                chunk_len = struct.unpack(">I", raw[pos + 4 : pos + 8])[0]
                self.assertEqual(raw[pos + 8 : pos + 16], b"\x89PNG\r\n\x1a\n")
                tags.add(tag)
                pos += chunk_len
            self.assertEqual(tags, {b"icp4", b"icp5", b"icp6", b"ic07", b"ic08", b"ic09"})

    def test_macos_menubar_template_png(self) -> None:
        png = render_macos_menubar_template_png(36)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertGreater(len(png), 200)

    def test_macos_launch_agent_plist_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plist_path = Path(tmp) / "LaunchAgents" / f"{MACOS_LAUNCH_AGENT_LABEL}.plist"
            argv = ["/Applications/GravityPulse.app/Contents/MacOS/GravityPulse", "--watch"]
            written = install_macos_launch_agent(plist_path=plist_path, argv=argv, load_agent=False)
            self.assertEqual(written, plist_path)
            self.assertTrue(plist_path.is_file())
            with plist_path.open("rb") as fh:
                parsed = plistlib.load(fh)
            self.assertEqual(parsed["Label"], MACOS_LAUNCH_AGENT_LABEL)
            self.assertEqual(parsed["ProgramArguments"], argv)
            self.assertTrue(parsed["RunAtLoad"])
            self.assertEqual(parsed["ProcessType"], "Background")

            uninstall_macos_launch_agent(plist_path=plist_path, unload_agent=False)
            self.assertFalse(plist_path.exists())

        payload = build_macos_launch_agent_plist(argv=["/usr/bin/python3", "build.py", "--watch"], cwd="/tmp")
        self.assertEqual(payload["WorkingDirectory"], "/tmp")

    def test_macos_antigravity_process_output_parser(self) -> None:
        sample_none = "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder\n/Applications/GravityPulse.app/Contents/MacOS/GravityPulse\n"
        self.assertFalse(is_antigravity_process_output(sample_none))

        sample_app = "/Applications/Antigravity.app/Contents/MacOS/Antigravity\n/usr/libexec/logd\n"
        self.assertTrue(is_antigravity_process_output(sample_app))

        sample_helper = "/Applications/Antigravity.app/Contents/Frameworks/Antigravity Helper.app/Contents/MacOS/Antigravity Helper\n"
        self.assertFalse(is_antigravity_process_output(sample_helper))

    def test_macos_apple_languages_and_mousewheel_helpers(self) -> None:
        self.assertEqual(parse_macos_apple_languages('(\n    "zh-Hans-CN",\n    "en-US"\n)\n'), "zh")
        self.assertEqual(parse_macos_apple_languages('(\n    "en-US",\n    "zh-Hans-CN"\n)\n'), "en")
        self.assertEqual(parse_macos_apple_languages('(\n    "ja-JP"\n)\n'), "en")

        self.assertEqual(compute_mousewheel_units(120, is_macos=False), -1)
        self.assertEqual(compute_mousewheel_units(-120, is_macos=False), 1)
        self.assertEqual(compute_mousewheel_units(1, is_macos=True), -1)
        self.assertEqual(compute_mousewheel_units(-3, is_macos=True), 3)
        self.assertEqual(compute_mousewheel_units(0, is_macos=True), 0)

    def test_macos_bundle_path_detection_and_command_shortcut(self) -> None:
        exe = Path("/Applications/GravityPulse.app/Contents/MacOS/GravityPulse")
        found = _find_macos_app_bundle(exe)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "GravityPulse.app")
        self.assertIsNone(_find_macos_app_bundle(Path("/usr/local/bin/python3")))

        if IS_MACOS:
            with tempfile.TemporaryDirectory() as tmp:
                sc = _create_macos_desktop_shortcut(desktop_dir=Path(tmp))
                self.assertTrue(sc.exists())

    def test_posix_singleton_ipc_lock_and_wakeup(self) -> None:
        if IS_WINDOWS:
            return
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ipc1 = SingletonIPC("test_app", with_show_signal=True, lock_dir=tmp_path, sock_dir=tmp_path)
            ipc2 = SingletonIPC("test_app", with_show_signal=True, lock_dir=tmp_path, sock_dir=tmp_path)
            try:
                self.assertTrue(ipc1.acquire(signal_if_running=False))
                self.assertTrue(is_named_lock_held("test_app", lock_dir=tmp_path))
                self.assertFalse(ipc1.poll_show_signal())
                self.assertFalse(ipc2.acquire(signal_if_running=True))
                self.assertTrue(ipc1.poll_show_signal())
                self.assertFalse(ipc1.poll_show_signal())
            finally:
                ipc1.release()
            self.assertFalse(is_named_lock_held("test_app", lock_dir=tmp_path))

    def test_macos_dialog_buttons_avoid_aqua_white_on_white_and_preserve_windows_button(self) -> None:
        def _walk_widgets(widget: tk.Misc) -> list[tk.Misc]:
            out: list[tk.Misc] = [widget]
            for child in widget.winfo_children():
                out.extend(_walk_widgets(child))
            return out

        root = tk.Tk()
        root.withdraw()
        try:
            clicks: list[str] = []
            mac_btn = make_dialog_button(
                root, text="Save", command=lambda: clicks.append("mac"),
                bg=INK, fg="#ffffff", padx=16, pady=5, font=("Helvetica", 9, "bold"), use_flat=True,
            )
            self.assertIsInstance(mac_btn, FlatDialogButton)
            self.assertEqual(mac_btn.cget("bg"), INK)
            self.assertEqual(mac_btn.cget("fg"), "#ffffff")
            self.assertEqual(mac_btn.cget("state"), "normal")
            mac_btn.invoke()  # type: ignore[attr-defined]
            self.assertEqual(clicks, ["mac"])

            mac_btn.configure(state="disabled", text="Busy")
            self.assertEqual(mac_btn.cget("state"), "disabled")
            self.assertEqual(mac_btn.cget("text"), "Busy")
            mac_btn.invoke()  # type: ignore[attr-defined]
            self.assertEqual(clicks, ["mac"])

            win_btn = make_dialog_button(
                root, text="Save", command=lambda: clicks.append("win"),
                bg=INK, fg="#ffffff", padx=16, pady=5, font=("Helvetica", 9, "bold"), use_flat=False,
            )
            self.assertIsInstance(win_btn, tk.Button)
            self.assertEqual(win_btn.cget("bg"), INK)
            self.assertEqual(win_btn.cget("fg"), "#ffffff")

            with (
                mock.patch("src.ui.desktop_app.SystemTrayIcon.start", return_value=None),
                mock.patch("src.ui.desktop_app.SystemTrayIcon.stop", return_value=None),
                mock.patch.object(GravityPulseApp, "refresh", return_value=None),
            ):
                app = GravityPulseApp(root, start_hidden=True, language="en")
                try:
                    with mock.patch("src.ui.primitives.IS_MACOS", True):
                        app._open_calibration_dialog()
                        assert app._calib_win is not None
                        mac_widgets = _walk_widgets(app._calib_win)
                        self.assertFalse(any(isinstance(w, tk.Button) for w in mac_widgets))
                        self.assertTrue(
                            any(
                                isinstance(w, FlatDialogButton) and w.cget("bg") == INK and w.cget("fg") == "#ffffff"
                                for w in mac_widgets
                            )
                        )
                        app._calib_win.destroy()
                        app._calib_win = None

                    with mock.patch("src.ui.primitives.IS_MACOS", False):
                        app._open_calibration_dialog()
                        assert app._calib_win is not None
                        win_widgets = _walk_widgets(app._calib_win)
                        self.assertTrue(
                            any(
                                isinstance(w, tk.Button) and w.cget("bg") == INK and w.cget("fg") == "#ffffff"
                                for w in win_widgets
                            )
                        )
                        app._calib_win.destroy()
                        app._calib_win = None
                finally:
                    app._quit_completely()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_open_macos_dmg_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dmg = Path(tmp) / "GravityPulse-v1.3.0.dmg"
            dmg.write_bytes(b"DMG")
            with mock.patch("src.core.updater_pkg.sys.platform", "win32"):
                self.assertFalse(open_macos_dmg(dmg))
            with (
                mock.patch("src.core.updater_pkg.sys.platform", "darwin"),
                mock.patch("src.core.updater_pkg.subprocess.Popen") as popen_mock,
            ):
                self.assertTrue(open_macos_dmg(dmg))
                popen_mock.assert_called_once_with(["open", str(dmg)])
                self.assertFalse(open_macos_dmg(Path(tmp) / "missing.dmg"))


if __name__ == "__main__":
    unittest.main()
