"""Multi-size PNG-in-ICO icon generator and Windows Desktop shortcut builder."""
from __future__ import annotations

import math
from pathlib import Path
import struct
import subprocess
import sys
import zlib

from config.settings import APP_USER_MODEL_ID, ICON_PATH, IS_FROZEN, PROJECT_ROOT, RUNTIME_ROOT
from src.core.watcher import BUILD_ENTRY, resolve_pythonw


def _encode_png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw_rows = bytearray()
    stride = width * 4
    for y in range(height):
        raw_rows.append(0)
        raw_rows.extend(rgba[y * stride : (y + 1) * stride])

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=9)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _blend(bg: tuple[int, int, int, int], fg: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    fa = fg[3] / 255.0
    ba = bg[3] / 255.0
    out_a = fa + ba * (1.0 - fa)
    if out_a <= 1e-6:
        return (0, 0, 0, 0)
    r = int(round((fg[0] * fa + bg[0] * ba * (1.0 - fa)) / out_a))
    g = int(round((fg[1] * fa + bg[1] * ba * (1.0 - fa)) / out_a))
    b = int(round((fg[2] * fa + bg[2] * ba * (1.0 - fa)) / out_a))
    return (r, g, b, int(round(out_a * 255)))


def _dist_point_to_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _render_icon_png(size: int) -> bytes:
    buf = bytearray(size * size * 4)
    cx = cy = (size - 1) / 2.0
    pad = size * 0.06
    half = size / 2.0 - pad
    radius = size * 0.22
    inner_half = half - radius
    ring_r = size * 0.30
    ring_thick = max(1.4, size * 0.065)

    for y in range(size):
        for x in range(size):
            qx = max(abs(x - cx) - inner_half, 0.0)
            qy = max(abs(y - cy) - inner_half, 0.0)
            sdf_box = math.hypot(qx, qy) - radius

            col = (0, 0, 0, 0)
            if sdf_box < 0.8:
                alpha = max(0.0, min(1.0, 0.8 - sdf_box))
                t = (x + y) / (2.0 * max(1, size - 1))
                r = int(round(37 + (79 - 37) * t))
                g = int(round(99 + (70 - 99) * t))
                b = int(round(235 + (229 - 235) * t))
                col = (r, g, b, int(round(alpha * 255)))

            d_ring = abs(math.hypot(x - cx, y - cy) - ring_r)
            if d_ring < ring_thick * 0.5 + 0.8:
                a = max(0.0, min(1.0, (ring_thick * 0.5 + 0.8 - d_ring) / 1.2))
                col = _blend(col, (255, 255, 255, int(round(a * 245))))

            hand_thick = max(1.2, size * 0.055)
            d_min = _dist_point_to_segment(x, y, cx, cy, cx, cy - ring_r * 0.65)
            d_hr = _dist_point_to_segment(x, y, cx, cy, cx + ring_r * 0.48, cy + ring_r * 0.22)
            d_hand = min(d_min, d_hr)
            if d_hand < hand_thick * 0.5 + 0.8:
                a = max(0.0, min(1.0, (hand_thick * 0.5 + 0.8 - d_hand) / 1.2))
                col = _blend(col, (255, 255, 255, int(round(a * 255))))

            dot_x = cx + ring_r * 0.72
            dot_y = cy + ring_r * 0.72
            d_dot = math.hypot(x - dot_x, y - dot_y)
            dot_r = size * 0.095
            if d_dot < dot_r + 0.8:
                a = max(0.0, min(1.0, dot_r + 0.8 - d_dot))
                col = _blend(col, (16, 185, 129, int(round(a * 255))))

            idx = (y * size + x) * 4
            buf[idx : idx + 4] = bytes(col)

    return _encode_png(size, size, bytes(buf))


def build_ico(output_path: Path = ICON_PATH) -> Path:
    """Write 16/32/48/256px PNG-in-ICO file to `assets/gravitypulse.ico`."""
    sizes = [16, 32, 48, 256]
    pngs = [_render_icon_png(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = bytearray()
    for s, png in zip(sizes, pngs):
        dim = 0 if s >= 256 else s
        entries.extend(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset))
        offset += len(png)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(header + bytes(entries) + b"".join(pngs))
    return output_path


def bind_shortcut_app_user_model_id(lnk_path: Path, app_id: str = APP_USER_MODEL_ID) -> bool:
    """Attach `System.AppUserModel.ID` onto a `.lnk` file so Windows Taskbar groups with the running app."""
    if not lnk_path.exists():
        return False
    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
        from win32com.shell import shellcon

        store = propsys.SHGetPropertyStoreFromParsingName(
            str(lnk_path), None, shellcon.GPS_READWRITE, propsys.IID_IPropertyStore
        )
        pv = propsys.PROPVARIANTType(app_id, pythoncom.VT_LPWSTR)
        store.SetValue(pscon.PKEY_AppUserModel_ID, pv)
        store.Commit()
        del pv, store
        return True
    except Exception:
        return False


def create_desktop_shortcut() -> Path:
    """Create or update `GravityPulse.lnk` on the user's Windows Desktop."""
    if not ICON_PATH.exists():
        build_ico(ICON_PATH)
    if IS_FROZEN:
        target_path = str(Path(sys.executable).resolve())
        arguments = ""
        work_dir = str(RUNTIME_ROOT)
    else:
        target_path = resolve_pythonw()
        arguments = f'"{BUILD_ENTRY}"'
        work_dir = str(PROJECT_ROOT)
    icon_loc = f"{ICON_PATH},0" if ICON_PATH.exists() else f"{target_path},0"
    ps_script = (
        "$desktop = [Environment]::GetFolderPath('Desktop'); "
        "$lnkPath = Join-Path $desktop 'GravityPulse.lnk'; "
        "$ws = New-Object -ComObject WScript.Shell; "
        "$sc = $ws.CreateShortcut($lnkPath); "
        f"$sc.TargetPath = '{target_path}'; "
        f"$sc.Arguments = '{arguments}'; "
        f"$sc.WorkingDirectory = '{work_dir}'; "
        f"$sc.IconLocation = '{icon_loc}'; "
        "$sc.Description = 'GravityPulse · Antigravity 2.0 监控面板'; "
        "$sc.Save(); "
        "Write-Output $lnkPath"
    )
    res = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        check=True,
    )
    out_line = (
        res.stdout.strip().splitlines()[-1]
        if res.stdout.strip()
        else str(Path.home() / "Desktop" / "GravityPulse.lnk")
    )
    lnk_path = Path(out_line)
    bind_shortcut_app_user_model_id(lnk_path)
    return lnk_path
