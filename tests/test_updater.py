"""Unit tests for cross-platform GitHub Releases updater (Win64 frozen, macOS DMG, Git, Source ZIP, i18n)."""
from __future__ import annotations

from datetime import datetime
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import tkinter as tk
import unittest
from unittest import mock
from urllib import error as url_error
import zipfile

from config.settings import INK, LOCAL_TZ
from src.core.models import QuotaConfig
from src.core.quota import load_quota_config, save_quota_config
from src.core.updater import (
    ReleaseInfo,
    UpdateApplyResult,
    UpdateCheckResult,
    apply_macos_dmg_update,
    apply_update,
    apply_zip_archive,
    check_for_update,
    inspect_git_worktree,
    is_newer_version,
    pick_download_url,
    stage_win_frozen_zip_update,
)
from src.ui.desktop_app import GravityPulseApp
from src.ui.modals import build_calibrated_quota_config
from src.ui.primitives import FlatDialogButton


def _make_zip_bytes(files: dict[str, bytes | str], root_prefix: str = "GravityPulse-1.3.0") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel, content in files.items():
            arcname = f"{root_prefix}/{rel}" if root_prefix else rel
            data = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(arcname, data)
    return buf.getvalue()


class TestUpdaterCore(unittest.TestCase):
    def test_semver_comparison_handles_v_prefix_multidigit_and_prerelease(self) -> None:
        self.assertTrue(is_newer_version("1.2.0", "v1.3.0"))
        self.assertTrue(is_newer_version("1.2.0", "v1.10.0"))
        self.assertTrue(is_newer_version("1.3.0-rc1", "v1.3.0"))
        self.assertFalse(is_newer_version("1.3.0", "v1.3.0"))
        self.assertFalse(is_newer_version("1.3.0", "v1.2.9"))
        self.assertFalse(is_newer_version("1.3.0", "v1.3.0-rc1"))

    def test_check_for_update_platform_assets_and_bilingual_errors(self) -> None:
        payload = {
            "tag_name": "v1.4.0",
            "name": "v1.4.0",
            "body": "Release notes",
            "html_url": "https://github.com/aodilab/GravityPulse/releases/tag/v1.4.0",
            "assets": [
                {"name": "GravityPulse-win64.zip", "browser_download_url": "https://example.invalid/GravityPulse-win64.zip"},
                {"name": "GravityPulse-macos.dmg", "browser_download_url": "https://example.invalid/GravityPulse-macos.dmg"},
            ],
        }
        fetcher = lambda _u, _v, _t: json.dumps(payload).encode("utf-8")

        res_src = check_for_update(current_version="1.3.0", fetcher=fetcher, is_frozen=False, platform="win32")
        self.assertEqual(res_src.status, "available")
        assert res_src.release is not None
        self.assertTrue(res_src.release.download_url.endswith("/archive/refs/tags/v1.4.0.zip"))
        self.assertEqual(res_src.release.win_zip_url, "https://example.invalid/GravityPulse-win64.zip")
        self.assertEqual(res_src.release.macos_dmg_url, "https://example.invalid/GravityPulse-macos.dmg")

        res_win = check_for_update(current_version="1.3.0", fetcher=fetcher, is_frozen=True, platform="win32")
        assert res_win.release is not None
        self.assertEqual(res_win.release.download_url, "https://example.invalid/GravityPulse-win64.zip")

        res_mac = check_for_update(current_version="1.3.0", fetcher=fetcher, is_frozen=True, platform="darwin")
        assert res_mac.release is not None
        self.assertEqual(res_mac.release.download_url, "https://example.invalid/GravityPulse-macos.dmg")

        self.assertEqual(
            pick_download_url("https://s", "https://w", "https://m", is_frozen=True, platform="win32"),
            "https://w",
        )
        self.assertEqual(
            pick_download_url("https://s", "https://w", "https://m", is_frozen=True, platform="darwin"),
            "https://m",
        )
        self.assertEqual(
            pick_download_url("https://s", "https://w", "https://m", is_frozen=False, platform="darwin"),
            "https://s",
        )

        res_same = check_for_update(current_version="1.4.0", fetcher=fetcher)
        self.assertEqual(res_same.status, "up_to_date")

        def raise_404(_u: str, _v: str, _t: float) -> bytes:
            raise url_error.HTTPError(_u, 404, "Not Found", {}, None)  # type: ignore[arg-type]

        self.assertEqual(check_for_update(current_version="1.3.0", fetcher=raise_404).status, "up_to_date")

        def raise_403(_u: str, _v: str, _t: float) -> bytes:
            raise url_error.HTTPError(_u, 403, "Forbidden", {}, None)  # type: ignore[arg-type]

        err_zh = check_for_update(current_version="1.3.0", fetcher=raise_403, lang="zh")
        err_en = check_for_update(current_version="1.3.0", fetcher=raise_403, lang="en")
        self.assertEqual(err_zh.status, "error")
        self.assertIn("受限", err_zh.error)
        self.assertIn("rate limit", err_en.error)

    def test_win_frozen_zip_staging_preserves_data_and_rejects_bad_archives(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            install_dir = Path(td) / "GravityPulse"
            install_dir.mkdir(parents=True)
            (install_dir / "data").mkdir()
            (install_dir / "data" / "quota_config.json").write_text('{"keep": true}\n', encoding="utf-8")

            win_zip = _make_zip_bytes(
                {
                    "GravityPulse.exe": b"MZ-new-exe",
                    "_internal/app.bin": b"payload",
                    "data/quota_config.json": '{"overwrite": true}\n',
                    "scratch/temp.txt": "remove-me",
                },
                root_prefix="GravityPulse",
            )
            rel = ReleaseInfo(
                tag="v1.4.0",
                version="1.4.0",
                title="v1.4.0",
                notes="",
                html_url="https://example.invalid/release",
                download_url="https://example.invalid/GravityPulse-win64.zip",
                win_zip_url="https://example.invalid/GravityPulse-win64.zip",
            )
            statuses: list[str] = []
            res = apply_update(
                rel,
                current_version="1.3.0",
                status_cb=statuses.append,
                downloader=lambda _u, _v, _t: win_zip,
                lang="en",
                is_frozen=True,
                platform="win32",
                install_dir=install_dir,
                launch_helper=False,
            )
            self.assertTrue(res.ok, res.message)
            self.assertEqual(res.method, "win_frozen")
            self.assertTrue(res.needs_quit)
            self.assertIn("Ready v1.4.0", res.message)
            self.assertTrue(len(statuses) >= 2)

            script_path = Path(res.downloaded_path)
            self.assertTrue(script_path.is_file())
            script_text = script_path.read_text(encoding="utf-8-sig")
            try:
                self.assertIn("/MIR /XD data scratch .git", script_text)
                self.assertIn("GravityPulse.exe", script_text)
                self.assertIn("--watch", script_text)
                # Locate staged folder referenced in script and confirm protected dirs were stripped
                for line in script_text.splitlines():
                    if line.startswith("$stageParent = '"):
                        stage_parent = Path(line.split("'", 2)[1])
                        self.assertFalse((stage_parent / "pkg" / "GravityPulse" / "data").exists())
                        self.assertFalse((stage_parent / "pkg" / "GravityPulse" / "scratch").exists())
                        self.assertTrue((stage_parent / "pkg" / "GravityPulse" / "GravityPulse.exe").is_file())
                        shutil.rmtree(stage_parent, ignore_errors=True)
                        break
            finally:
                script_path.unlink(missing_ok=True)

            # Missing GravityPulse.exe must fail safely
            bad_win = Path(td) / "no_exe.zip"
            bad_win.write_bytes(_make_zip_bytes({"README.md": "missing exe"}, root_prefix="GravityPulse"))
            ok_bad, msg_bad, _ = stage_win_frozen_zip_update(bad_win, install_dir, lang="en", launch_helper=False)
            self.assertFalse(ok_bad)
            self.assertIn("GravityPulse.exe", msg_bad)

            # Spawning powershell.exe must NOT pass DETACHED_PROCESS (0x8), which aborts ConsoleHost before -File runs
            good_win = Path(td) / "good.zip"
            good_win.write_bytes(win_zip)
            with mock.patch("src.core.updater_pkg.subprocess.Popen") as popen_mock:
                ok_sp, _msg_sp, sp_script = stage_win_frozen_zip_update(good_win, install_dir, lang="en", launch_helper=True)
            self.assertTrue(ok_sp)
            popen_mock.assert_called_once()
            flags = int(popen_mock.call_args.kwargs.get("creationflags", 0))
            self.assertTrue(flags & 0x08000000, "CREATE_NO_WINDOW must be set")
            self.assertFalse(flags & 0x00000008, "DETACHED_PROCESS breaks powershell.exe -File execution")
            sp_path = Path(sp_script)
            if sp_path.is_file():
                for ln in sp_path.read_text(encoding="utf-8-sig").splitlines():
                    if ln.startswith("$stageParent = '"):
                        shutil.rmtree(Path(ln.split("'", 2)[1]), ignore_errors=True)
                        break
                sp_path.unlink(missing_ok=True)

            # Zip-Slip traversal must be blocked
            slip_win = Path(td) / "slip.zip"
            slip_win.write_bytes(_make_zip_bytes({"../evil.exe": b"bad"}, root_prefix=""))
            ok_slip, _msg_slip, _ = stage_win_frozen_zip_update(slip_win, install_dir, lang="zh", launch_helper=False)
            self.assertFalse(ok_slip)

    def test_macos_dmg_update_saves_atomically_with_bilingual_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dl_dir = Path(td) / "Downloads"
            rel = ReleaseInfo(
                tag="v1.4.0",
                version="1.4.0",
                title="v1.4.0",
                notes="",
                html_url="https://example.invalid/release",
                download_url="https://example.invalid/GravityPulse-macos.dmg",
                macos_dmg_url="https://example.invalid/GravityPulse-macos.dmg",
            )
            res_zh = apply_update(
                rel,
                current_version="1.3.0",
                downloader=lambda _u, _v, _t: b"FAKE-DMG-CONTENT",
                lang="zh",
                is_frozen=True,
                platform="darwin",
                downloads_dir=dl_dir,
                open_dmg=False,
            )
            self.assertTrue(res_zh.ok, res_zh.message)
            self.assertEqual(res_zh.method, "macos_dmg")
            self.assertFalse(res_zh.needs_quit)
            self.assertIn("GravityPulse-v1.4.0.dmg", res_zh.message)
            self.assertIn("应用程序", res_zh.message)
            saved_dmg = Path(res_zh.downloaded_path)
            self.assertEqual(saved_dmg.read_bytes(), b"FAKE-DMG-CONTENT")

            res_en = apply_macos_dmg_update(b"DMG2", version="1.4.1", downloads_dir=dl_dir, lang="en", open_dmg=False)
            self.assertTrue(res_en.ok)
            self.assertIn("Drag GravityPulse.app to Applications", res_en.message)

            res_empty = apply_macos_dmg_update(b"", version="1.4.1", downloads_dir=dl_dir, lang="en", open_dmg=False)
            self.assertFalse(res_empty.ok)
            self.assertIn("empty", res_empty.message.lower())

    def test_apply_source_zip_preserves_data_cleans_stale_py_and_blocks_zip_slip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "app"
            (root / "config").mkdir(parents=True)
            (root / "src" / "core").mkdir(parents=True)
            (root / "data").mkdir(parents=True)
            (root / "scratch").mkdir(parents=True)

            (root / "build.py").write_text("# old build\n", encoding="utf-8")
            (root / "config" / "settings.py").write_text('APP_VERSION = "1.2.0"\n', encoding="utf-8")
            (root / "src" / "core" / "obsolete.py").write_text("# remove me\n", encoding="utf-8")
            (root / "data" / "quota_config.json").write_text('{"keep": true}\n', encoding="utf-8")
            (root / "scratch" / "note.txt").write_text("local-note\n", encoding="utf-8")

            good_zip_bytes = _make_zip_bytes({
                "build.py": "# new build\n",
                "config/settings.py": 'APP_VERSION = "1.3.0"\n',
                "src/core/fresh.py": "# fresh\n",
                "data/quota_config.json": '{"overwrite": true}\n',
            })
            rel = ReleaseInfo(
                tag="v1.3.0",
                version="1.3.0",
                title="v1.3.0",
                notes="",
                html_url="https://example.invalid/release",
                download_url="https://example.invalid/release.zip",
                source_zip_url="https://example.invalid/release.zip",
            )
            statuses: list[str] = []
            res = apply_update(
                rel,
                project_root=root,
                current_version="1.2.0",
                status_cb=statuses.append,
                downloader=lambda _u, _v, _t: good_zip_bytes,
                is_frozen=False,
            )
            self.assertTrue(res.ok, res.message)
            self.assertEqual(res.method, "zip")
            self.assertTrue(res.needs_restart)
            self.assertIn('APP_VERSION = "1.3.0"', (root / "config" / "settings.py").read_text(encoding="utf-8"))
            self.assertTrue((root / "src" / "core" / "fresh.py").is_file())
            self.assertFalse((root / "src" / "core" / "obsolete.py").exists())
            self.assertEqual((root / "data" / "quota_config.json").read_text(encoding="utf-8"), '{"keep": true}\n')
            self.assertEqual((root / "scratch" / "note.txt").read_text(encoding="utf-8"), "local-note\n")
            self.assertTrue(len(statuses) >= 2)

            bad_zip_path = Path(td) / "slip.zip"
            bad_zip_path.write_bytes(_make_zip_bytes({"../../escape.txt": "bad"}, root_prefix=""))
            self.assertFalse(apply_zip_archive(bad_zip_path, root, version="1.3.1").ok)

            incomplete_zip_path = Path(td) / "incomplete.zip"
            incomplete_zip_path.write_bytes(_make_zip_bytes({"README.md": "only readme"}))
            self.assertFalse(apply_zip_archive(incomplete_zip_path, root, version="1.3.1").ok)

    def test_git_worktree_blocks_dirty_bilingual_and_fast_forwards_clean_repo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            origin = base / "origin.git"
            work = base / "work"
            subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
            subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(work), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(work), "config", "user.name", "Tester"], check=True)
            (work / "config").mkdir(parents=True, exist_ok=True)
            (work / "build.py").write_text("# v1\n", encoding="utf-8")
            (work / "config" / "settings.py").write_text('APP_VERSION = "1.2.0"\n', encoding="utf-8")
            subprocess.run(["git", "-C", str(work), "add", "."], check=True)
            subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(work), "push", "-u", "origin", "HEAD"], check=True, capture_output=True)

            upstream = base / "upstream"
            subprocess.run(["git", "clone", str(origin), str(upstream)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(upstream), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(upstream), "config", "user.name", "Tester"], check=True)
            (upstream / "config" / "settings.py").write_text('APP_VERSION = "1.3.0"\n', encoding="utf-8")
            subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
            subprocess.run(["git", "-C", str(upstream), "commit", "-m", "v1.3.0"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(upstream), "tag", "v1.3.0"], check=True)
            subprocess.run(["git", "-C", str(upstream), "push", "origin", "HEAD", "--tags"], check=True, capture_output=True)

            rel = ReleaseInfo(
                tag="v1.3.0", version="1.3.0", title="v1.3.0", notes="",
                html_url="https://example.invalid", download_url="https://example.invalid/v1.3.0.zip",
            )

            (work / "build.py").write_text("# dirty local edit\n", encoding="utf-8")
            self.assertEqual(inspect_git_worktree(work), (True, True, True))
            blocked_zh = apply_update(rel, project_root=work, lang="zh", is_frozen=False)
            blocked_en = apply_update(rel, project_root=work, lang="en", is_frozen=False)
            self.assertFalse(blocked_zh.ok)
            self.assertIn("未提交", blocked_zh.message)
            self.assertFalse(blocked_en.ok)
            self.assertIn("Uncommitted", blocked_en.message)

            subprocess.run(["git", "-C", str(work), "checkout", "--", "build.py"], check=True)
            self.assertEqual(inspect_git_worktree(work), (True, False, True))
            ok_res = apply_update(rel, project_root=work, lang="en", is_frozen=False)
            self.assertTrue(ok_res.ok, ok_res.message)
            self.assertEqual(ok_res.method, "git")
            self.assertIn('APP_VERSION = "1.3.0"', (work / "config" / "settings.py").read_text(encoding="utf-8"))

    def test_auto_check_update_config_and_bilingual_ui_modal_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "quota_config.json"
            cfg = QuotaConfig(auto_check_update=False)
            save_quota_config(cfg, cfg_path)
            loaded = load_quota_config(cfg_path)
            self.assertFalse(loaded.auto_check_update)

            now_loc = datetime(2026, 11, 25, 19, 30, 0, tzinfo=LOCAL_TZ)
            updated = build_calibrated_quota_config(
                loaded, now_bj=now_loc, used_pct=20.0, mult=3.5, notify_switch=True,
                reset_cycle=True, auto_check_update=True,
            )
            self.assertTrue(updated.auto_check_update)

        for lang, expected_title in (("zh", "软件更新"), ("en", "Software Update")):
            root = tk.Tk()
            root.withdraw()
            with mock.patch("src.ui.desktop_app.SystemTrayIcon.start", return_value=None), mock.patch(
                "src.ui.desktop_app.SystemTrayIcon.stop", return_value=None
            ), mock.patch.object(GravityPulseApp, "refresh", return_value=None):
                app = GravityPulseApp(root, start_hidden=True, language=lang)
                try:
                    self.assertFalse(app._update_btn_packed)
                    rel = ReleaseInfo(
                        tag="v1.9.0", version="1.9.0", title="v1.9.0", notes="Test release notes",
                        html_url="https://example.invalid/r", download_url="https://example.invalid/r.zip",
                    )
                    app._on_update_check_done(
                        UpdateCheckResult(status="available", current_version="1.3.0", release=rel), manual=False
                    )
                    self.assertTrue(app._update_btn_packed)
                    self.assertEqual(app._available_release, rel)

                    app._window_visible = True
                    app._open_update_dialog(force_check=False)
                    self.assertIsNotNone(app._update_win)
                    assert app._update_win is not None
                    self.assertTrue(app._update_win.winfo_exists())
                    self.assertEqual(app._update_win.title(), expected_title)

                    app._on_close_to_tray()
                    self.assertIsNone(app._update_win)

                    app._on_update_check_done(
                        UpdateCheckResult(status="up_to_date", current_version="1.9.0"), manual=False
                    )
                    self.assertFalse(app._update_btn_packed)
                finally:
                    app._quit_completely()

    def test_macos_dmg_finish_updates_apply_button_and_supports_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dmg_file = Path(td) / "GravityPulse-v1.4.0.dmg"
            dmg_file.write_bytes(b"DMG-DATA")
            rel = ReleaseInfo(
                tag="v1.4.0", version="1.4.0", title="v1.4.0", notes="Notes",
                html_url="https://example.invalid/r", download_url="https://example.invalid/r.dmg",
                macos_dmg_url="https://example.invalid/r.dmg",
            )
            cases = (
                ("zh", "下载并打开 DMG", "已打开 DMG", "关闭"),
                ("en", "Download & Open DMG", "DMG Opened", "Close"),
            )
            for lang, init_lbl, opened_lbl, close_lbl in cases:
                root = tk.Tk()
                root.withdraw()
                apply_res = UpdateApplyResult(
                    ok=True, method="macos_dmg", version="1.4.0",
                    message="DMG ready", needs_restart=False, needs_quit=False,
                    downloaded_path=str(dmg_file),
                )
                class _SyncThread:
                    def __init__(self, *args, target=None, **_kwargs):
                        self._target = target

                    def start(self) -> None:
                        if self._target is not None:
                            self._target()

                with (
                    mock.patch("src.ui.desktop_app.SystemTrayIcon.start", return_value=None),
                    mock.patch("src.ui.desktop_app.SystemTrayIcon.stop", return_value=None),
                    mock.patch.object(GravityPulseApp, "refresh", return_value=None),
                    mock.patch("src.ui.updater_ui.IS_FROZEN", True),
                    mock.patch("src.ui.updater_ui.IS_MACOS", True),
                    mock.patch("src.ui.primitives.IS_MACOS", True),
                    mock.patch("src.ui.updater_ui.apply_update", return_value=apply_res) as apply_mock,
                    mock.patch("src.ui.updater_ui.open_macos_dmg", return_value=True) as reopen_mock,
                ):
                    app = GravityPulseApp(root, start_hidden=True, language=lang)
                    try:
                        app._available_release = rel
                        app._window_visible = True
                        app._open_update_dialog(force_check=False)
                        btn = app._update_apply_btn
                        later = app._update_later_btn
                        assert btn is not None and later is not None
                        self.assertIsInstance(btn, FlatDialogButton)
                        self.assertEqual(btn.cget("text"), init_lbl)
                        self.assertEqual(btn.cget("bg"), INK)
                        self.assertEqual(btn.cget("fg"), "#ffffff")

                        with mock.patch("src.ui.updater_ui.threading.Thread", _SyncThread):
                            btn.invoke()  # type: ignore[attr-defined]
                        root.update()

                        self.assertFalse(app._update_applying)
                        self.assertEqual(btn.cget("text"), opened_lbl)
                        self.assertEqual(btn.cget("state"), "normal")
                        self.assertEqual(btn.cget("bg"), INK)
                        self.assertEqual(later.cget("state"), "normal")
                        self.assertEqual(later.cget("text"), close_lbl)
                        self.assertEqual(apply_mock.call_count, 1)

                        # Clicking again reopens the local DMG without re-downloading
                        btn.invoke()  # type: ignore[attr-defined]
                        reopen_mock.assert_called_once_with(str(dmg_file))
                        self.assertEqual(apply_mock.call_count, 1)
                    finally:
                        app._quit_completely()


if __name__ == "__main__":
    unittest.main()
