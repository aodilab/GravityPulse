"""Centralized paths, pricing constants, cache thresholds, IPC identifiers, and UI design tokens."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_FROZEN = bool(getattr(sys, "frozen", False))
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT)) if IS_FROZEN else PROJECT_ROOT
RUNTIME_ROOT = Path(sys.executable).resolve().parent if IS_FROZEN else PROJECT_ROOT
ASSETS_DIR = BUNDLE_ROOT / "assets"
DATA_DIR = (
    Path.home() / "Library" / "Application Support" / "GravityPulse"
    if (IS_MACOS and IS_FROZEN)
    else RUNTIME_ROOT / "data"
)
SCRATCH_DIR = DATA_DIR / "scratch" if (IS_MACOS and IS_FROZEN) else RUNTIME_ROOT / "scratch"
ICON_PATH = ASSETS_DIR / "gravitypulse.ico"
ICNS_PATH = ASSETS_DIR / "gravitypulse.icns"

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc
MIN_VISIBLE_DATE: date | None = None
DEFAULT_GAP_LIMIT = timedelta(minutes=30)
ACTIVE_TASK_GAP_LIMIT = timedelta(minutes=60)
DEFAULT_ANTIGRAVITY_DIR = Path.home() / ".gemini" / "antigravity"
DEFAULT_QUOTA_CONFIG_PATH = DATA_DIR / "quota_config.json"
DEFAULT_UI_PREFS_PATH = DATA_DIR / "ui_prefs.json"
DEFAULT_LEDGER_PATH = DATA_DIR / "telemetry_ledger.db"
RUNNING_STATUS = "CASCADE_RUN_STATUS_RUNNING"

# Official Gemini API base prices ($ per 1M tokens; output includes thinking tokens)
HYDROGEN_INPUT_PER_M = 1.50
HYDROGEN_CACHE_PER_M = 0.075
HYDROGEN_OUTPUT_PER_M = 7.50

# Default Antigravity shared-pool model multipliers relative to Hydrogen (1.0x)
DEFAULT_CARBON_MULTIPLIER = 3.5
DEFAULT_NEON_MULTIPLIER = 1.75

# Neon calibrated Antigravity pool prices ($2.625 / $0.13125 / $13.125 = 1.75x Hydrogen)
NEON_INPUT_PER_M = HYDROGEN_INPUT_PER_M * DEFAULT_NEON_MULTIPLIER
NEON_CACHE_PER_M = HYDROGEN_CACHE_PER_M * DEFAULT_NEON_MULTIPLIER
NEON_OUTPUT_PER_M = HYDROGEN_OUTPUT_PER_M * DEFAULT_NEON_MULTIPLIER

ARGON_INPUT_PER_M = 2.00
ARGON_CACHE_PER_M = 0.10
ARGON_OUTPUT_PER_M = 10.00

FLASH_INPUT_PER_M = 0.75
FLASH_CACHE_PER_M = 0.075
FLASH_OUTPUT_PER_M = 3.75

# Antigravity dual-quota rolling window constants
WEEKLY_CYCLE_SEC = 7 * 86400
FIVE_HOUR_WINDOW_SEC = 5 * 3600
FIVE_HOUR_TO_WEEKLY_RATIO = 0.250

# Context & implicit KV cache survival thresholds
COMPACTION_LIMIT_TOKENS = 256_000
COMPACTION_512K_TOKENS = 512_000
NEAR_COMPACT_WATCH_TOKENS = 220_000
OVERFLOW_SWITCH_TOKENS = 270_000
IMPLICIT_CACHE_MIN_TOKENS = 4_096

CARBON_SWITCH_COMPACTIONS = 5
NEON_SWITCH_COMPACTIONS = 4
HYDROGEN_SWITCH_COMPACTIONS = 4
CARBON_512K_SWITCH_COMPACTIONS = 1
NEON_512K_SWITCH_COMPACTIONS = 1
HYDROGEN_512K_SWITCH_COMPACTIONS = 1
CARBON_512K_IDLE_WATCH_TOKENS = 220_000
CARBON_512K_WATCH_TOKENS = 220_000
CARBON_512K_SWITCH_TOKENS = 260_000
CARBON_512K_RUN_SWITCH_TOKENS = 400_000
NEON_512K_IDLE_WATCH_TOKENS = 190_000
NEON_512K_WATCH_TOKENS = 190_000
NEON_512K_SWITCH_TOKENS = 230_000
NEON_512K_RUN_SWITCH_TOKENS = 430_000
HYDROGEN_512K_IDLE_WATCH_TOKENS = 160_000
HYDROGEN_512K_WATCH_TOKENS = 160_000
HYDROGEN_512K_SWITCH_TOKENS = 190_000
HYDROGEN_512K_RUN_SWITCH_TOKENS = 450_000
CARBON_512K_CRITICAL_TOKENS = 400_000
NEON_512K_CRITICAL_TOKENS = 430_000
HYDROGEN_512K_CRITICAL_TOKENS = 450_000
COLD_CACHE_IDLE_GENTLE_RATIO = 1.05
COLD_CACHE_SWITCH_RATIO = 1.30
PROFILE_RECENT_DIALOGS = 10
PROFILE_RECENT_TURNS = 15
FOLLOWUP_TURN_MIN_DELTA_TOKENS = 30_000
STRUCTURAL_REALERT_DELTA_TOKENS = 50_000
CRITICAL_REALERT_DELTA_TOKENS = 30_000
STRUCTURAL_MAX_ALERTS_PER_CYCLE = 2
CRITICAL_MAX_ALERTS_PER_CYCLE = 3

IMPLICIT_CACHE_FRESH_SEC = 300       # < 5 min (~82-89% hit)
IMPLICIT_CACHE_COOLING_SEC = 900     # 5-15 min (~46-48% hit)
IMPLICIT_CACHE_EXPIRED_SEC = 1_200   # >= 20 min (86.7% real cache miss)

CARBON_CACHE_FRESH_SEC = 180         # < 3 min (94-97% hit)
CARBON_CACHE_COOLING_SEC = 240       # 3-4 min (sharp drop observed around 3.4m)
CARBON_CACHE_EXPIRED_SEC = 360       # >= 6 min (high miss probability)

NEON_CACHE_FRESH_SEC = 300           # < 5 min (post-2026-11-21 pool expansion: 95.0% hit in 3-5m)
NEON_CACHE_COOLING_SEC = 360         # 5-6 min (warning zone before cliff)
NEON_CACHE_EXPIRED_SEC = 420         # >= 7 min (cliff observed between 6.4m hit and 7.8m miss)
NEON_CACHE_POLICY_EPOCH_TS = datetime(2026, 11, 21, 10, 11, 28, tzinfo=timezone.utc).timestamp()

# Built-in fallback model enum mapping (supplemented dynamically per-DB from gen_metadata)
FALLBACK_MODEL_FAMILIES: dict[int, str] = {
    1319: "flash",     # Gemini 3.8 Flash
    1322: "flash",     # Gemini 3.8 Flash (tiered / browser subagent)
    1418: "argon",     # Gemini 4 Argon
    1437: "carbon",    # Gemini 4 Carbon (Medium)
    1438: "carbon",    # Gemini 4 Carbon (Low)
    1525: "hydrogen",  # Gemini 4 Hydrogen (High)
    1526: "hydrogen",  # Gemini 4 Hydrogen (Medium)
    1530: "hydrogen",  # Gemini 4 Hydrogen (Tiered High)
    1531: "hydrogen",  # Gemini 4 Hydrogen (Tiered XHigh)
    1532: "neon",      # Gemini 4 Neon (High)
    1533: "neon",      # Gemini 4 Neon (Medium)
    1667: "exclude",   # Orchestrator (Carbon)
    1669: "exclude",   # Gemini Flash (Orchestrator worker)
}

# Version & GitHub Releases Update Check
APP_VERSION = "1.3.0"
GITHUB_REPO = "aodilab/GravityPulse"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
UPDATE_STARTUP_DELAY_MS = 15_000
UPDATE_CHECK_INTERVAL_MS = 6 * 3600_000
UPDATE_HTTP_TIMEOUT_SEC = 10.0

# Platform Singleton & Companion Watcher Identifiers
APP_USER_MODEL_ID = "GravityPulse.Desktop.App"
WATCHER_MUTEX_NAME = "Local\\GravityPulseWatcherSingleton"
SINGLETON_MUTEX_NAME = "Local\\GravityPulseAppSingleton"
SHOW_EVENT_NAME = "Local\\GravityPulseShowEvent"
TARGET_EXE_NAMES = {"antigravity.exe"}
TARGET_MACOS_NAMES = {"antigravity"}
WATCHER_POLL_SEC = 3.0
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_REG_VALUE_NAME = "GravityPulseWatcher"
MACOS_LAUNCH_AGENT_LABEL = "com.aodilab.gravitypulse.watcher"
MACOS_LAUNCH_AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LAUNCH_AGENT_LABEL}.plist"

# Desktop UI Tokens & Timers
BG_PAGE = "#f5f5f7"
BG_CARD = "#ffffff"
BG_HOVER = "#f7f7f9"
INK = "#1d1d1f"
INK_2 = "#6e6e73"
INK_3 = "#86868b"
LINE_HEX = "#ececef"
BAR_TRACK = "#f2f2f5"
BAR_FILL = "#b8b8bf"
GREEN_TEXT = "#1b6d2f"
GREEN_BG = "#eaf5ed"
GREEN_DOT = "#34c759"
FONT_UI = "PingFang SC" if IS_MACOS else ("Microsoft YaHei UI" if IS_WINDOWS else "Helvetica")

REFRESH_MS = 30_000    # 30s baseline fallback incremental auto-refresh
MONITOR_MS = 15_000    # 15s lightweight background switch-alert check
LIVE_TICK_MS = 1_000   # 1s pure-memory live stopwatch & idle-ago UI tick (visible only)
LIVE_SYNC_MS = 2_000   # 2s lightweight file-fingerprint check for instant incremental & bg alert sync
CACHE_LEARN_TTL_SEC = 60.0  # Reuse 30-day empirical cache thresholds for 60s
IPC_POLL_MS = 400      # 400ms poll for tray / singleton show-window signal
TOAST_DURATION_MS = 12_000

# Engineering Line Limits (checked by `python build.py --audit`)
MAX_SOURCE_LINES = 520
MAX_SCRIPT_LINES = 260
MAX_TEST_LINES = 520
