"""Immutable domain models and formatting helpers for GravityPulse."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import os
import re
from urllib.parse import unquote, urlparse

from config.settings import DEFAULT_CARBON_MULTIPLIER, DEFAULT_NEON_MULTIPLIER, LOCAL_TZ


@dataclass(frozen=True)
class QuotaConfig:
    """Calibration, multiplier & desktop reminder settings."""

    carbon_multiplier: float = DEFAULT_CARBON_MULTIPLIER
    anchor_start: str = "2026-11-14T15:04:01+08:00"
    anchor_end: str = "2026-11-15T23:00:46+08:00"
    anchor_weekly_used_pct: float = 18.0
    subscription_start: str = "2026-11-14T15:04:01+08:00"
    daily_divisor: float = 7.0
    weekly_cap_usd_override: float = 0.0
    notify_switch: bool = True
    rolling_reset_start: str = ""
    carbon_multiplier_history: tuple[tuple[str, float], ...] = ()
    five_hour_reset_start: str = ""
    neon_multiplier: float = DEFAULT_NEON_MULTIPLIER
    auto_check_update: bool = True


@dataclass(frozen=True)
class ModelQuotaUsage:
    """Per-model token and quota consumption percentages for one scope."""

    model_key: str  # "carbon" | "neon" | "hydrogen" | "other"
    input_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    weighted_usd: float = 0.0
    daily_pct: float = 0.0
    weekly_pct: float = 0.0


@dataclass(frozen=True)
class ProjectQuotaUsage:
    """Gemini model quota breakdown for one workspace/project."""

    workspace: str
    carbon: ModelQuotaUsage
    hydrogen: ModelQuotaUsage
    other: ModelQuotaUsage = ModelQuotaUsage(model_key="other")
    neon: ModelQuotaUsage = ModelQuotaUsage(model_key="neon")

    @property
    def total_daily_pct(self) -> float:
        return self.carbon.daily_pct + self.neon.daily_pct + self.hydrogen.daily_pct + self.other.daily_pct

    @property
    def total_weekly_pct(self) -> float:
        return self.carbon.weekly_pct + self.neon.weekly_pct + self.hydrogen.weekly_pct + self.other.weekly_pct

    @property
    def total_weighted_usd(self) -> float:
        return self.carbon.weighted_usd + self.neon.weighted_usd + self.hydrogen.weighted_usd + self.other.weighted_usd


@dataclass(frozen=True)
class IntervalQuotaUsage:
    """Model weekly quota percentages consumed within a single merged work interval."""

    carbon_weekly_pct: float = 0.0
    hydrogen_weekly_pct: float = 0.0
    other_weekly_pct: float = 0.0
    neon_weekly_pct: float = 0.0

    @property
    def total_weekly_pct(self) -> float:
        return self.carbon_weekly_pct + self.neon_weekly_pct + self.hydrogen_weekly_pct + self.other_weekly_pct


@dataclass(frozen=True)
class QuotaSummary:
    """Aggregated quota summary across a day or week, plus live rolling 7d & 5h status."""

    calibrated: bool
    carbon_multiplier: float
    weekly_cap_usd: float
    daily_cap_usd: float
    total_daily_pct: float
    total_weekly_pct: float
    carbon_daily_pct: float
    carbon_weekly_pct: float
    hydrogen_daily_pct: float
    hydrogen_weekly_pct: float
    by_workspace: dict[str, ProjectQuotaUsage]
    other_daily_pct: float = 0.0
    other_weekly_pct: float = 0.0
    neon_daily_pct: float = 0.0
    neon_weekly_pct: float = 0.0
    cycle_used_pct: float = 0.0
    cycle_left_pct: float = 100.0
    cycle_reset_str: str = ""
    five_hour_used_pct: float = 0.0
    five_hour_left_pct: float = 100.0
    five_hour_reset_str: str = ""
    interval_quotas: tuple[IntervalQuotaUsage, ...] = ()


@dataclass(frozen=True)
class TimeInterval:
    """Half-open UTC/timezone-aware time interval [start, end]."""

    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass(frozen=True)
class ConversationMeta:
    """Metadata loaded from conversation_summaries.db."""

    conversation_id: str
    title: str
    workspace: str
    status: str
    parent_conversation_id: str
    nesting_depth: int
    last_modified_time: datetime | None
    last_user_input_time: datetime | None


@dataclass(frozen=True)
class ConversationWork:
    """Work statistics for a single root user conversation on the target day."""

    conversation_id: str
    title: str
    workspace: str
    is_running: bool
    intervals: tuple[TimeInterval, ...]
    total_seconds: float
    last_active: datetime | None


@dataclass(frozen=True)
class ProjectWork:
    """Union-merged work statistics grouped by project/workspace."""

    workspace: str
    is_running: bool
    conversation_count: int
    intervals: tuple[TimeInterval, ...]
    total_seconds: float
    last_active: datetime | None


@dataclass(frozen=True)
class ProjectContextProfile:
    """Dynamically learned per-(workspace, model) warmup cost and 512K switch-zone bounds."""

    sample_count: int = 0
    working_ctx_tokens: int = 0
    new_window_cost_usd: float = 0.0
    turn_span_tokens: int = 0
    zone_start_tokens: int = 0
    idle_switch_tokens: int = 0
    run_switch_tokens: int = 0
    idle_zone_tokens: int = 0
    turn_growth_tokens: int = 0
    turn_steps: float = 0.0


@dataclass(frozen=True)
class DialogContextInfo:
    """Latest turn context token metrics for an active/recent user conversation."""

    conversation_id: str
    title: str
    workspace: str
    model_label: str
    is_running: bool
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    thinking_tokens: int
    last_active: datetime | None
    total_input_tokens: int = 0
    total_cache_read_tokens: int = 0
    compaction_count: int = 0
    cache_thresholds: tuple[int, int, int] | None = None
    user_turn_count: int = 0
    post_finish_turn_count: int = 0
    last_user_input_ts: float = 0.0
    active_subtasks: int = 0
    self_running: bool = True
    context_limit: int = 0
    work_phase: str = ""
    task_done: int = 0
    task_remaining: int = 0
    project_profile: ProjectContextProfile | None = None
    is_handover_turn: bool = False
    is_subdialog: bool = False
    parent_conversation_id: str = ""
    parent_title: str = ""

    @property
    def context_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens

    @property
    def cache_hit_pct(self) -> float:
        denom = self.input_tokens + self.cache_read_tokens
        return (self.cache_read_tokens * 100.0 / denom) if denom > 0 else 0.0

    @property
    def cum_cache_hit_pct(self) -> float:
        denom = self.total_input_tokens + self.total_cache_read_tokens
        return (self.total_cache_read_tokens * 100.0 / denom) if denom > 0 else self.cache_hit_pct


@dataclass(frozen=True)
class DayReport:
    """Aggregated Beijing-time daily work report across Antigravity 2.0 windows."""

    target_date: date
    generated_at: datetime
    total_seconds: float
    merged_intervals: tuple[TimeInterval, ...]
    conversations: tuple[ConversationWork, ...]
    projects: tuple[ProjectWork, ...]
    running_count: int
    quota: QuotaSummary | None = None
    dialogs: tuple[DialogContextInfo, ...] = ()


@dataclass(frozen=True)
class WeekReport:
    """Aggregated Monday-to-Sunday Beijing-time weekly work report."""

    week_start: date
    week_end: date
    generated_at: datetime
    total_seconds: float
    days: tuple[DayReport, ...]
    conversations: tuple[ConversationWork, ...]
    projects: tuple[ProjectWork, ...]
    running_count: int
    quota: QuotaSummary | None = None
    dialogs: tuple[DialogContextInfo, ...] = ()


def parse_iso_utc(raw: str | None) -> datetime | None:
    """Parse ISO-8601 timestamp strings (including 7-digit fractional seconds) into UTC datetime."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or s.startswith("0001-01-01"):
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = s.replace(" ", "T", 1)
    if "." in s:
        head, tail = s.split(".", 1)
        tz_idx = -1
        for sep in ("+", "-"):
            idx = tail.find(sep)
            if idx != -1 and (tz_idx == -1 or idx < tz_idx):
                tz_idx = idx
        if tz_idx != -1:
            frac, tz_part = tail[:tz_idx], tail[tz_idx:]
        else:
            frac, tz_part = tail, ""
        frac = (frac[:6]).ljust(6, "0") if frac.isdigit() else frac[:6]
        s = f"{head}.{frac}{tz_part}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def extract_workspace_name(workspace_uris_json: str) -> str:
    """Extract project name from workspace_uris JSON, folding git/Antigravity worktrees into their parent project."""
    if not workspace_uris_json:
        return "未绑定工作区"
    try:
        uris = json.loads(workspace_uris_json)
    except json.JSONDecodeError:
        return "未绑定工作区"
    if not isinstance(uris, list) or not uris:
        return "未绑定工作区"
    first = str(uris[0])
    parsed = urlparse(first)
    path_str = unquote(parsed.path or first).rstrip("/\\")
    if not path_str:
        return first
    parts = [p for p in re.split(r"[/\\]+", path_str) if p]
    if not parts:
        return path_str
    lower_parts = [p.lower() for p in parts]
    for idx in range(len(parts)):
        # Antigravity managed worktree: .../.gemini/antigravity/worktrees/<project>/<branch>[/...]
        if lower_parts[idx : idx + 2] == ["antigravity", "worktrees"] and idx + 3 < len(parts):
            return parts[idx + 2]
        # Repo-local hidden worktree: .../<project>/.worktrees/<branch>[/...]
        if lower_parts[idx] == ".worktrees" and idx >= 1 and idx + 1 < len(parts) and not parts[idx - 1].endswith(":"):
            return parts[idx - 1]
        # Repo-local Claude/agent worktree: .../<project>/.claude/worktrees/<branch>[/...]
        if lower_parts[idx : idx + 2] == [".claude", "worktrees"] and idx >= 1 and idx + 2 < len(parts) and not parts[idx - 1].endswith(":"):
            return parts[idx - 1]
    return parts[-1]


def local_day_bounds(target_date: date) -> tuple[datetime, datetime]:
    """Return [00:00:00, 24:00:00) in the host's local timezone."""
    start = datetime.combine(target_date, time.min, tzinfo=LOCAL_TZ)
    end = start + timedelta(days=1)
    return start, end


def local_week_bounds(target_date: date) -> tuple[date, date]:
    """Return (Monday, Sunday) dates for the week containing `target_date`."""
    monday = target_date - timedelta(days=target_date.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def format_duration(seconds: float, include_seconds: bool = False) -> str:
    """Format duration in Chinese hours/minutes (and optional seconds)."""
    total_sec = max(0, int(round(seconds)))
    hours, rem = divmod(total_sec, 3600)
    minutes, secs = divmod(rem, 60)
    if include_seconds:
        if hours > 0:
            return f"{hours}小时 {minutes:02d}分 {secs:02d}秒"
        return f"{minutes}分 {secs:02d}秒"
    if hours > 0:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def format_hms(seconds: float) -> str:
    """Format duration as HH:MM:SS."""
    total_sec = max(0, int(round(seconds)))
    hours, rem = divmod(total_sec, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_compact_hm(seconds: float) -> str:
    """Format duration compactly for weekly bar labels (e.g. 8h05m, 36m)."""
    total_sec = max(0, int(round(seconds)))
    if total_sec == 0:
        return "0m"
    hours, rem = divmod(total_sec, 3600)
    minutes = rem // 60
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def format_tokens(count: int) -> str:
    """Format token count cleanly as K or M with at most 1 decimal place (e.g. 8.3M, 239K, 67.7K)."""
    n = max(0, int(count))
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 100_000:
        return f"{round(n / 1_000)}K"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
