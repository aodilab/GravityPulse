"""Platform package helpers for GravityPulse updater (ZIP extraction, Win64 swap script, macOS DMG)."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """Safely extract `zip_path` into `dest_dir`, blocking Zip-Slip traversal paths."""
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            posix_p = PurePosixPath(info.filename)
            if posix_p.is_absolute() or ".." in posix_p.parts or info.filename.startswith("\\"):
                raise ValueError("Unsafe zip path")
            target = (dest_resolved / Path(*posix_p.parts)).resolve()
            if dest_resolved != target and dest_resolved not in target.parents:
                raise ValueError("Unsafe zip path")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src_f, target.open("wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)


def locate_source_root(extract_dir: Path) -> Path | None:
    """Locate the GravityPulse source root containing `build.py` and `config/settings.py`."""
    if (extract_dir / "build.py").is_file() and (extract_dir / "config" / "settings.py").is_file():
        return extract_dir
    subdirs = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(subdirs) == 1 and (subdirs[0] / "build.py").is_file() and (subdirs[0] / "config" / "settings.py").is_file():
        return subdirs[0]
    return None


def locate_win_bundle_root(extract_dir: Path) -> Path | None:
    """Locate the PyInstaller Windows bundle folder containing `GravityPulse.exe`."""
    if (extract_dir / "GravityPulse.exe").is_file():
        return extract_dir
    subdirs = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(subdirs) == 1 and (subdirs[0] / "GravityPulse.exe").is_file():
        return subdirs[0]
    return None


def build_win_frozen_swap_script(
    stage_root: Path,
    install_dir: Path,
    parent_pid: int,
    stage_parent: Path,
    relaunch_args: list[str] | None = None,
) -> str:
    """Build a PowerShell helper that waits for exit, preserves `data/`, swaps files, and relaunches."""
    def _ps_q(val: str) -> str:
        return "'" + val.replace("'", "''") + "'"

    exe_path = install_dir / "GravityPulse.exe"
    arg_list = ", ".join(_ps_q(a) for a in (relaunch_args or []))
    launch_line = (
        f"Start-Process -FilePath $exe -ArgumentList @({arg_list}) -WorkingDirectory $dst"
        if arg_list
        else "Start-Process -FilePath $exe -WorkingDirectory $dst"
    )
    return "\n".join([
        "$ErrorActionPreference = 'SilentlyContinue'",
        f"$waitPid = {int(parent_pid)}",
        f"$src = {_ps_q(str(stage_root.resolve()))}",
        f"$dst = {_ps_q(str(install_dir.resolve()))}",
        f"$exe = {_ps_q(str(exe_path.resolve()))}",
        f"$stageParent = {_ps_q(str(stage_parent.resolve()))}",
        "for ($i = 0; $i -lt 120; $i++) {",
        "  if (-not (Get-Process -Id $waitPid -ErrorAction SilentlyContinue)) { break }",
        "  Start-Sleep -Milliseconds 250",
        "}",
        "$restartWatcher = $false",
        "$sameExe = Get-CimInstance Win32_Process -Filter \"Name='GravityPulse.exe'\" | Where-Object { $_.ExecutablePath -and ($_.ExecutablePath.ToLower() -eq $exe.ToLower()) }",
        "foreach ($p in $sameExe) {",
        "  if ($p.CommandLine -and $p.CommandLine.ToLower().Contains('--watch')) { $restartWatcher = $true }",
        "  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue",
        "}",
        "Start-Sleep -Milliseconds 400",
        "$null = & robocopy $src $dst /MIR /XD data scratch .git /R:6 /W:1 /NFL /NDL /NJH /NJS /NP",
        "if ($LASTEXITCODE -lt 8 -and (Test-Path $exe)) {",
        f"  {launch_line}",
        "  if ($restartWatcher) { Start-Sleep -Milliseconds 1500; Start-Process -FilePath $exe -ArgumentList @('--watch') -WorkingDirectory $dst }",
        "}",
        "Start-Sleep -Milliseconds 500",
        "Remove-Item -LiteralPath $stageParent -Recurse -Force -ErrorAction SilentlyContinue",
        "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue",
    ]) + "\n"


def stage_win_frozen_zip_update(
    zip_path: Path,
    install_dir: Path,
    version: str = "",
    parent_pid: int | None = None,
    lang: str = "zh",
    launch_helper: bool = True,
    relaunch_args: list[str] | None = None,
) -> tuple[bool, str, str]:
    """Validate `GravityPulse-win64.zip`, stage files preserving `data/`, and launch detached swapper."""
    stage_parent = Path(tempfile.mkdtemp(prefix="gravitypulse_winupd_"))
    extract_dir = stage_parent / "pkg"
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        safe_extract_zip(zip_path, extract_dir)
    except (zipfile.BadZipFile, ValueError, OSError):
        shutil.rmtree(stage_parent, ignore_errors=True)
        msg = "Windows 更新包校验失败，请重试或手动下载。" if lang == "zh" else "Windows update archive verification failed."
        return False, msg, ""

    bundle_root = locate_win_bundle_root(extract_dir)
    if bundle_root is None:
        shutil.rmtree(stage_parent, ignore_errors=True)
        msg = "更新包缺少 GravityPulse.exe，已中止更新。" if lang == "zh" else "GravityPulse.exe missing in archive; aborted."
        return False, msg, ""

    for protected in ("data", "scratch"):
        p_dir = bundle_root / protected
        if p_dir.exists():
            shutil.rmtree(p_dir, ignore_errors=True)

    script_path = Path(tempfile.gettempdir()) / f"gravitypulse_apply_{int(time.time() * 1000)}.ps1"
    script_text = build_win_frozen_swap_script(
        bundle_root,
        install_dir,
        parent_pid if parent_pid is not None else os.getpid(),
        stage_parent,
        relaunch_args=relaunch_args,
    )
    script_path.write_text(script_text, encoding="utf-8-sig")

    if launch_helper:
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        except OSError:
            shutil.rmtree(stage_parent, ignore_errors=True)
            script_path.unlink(missing_ok=True)
            msg = "无法启动自动替换脚本，请打开发布页手动下载。" if lang == "zh" else "Failed to launch update helper."
            return False, msg, ""

    ver_s = f" v{version}" if version else ""
    ok_msg = (
        f"已就绪{ver_s}，正在退出并安全替换重启…"
        if lang == "zh"
        else f"Ready{ver_s}. Exiting to swap files and restart…"
    )
    return True, ok_msg, str(script_path)


def open_macos_dmg(dmg_path: Path | str) -> bool:
    """Open a local `.dmg` image via macOS `open`, returning True when launched."""
    path = Path(dmg_path)
    if sys.platform != "darwin" or not path.is_file():
        return False
    try:
        subprocess.Popen(["open", str(path)])
        return True
    except OSError:
        return False


def save_and_open_macos_dmg(
    dmg_bytes: bytes,
    version: str = "",
    downloads_dir: Path | None = None,
    lang: str = "zh",
    open_dmg: bool = True,
) -> tuple[bool, str, str]:
    """Save `.dmg` into `~/Downloads` atomically and open it so the user can drag into Applications."""
    if not dmg_bytes:
        msg = "DMG 安装包内容为空，请重试。" if lang == "zh" else "Downloaded DMG is empty."
        return False, msg, ""
    target_dir = downloads_dir or (Path.home() / "Downloads")
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_ver = re.sub(r"[^0-9A-Za-z._-]", "", version or "latest")
    dmg_path = target_dir / f"GravityPulse-v{safe_ver}.dmg"
    tmp_path = dmg_path.with_name(dmg_path.name + ".part")
    try:
        tmp_path.write_bytes(dmg_bytes)
        os.replace(tmp_path, dmg_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        msg = "保存 DMG 文件失败，请打开发布页手动下载。" if lang == "zh" else "Failed to save DMG to Downloads."
        return False, msg, ""

    if open_dmg:
        open_macos_dmg(dmg_path)

    ok_msg = (
        f"已下载并打开 {dmg_path.name}，请将 GravityPulse.app 拖入「应用程序」替换（用户数据自动保留）。"
        if lang == "zh"
        else f"Downloaded and opened {dmg_path.name}. Drag GravityPulse.app to Applications (your data is preserved)."
    )
    return True, ok_msg, str(dmg_path)
