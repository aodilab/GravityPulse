"""Unified root entry point for GravityPulse desktop app, companion watcher, setup, and audit."""
from __future__ import annotations

import os
import sys

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def main() -> int:
    raw_args = sys.argv[1:]
    arg_set = {a.lower() for a in raw_args}

    if "--help" in arg_set or "-h" in arg_set:
        print(
            "Usage: python build.py [option]\n"
            "  (no args)            Launch or focus the GravityPulse desktop window\n"
            "  --background         Start resident tray/menu-bar app hidden in background\n"
            "  --watch              Run singleton Antigravity 2.0 companion watcher\n"
            "  --setup              Generate icons, Desktop shortcut, and auto-start entry\n"
            "  --icon               Regenerate assets/gravitypulse.ico and .icns\n"
            "  --shortcut           Create/update Desktop shortcut\n"
            "  --install-startup    Register companion watcher auto-start (HKCU Run / LaunchAgent)\n"
            "  --uninstall-startup  Remove companion watcher auto-start entry\n"
            "  --update             Check GitHub Releases and apply latest version safely\n"
            "  --test               Run unit test suite\n"
            "  --audit              Run line-limit check, syntax check, and unit tests"
        )
        return 0

    if "--watch" in arg_set:
        from src.core.watcher import run_watcher_loop

        run_watcher_loop()
        return 0

    if "--audit" in arg_set:
        from scripts.audit_project import run_full_audit

        return run_full_audit()

    if "--test" in arg_set:
        from scripts.audit_project import run_unit_tests

        return 0 if run_unit_tests(verbosity=2) else 1

    if "--icon" in arg_set:
        from scripts.icon_builder import build_icns, build_ico

        ico = build_ico()
        icns = build_icns()
        print(f"Generated icons:\n  {ico}\n  {icns}")
        return 0

    if "--shortcut" in arg_set:
        from scripts.icon_builder import create_desktop_shortcut

        lnk = create_desktop_shortcut()
        print(f"Updated shortcut: {lnk}")
        return 0

    if "--install-startup" in arg_set:
        from src.core.watcher import install_startup_registry

        cmd = install_startup_registry()
        print(f"Installed startup entry: {cmd}")
        return 0

    if "--uninstall-startup" in arg_set:
        from src.core.watcher import uninstall_startup_registry

        uninstall_startup_registry()
        print("Removed startup entry.")
        return 0

    if "--update" in arg_set:
        from config.settings import APP_VERSION
        from src.core.updater import apply_update, check_for_update

        chk = check_for_update(current_version=APP_VERSION, lang="en")
        if chk.status == "error":
            print(chk.error or "Update check failed.")
            return 1
        if chk.status == "up_to_date" or chk.release is None:
            print(f"Already up to date (v{chk.current_version}).")
            return 0
        print(f"New release found: v{chk.release.version} (current v{chk.current_version})")
        res = apply_update(chk.release, status_cb=print, lang="en")
        print(res.message)
        return 0 if res.ok else 1

    if "--setup" in arg_set:
        from config.settings import IS_MACOS
        from scripts.icon_builder import build_icns, build_ico, create_desktop_shortcut
        from src.core.watcher import install_startup_registry

        icon = build_icns() if IS_MACOS else build_ico()
        lnk = create_desktop_shortcut()
        reg_cmd = install_startup_registry()
        print(f"Icon:     {icon}\nShortcut: {lnk}\nStartup:  {reg_cmd}")
        return 0

    from src.ui.desktop_app import launch_desktop_app

    launch_desktop_app(arg_set)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
