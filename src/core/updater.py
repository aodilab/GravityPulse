"""Cross-platform GitHub Releases version check and safe updater (Git, Source ZIP, Win64 ZIP, macOS DMG)."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable
from urllib import error as url_error
from urllib import request as url_request
import zipfile

from config.settings import (
    APP_VERSION,
    GITHUB_LATEST_RELEASE_API,
    GITHUB_REPO,
    IS_FROZEN,
    PROJECT_ROOT,
    RUNTIME_ROOT,
    UPDATE_HTTP_TIMEOUT_SEC,
)
from src.core.updater_pkg import (
    build_win_frozen_swap_script,
    locate_source_root,
    safe_extract_zip,
    save_and_open_macos_dmg,
    stage_win_frozen_zip_update,
)

_CREATE_NO_WINDOW = 0x08000000
_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
_SEMVER_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$")
_PROTECTED_ROOT_NAMES = frozenset({
    "data",
    "scratch",
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".idea",
    ".vscode",
    ".pytest_cache",
})
_MANAGED_PY_DIRS = frozenset({"config", "src", "scripts", "tests"})


@dataclass(frozen=True)
class ReleaseInfo:
    """Metadata of a published GitHub Release with per-platform package URLs."""

    tag: str
    version: str
    title: str
    notes: str
    html_url: str
    download_url: str
    published_at: str = ""
    source_zip_url: str = ""
    win_zip_url: str = ""
    macos_dmg_url: str = ""


@dataclass(frozen=True)
class UpdateCheckResult:
    """Result of checking GitHub Releases against the running `APP_VERSION`."""

    status: str  # "available" | "up_to_date" | "error"
    current_version: str
    release: ReleaseInfo | None = None
    error: str = ""


@dataclass(frozen=True)
class UpdateApplyResult:
    """Outcome of applying an update onto the running source tree or frozen bundle."""

    ok: bool
    method: str  # "git" | "zip" | "win_frozen" | "macos_dmg" | ""
    message: str
    version: str = ""
    needs_restart: bool = False
    needs_quit: bool = False
    downloaded_path: str = ""


def _msg(lang: str, zh: str, en: str) -> str:
    return zh if lang == "zh" else en


def normalize_version(raw: str) -> str:
    """Strip leading 'v'/'V' and whitespace from a release tag."""
    return raw.strip().lstrip("vV")


def parse_version_key(raw: str) -> tuple[tuple[int, ...], int, str]:
    """Return a comparable key `((major, minor, patch, ...), is_final, prerelease)`."""
    m = _SEMVER_RE.match(raw.strip())
    if not m:
        return ((0, 0, 0), 0, raw.strip())
    nums = [int(p) for p in m.group(1).split(".")]
    while len(nums) < 3:
        nums.append(0)
    prerelease = m.group(2) or ""
    return (tuple(nums), 0 if prerelease else 1, prerelease)


def is_newer_version(current: str, candidate: str) -> bool:
    """Return True iff `candidate` represents a strictly newer version than `current`."""
    cur_nums, cur_fin, cur_pre = parse_version_key(current)
    cand_nums, cand_fin, cand_pre = parse_version_key(candidate)
    max_len = max(len(cur_nums), len(cand_nums))
    c_pad = cur_nums + (0,) * (max_len - len(cur_nums))
    n_pad = cand_nums + (0,) * (max_len - len(cand_nums))
    if n_pad != c_pad:
        return n_pad > c_pad
    if cand_fin != cur_fin:
        return cand_fin > cur_fin
    return cand_pre > cur_pre


def _extract_asset_urls(payload: dict, tag: str, repo: str = GITHUB_REPO) -> tuple[str, str, str]:
    """Return `(source_zip_url, win_zip_url, macos_dmg_url)` from a GitHub Release JSON payload."""
    win_zip = fallback_zip = macos_dmg = ""
    assets = payload.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").lower()
            dl = str(asset.get("browser_download_url") or "").strip()
            if not dl:
                continue
            if name.endswith(".dmg") and not macos_dmg:
                macos_dmg = dl
            elif name.endswith(".zip"):
                if "win" in name and not win_zip:
                    win_zip = dl
                elif not fallback_zip:
                    fallback_zip = dl
    if not win_zip:
        win_zip = fallback_zip
    source_zip = (
        f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
        if tag
        else str(payload.get("zipball_url") or "").strip()
    )
    return source_zip, win_zip, macos_dmg


def pick_download_url(
    source_zip: str, win_zip: str, macos_dmg: str, is_frozen: bool = IS_FROZEN, platform: str = sys.platform,
) -> str:
    """Select the exact artifact URL matching the current runtime mode (frozen Win64/macOS vs source)."""
    if is_frozen and platform == "win32":
        return win_zip
    if is_frozen and platform == "darwin":
        return macos_dmg
    return source_zip


def parse_release_payload(
    payload: dict, repo: str = GITHUB_REPO, is_frozen: bool = IS_FROZEN, platform: str = sys.platform,
) -> ReleaseInfo | None:
    """Parse a GitHub `/releases/latest` JSON object into `ReleaseInfo`."""
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        return None
    version = normalize_version(tag)
    title = str(payload.get("name") or tag).strip()
    notes = str(payload.get("body") or "").strip()
    html_url = str(payload.get("html_url") or f"https://github.com/{repo}/releases/tag/{tag}").strip()
    source_zip, win_zip, macos_dmg = _extract_asset_urls(payload, tag, repo=repo)
    download_url = pick_download_url(source_zip, win_zip, macos_dmg, is_frozen=is_frozen, platform=platform)
    return ReleaseInfo(
        tag=tag, version=version, title=title, notes=notes, html_url=html_url, download_url=download_url,
        published_at=str(payload.get("published_at") or "").strip(),
        source_zip_url=source_zip, win_zip_url=win_zip, macos_dmg_url=macos_dmg,
    )


def _http_get_bytes(url: str, current_version: str, timeout: float, max_bytes: int = _MAX_DOWNLOAD_BYTES) -> bytes:
    req = url_request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"GravityPulse/{current_version}"},
    )
    with url_request.urlopen(req, timeout=timeout) as resp:
        chunks: list[bytes] = []
        total = 0
        while True:
            buf = resp.read(65536)
            if not buf:
                break
            total += len(buf)
            if total > max_bytes:
                raise ValueError("Package exceeds size limit")
            chunks.append(buf)
        return b"".join(chunks)


def check_for_update(
    current_version: str = APP_VERSION,
    api_url: str = GITHUB_LATEST_RELEASE_API,
    repo: str = GITHUB_REPO,
    timeout: float = UPDATE_HTTP_TIMEOUT_SEC,
    fetcher: Callable[[str, str, float], bytes] | None = None,
    lang: str = "zh",
    is_frozen: bool = IS_FROZEN,
    platform: str = sys.platform,
) -> UpdateCheckResult:
    """Check GitHub Releases for a newer version without modifying local files."""
    get_bytes = fetcher or (lambda u, v, t: _http_get_bytes(u, v, t, max_bytes=2 * 1024 * 1024))
    try:
        raw = get_bytes(api_url, current_version, timeout)
    except url_error.HTTPError as exc:
        if exc.code == 404:
            return UpdateCheckResult(status="up_to_date", current_version=current_version)
        if exc.code in (403, 429):
            return UpdateCheckResult(
                status="error", current_version=current_version,
                error=_msg(lang, "GitHub 请求受限，请稍后重试。", "GitHub rate limit reached. Try again later."),
            )
        return UpdateCheckResult(
            status="error", current_version=current_version,
            error=_msg(lang, f"检查更新失败（HTTP {exc.code}）。", f"Update check failed (HTTP {exc.code})."),
        )
    except Exception:
        return UpdateCheckResult(
            status="error", current_version=current_version,
            error=_msg(lang, "无法连接 GitHub，请稍后重试。", "Unable to reach GitHub. Try again later."),
        )

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return UpdateCheckResult(
            status="error", current_version=current_version,
            error=_msg(lang, "更新信息格式异常，请稍后重试。", "Invalid release metadata received."),
        )
    if not isinstance(payload, dict):
        return UpdateCheckResult(status="up_to_date", current_version=current_version)

    rel = parse_release_payload(payload, repo=repo, is_frozen=is_frozen, platform=platform)
    if rel is None:
        return UpdateCheckResult(status="up_to_date", current_version=current_version)
    if is_newer_version(current_version, rel.tag):
        return UpdateCheckResult(status="available", current_version=current_version, release=rel)
    return UpdateCheckResult(status="up_to_date", current_version=current_version, release=rel)


def _run_git(project_root: Path, args: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    kwargs: dict[str, object] = {
        "cwd": str(project_root), "capture_output": True, "text": True,
        "encoding": "utf-8", "errors": "replace", "timeout": timeout,
    }
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    proc = subprocess.run(["git", *args], **kwargs)  # type: ignore[arg-type]
    return proc.returncode, proc.stdout, proc.stderr


def inspect_git_worktree(project_root: Path = PROJECT_ROOT) -> tuple[bool, bool, bool]:
    """Return `(is_git_repo, is_dirty, git_available)` for `project_root`."""
    if not (project_root / ".git").exists():
        return False, False, False
    try:
        rc, out, _err = _run_git(project_root, ["status", "--porcelain"], timeout=8.0)
    except (OSError, subprocess.SubprocessError):
        return True, True, False
    if rc != 0:
        return True, True, False
    return True, bool(out.strip()), True


def _apply_git_update(
    release: ReleaseInfo, project_root: Path, status_cb: Callable[[str], None] | None, lang: str,
) -> UpdateApplyResult:
    if status_cb:
        status_cb(_msg(lang, "正在拉取新版本代码…", "Fetching latest release…"))
    try:
        rc, _out, _err = _run_git(project_root, ["fetch", "origin", "--tags"], timeout=45.0)
        if rc != 0:
            return UpdateApplyResult(ok=False, method="git", message=_msg(lang, "拉取远程仓库失败，请检查网络或打开发布页下载。", "Failed to fetch remote tags."))
        rc_m, _out_m, _err_m = _run_git(project_root, ["merge", "--ff-only", release.tag], timeout=20.0)
        if rc_m != 0:
            return UpdateApplyResult(ok=False, method="git", message=_msg(lang, "当前分支存在分叉，请手动同步或打开发布页下载。", "Branch has diverged; sync manually."))
    except subprocess.TimeoutExpired:
        return UpdateApplyResult(ok=False, method="git", message=_msg(lang, "拉取更新超时，请稍后重试。", "Git fetch timed out."))
    except OSError:
        return UpdateApplyResult(ok=False, method="git", message=_msg(lang, "未检测到 git 命令，请手动更新。", "git command not found."))

    settings_py = project_root / "config" / "settings.py"
    if settings_py.is_file():
        try:
            os.utime(settings_py, None)
        except OSError:
            pass
    return UpdateApplyResult(
        ok=True, method="git", version=release.version, needs_restart=True,
        message=_msg(lang, f"已更新到 v{release.version}，正在重启。", f"Updated to v{release.version}. Restarting…"),
    )


def apply_zip_archive(zip_path: Path, project_root: Path, version: str = "", lang: str = "zh") -> UpdateApplyResult:
    """Validate and atomically apply a source release ZIP onto `project_root`, preserving `data/`."""
    with tempfile.TemporaryDirectory(prefix="gravitypulse_extract_") as tmp_str:
        extract_dir = Path(tmp_str)
        try:
            safe_extract_zip(zip_path, extract_dir)
        except (zipfile.BadZipFile, ValueError, OSError):
            return UpdateApplyResult(ok=False, method="zip", message=_msg(lang, "更新包校验失败，请重试或手动下载。", "Update archive verification failed."))

        src_root = locate_source_root(extract_dir)
        if src_root is None:
            return UpdateApplyResult(ok=False, method="zip", message=_msg(lang, "更新包缺少核心文件，已中止覆盖。", "Core files missing in archive; update aborted."))

        new_managed_py: set[Path] = set()
        for src_file in sorted(src_root.rglob("*")):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(src_root)
            if not rel.parts or rel.parts[0] in _PROTECTED_ROOT_NAMES:
                continue
            if rel.parts[0] in _MANAGED_PY_DIRS and rel.suffix == ".py":
                new_managed_py.add(rel)
            dst_file = project_root / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_dst = dst_file.with_name(dst_file.name + ".gp_tmp")
            try:
                shutil.copy2(src_file, tmp_dst)
                os.replace(tmp_dst, dst_file)
            except OSError:
                try:
                    tmp_dst.unlink(missing_ok=True)
                except OSError:
                    pass
                return UpdateApplyResult(ok=False, method="zip", message=_msg(lang, f"写入文件失败：{rel.as_posix()}", f"Failed writing {rel.as_posix()}"))

        for mdir in _MANAGED_PY_DIRS:
            base = project_root / mdir
            if not base.is_dir():
                continue
            for old_py in list(base.rglob("*.py")):
                if old_py.relative_to(project_root) not in new_managed_py:
                    try:
                        old_py.unlink()
                    except OSError:
                        pass

    settings_py = project_root / "config" / "settings.py"
    if settings_py.is_file():
        try:
            os.utime(settings_py, None)
        except OSError:
            pass
    ver_s = f" v{version}" if version else ""
    return UpdateApplyResult(
        ok=True, method="zip", version=version, needs_restart=True,
        message=_msg(lang, f"已更新到{ver_s}，正在重启。", f"Updated to{ver_s}. Restarting…"),
    )


def apply_macos_dmg_update(
    dmg_bytes: bytes, version: str = "", downloads_dir: Path | None = None, lang: str = "zh", open_dmg: bool = True,
) -> UpdateApplyResult:
    """Save `.dmg` into `~/Downloads` atomically and open it for drag-to-Applications install."""
    ok, message, path_str = save_and_open_macos_dmg(dmg_bytes, version=version, downloads_dir=downloads_dir, lang=lang, open_dmg=open_dmg)
    return UpdateApplyResult(ok=ok, method="macos_dmg", version=version if ok else "", message=message, downloaded_path=path_str)


def apply_update(
    release: ReleaseInfo,
    project_root: Path = PROJECT_ROOT,
    current_version: str = APP_VERSION,
    status_cb: Callable[[str], None] | None = None,
    downloader: Callable[[str, str, float], bytes] | None = None,
    lang: str = "zh",
    is_frozen: bool = IS_FROZEN,
    platform: str = sys.platform,
    install_dir: Path | None = None,
    downloads_dir: Path | None = None,
    launch_helper: bool = True,
    open_dmg: bool = True,
) -> UpdateApplyResult:
    """Safely update GravityPulse across frozen Win64, frozen macOS DMG, clean Git, and source ZIP."""
    dl_fn = downloader or (lambda u, v, t: _http_get_bytes(u, v, t, max_bytes=_MAX_DOWNLOAD_BYTES))

    if is_frozen and platform == "win32":
        dl_url = release.win_zip_url or release.download_url
        if not dl_url:
            return UpdateApplyResult(ok=False, method="win_frozen", message=_msg(lang, "未找到 Windows 更新包，请打开发布页下载。", "Windows release package not found."))
        if status_cb:
            status_cb(_msg(lang, "正在下载 Windows 更新包…", "Downloading Windows package…"))
        try:
            zip_bytes = dl_fn(dl_url, current_version, 120.0)
        except Exception:
            return UpdateApplyResult(ok=False, method="win_frozen", message=_msg(lang, "下载更新包失败，请稍后重试或打开发布页下载。", "Download failed. Try again or open the release page."))
        if status_cb:
            status_cb(_msg(lang, "正在校验更新包并准备安全替换…", "Verifying package and staging swap…"))
        with tempfile.TemporaryDirectory(prefix="gravitypulse_dl_") as tmp_dir:
            zip_path = Path(tmp_dir) / "GravityPulse-win64.zip"
            zip_path.write_bytes(zip_bytes)
            ok, msg, script_path = stage_win_frozen_zip_update(
                zip_path, install_dir or RUNTIME_ROOT, version=release.version, lang=lang, launch_helper=launch_helper,
            )
            return UpdateApplyResult(
                ok=ok, method="win_frozen", version=release.version if ok else "",
                message=msg, needs_quit=ok, downloaded_path=script_path,
            )

    if is_frozen and platform == "darwin":
        dl_url = release.macos_dmg_url or release.download_url
        if not dl_url:
            return UpdateApplyResult(ok=False, method="macos_dmg", message=_msg(lang, "未找到 macOS DMG 安装包，请打开发布页下载。", "macOS DMG package not found."))
        if status_cb:
            status_cb(_msg(lang, "正在下载 macOS DMG 安装包…", "Downloading macOS DMG…"))
        try:
            dmg_bytes = dl_fn(dl_url, current_version, 120.0)
        except Exception:
            return UpdateApplyResult(ok=False, method="macos_dmg", message=_msg(lang, "下载 DMG 失败，请稍后重试或打开发布页下载。", "DMG download failed. Try again or open the release page."))
        if status_cb:
            status_cb(_msg(lang, "正在保存并打开 DMG…", "Saving and opening DMG…"))
        return apply_macos_dmg_update(dmg_bytes, version=release.version, downloads_dir=downloads_dir, lang=lang, open_dmg=open_dmg)

    is_git, is_dirty, git_ok = inspect_git_worktree(project_root)
    if is_git:
        if not git_ok:
            return UpdateApplyResult(ok=False, method="git", message=_msg(lang, "当前目录为 Git 仓库但未检测到 git，请手动更新。", "Git repository detected, but git is unavailable."))
        if is_dirty:
            return UpdateApplyResult(ok=False, method="git", message=_msg(lang, "检测到本地存在未提交改动，已暂停自动覆盖。", "Uncommitted local changes detected; auto-overwrite skipped."))
        return _apply_git_update(release, project_root, status_cb, lang)

    dl_url = release.source_zip_url or release.download_url
    if not dl_url:
        return UpdateApplyResult(ok=False, method="zip", message=_msg(lang, "未找到可下载的更新包，请打开发布页手动下载。", "No downloadable package found."))
    if status_cb:
        status_cb(_msg(lang, "正在下载更新包…", "Downloading update archive…"))
    try:
        zip_bytes = dl_fn(dl_url, current_version, 60.0)
    except Exception:
        return UpdateApplyResult(ok=False, method="zip", message=_msg(lang, "下载更新包失败，请稍后重试或打开发布页下载。", "Download failed. Try again or open the release page."))
    if status_cb:
        status_cb(_msg(lang, "正在校验并更新文件…", "Verifying and applying files…"))
    with tempfile.TemporaryDirectory(prefix="gravitypulse_dl_") as tmp_dir:
        zip_path = Path(tmp_dir) / "release.zip"
        zip_path.write_bytes(zip_bytes)
        return apply_zip_archive(zip_path, project_root, version=release.version, lang=lang)


__all__ = [
    "ReleaseInfo",
    "UpdateApplyResult",
    "UpdateCheckResult",
    "apply_macos_dmg_update",
    "apply_update",
    "apply_zip_archive",
    "build_win_frozen_swap_script",
    "check_for_update",
    "inspect_git_worktree",
    "is_newer_version",
    "normalize_version",
    "parse_release_payload",
    "parse_version_key",
    "pick_download_url",
    "stage_win_frozen_zip_update",
]
