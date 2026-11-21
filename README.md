# GravityPulse

[English](README.md) | [简体中文](README_ZH.md)

A lightweight, local-only Windows desktop monitor for **Antigravity 2.0**. It tracks effective coding time, calibrates 7-day / 5-hour rolling quota usage, and monitors per-conversation implicit KV cache warmth so you know when to open a fresh window instead of burning quota on cold recomputation.

> **Disclaimer**: GravityPulse is an unofficial, read-only community tool. It reads local telemetry files under `~/.gemini/antigravity`, requires no API keys, intercepts no network traffic, and uploads nothing. Because Antigravity's internal local storage schema may change in future updates, adjustments may occasionally be needed.

<p align="center">
  <img src="docs/screenshots/dashboard_en.png" alt="GravityPulse Dashboard (English)" width="840" />
</p>

<p align="center">
  <img src="docs/screenshots/toast_en.png" alt="Non-activating silent switch toast (English)" width="380" />
</p>

---

## Why GravityPulse

When working heavily with Antigravity 2.0, three questions come up constantly: how much of your 7-day and 5-hour rolling quota is left, whether a paused conversation's implicit KV cache has already cooled down (making a fresh window cheaper than continuing), and how much active time you actually spent across parallel windows and worktrees. GravityPulse reads local SQLite WAL files and transcripts directly in the background to answer all three without interrupting your workflow.

## Key Features

- **7-Day & 5-Hour Rolling Quota Tracking**: Calibrates against your official quota panel in one dialog, tracks remaining 7-day cycle and 5-hour rolling budgets, and breaks down consumption by model (`Carbon`, `Neon`, `Hydrogen`, `Other`), project, time interval, and day.
- **Implicit KV Cache & Switch-Window Advisor**: Learns per-model cache survival curves from your local step history, displays cumulative and latest-step cache hit rates, and compares cold-recomputation cost against starting a new conversation before suggesting a switch.
- **Accurate Active Work Time**: Uses explicit user messages to define session bounds (30-minute idle bridge, extended to 60 minutes while an agent task is actively running, with zero unattended tail after your last prompt), automatically merging subagents and Git worktrees into parent projects.
- **Subagent-Aware & Non-Intrusive Alerts**: Detects running subagents and non-daemon background tasks to avoid false cold-cache warnings. Switch notifications appear as silent, non-activating bottom-right cards (`WS_EX_NOACTIVATE`) that never steal focus or play system sounds.
- **Local Incremental Ledger & Tray Residency**: Keeps a local ledger under `data/telemetry_ledger.db` so deleting conversations in Antigravity does not erase historical work time or quota records. Runs quietly in the Windows system tray and auto-starts alongside `Antigravity.exe`.
- **Bilingual UI (English / 简体中文)**: Automatically matches your Windows UI language on first launch, with instant switching from the top bar (`EN` / `中`) or tray menu.

---

## Platform & Requirements

- **OS**: Windows 10 / 11 (uses native Win32 tray, non-activating toast windows, and startup registry; macOS/Linux PRs welcome)
- **Runtime (from source)**: Python 3.11+ (with standard `tkinter`, `sqlite3`, `ctypes`) and `Pillow`

---

## Quick Start

### Option 1: Portable Windows Release (No Python Required)

1. Download `GravityPulse-win64.zip` from the [Releases](../../releases) page.
2. Extract it to a folder of your choice and double-click `GravityPulse.exe` (because the portable binary is unsigned, Windows SmartScreen may show a prompt on first launch — click **More info → Run anyway**).
3. Right-click the GravityPulse tray icon to toggle **Auto-start with Antigravity**, **Create Desktop Shortcut**, or switch language — no terminal required.

### Option 2: Run from Source

```powershell
git clone https://github.com/aodilab/GravityPulse.git
cd GravityPulse
pip install -r requirements.txt

# Launch or focus the GravityPulse dashboard
python build.py

# Optional: generate icon, Desktop shortcut, and HKCU auto-start companion watcher
python build.py --setup
```

### First-Time Quota Calibration

Click the quota calibration button in the top bar and enter the current remaining/used percentage shown in your Antigravity Quota panel. GravityPulse stores your calibration locally in `data/quota_config.json` and continuously updates both rolling windows from local telemetry.

---

## CLI Commands

| Command | Description |
| --- | --- |
| `python build.py` | Launch or focus the GravityPulse desktop window |
| `python build.py --background` | Start hidden directly in the Windows system tray |
| `python build.py --watch` | Run the singleton companion watcher that auto-starts GravityPulse when `Antigravity.exe` opens |
| `python build.py --setup` | Generate `assets/gravitypulse.ico`, create Desktop `GravityPulse.lnk`, and register HKCU startup |
| `python build.py --uninstall-startup` | Remove the HKCU startup entry |
| `python build.py --test` | Run the unit test suite |
| `python build.py --audit` | Run line-budget check, syntax check, and full unit tests |

---

## Build Portable `.exe` Locally

```powershell
pip install pyinstaller
python build.py --icon
pyinstaller --noconfirm --clean packaging/gravitypulse.spec
```

The portable build will be output to `dist/GravityPulse/`.

---

## Project Structure

```text
GravityPulse/
├── build.py                      # Unified CLI entrypoint
├── config/settings.py            # Paths, pricing constants, cache thresholds, and UI tokens
├── src/
│   ├── core/                     # WAL/Protobuf decoding, ledger, sessionizer, quota, advisor, watcher
│   └── ui/                       # Pillow supersampled primitives, Win32 tray, incremental canvas views, modals
├── scripts/                      # Multi-size ICO builder, shortcut helper, and audit runner
├── packaging/gravitypulse.spec   # PyInstaller portable Windows spec
├── tests/                        # Unit test suite
└── assets/                       # Application icon
```

## Privacy & Security

- **100% Local**: Reads `~/.gemini/antigravity` in read-only mode and writes runtime config/ledger only inside the local `data/` directory.
- **Zero Telemetry / Network Calls**: GravityPulse makes no outbound HTTP requests.

## License

[MIT](LICENSE)
