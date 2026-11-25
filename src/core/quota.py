"""Gemini quota calibration, weighted cost accounting, and rolling 7d/5h cycle calculation."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import time as _time

from config.settings import (
    ARGON_CACHE_PER_M,
    ARGON_INPUT_PER_M,
    ARGON_OUTPUT_PER_M,
    DEFAULT_CARBON_MULTIPLIER,
    DEFAULT_NEON_MULTIPLIER,
    DEFAULT_QUOTA_CONFIG_PATH,
    FIVE_HOUR_TO_WEEKLY_RATIO,
    FIVE_HOUR_WINDOW_SEC,
    FLASH_CACHE_PER_M,
    FLASH_INPUT_PER_M,
    FLASH_OUTPUT_PER_M,
    HYDROGEN_CACHE_PER_M,
    HYDROGEN_INPUT_PER_M,
    HYDROGEN_OUTPUT_PER_M,
    LOCAL_TZ,
    WEEKLY_CYCLE_SEC,
)
from src.core.models import (
    ConversationMeta, IntervalQuotaUsage, ModelQuotaUsage, ProjectQuotaUsage,
    QuotaConfig, QuotaSummary, TimeInterval, local_week_bounds, parse_iso_utc,
)
from src.core.ledger import load_archived_usage, resolve_ledger_path, sync_telemetry_ledger
from src.core.storage import db_stat_signature, load_db_metadata, read_db_step_usage, resolve_root_id


def load_quota_config(config_path: Path = DEFAULT_QUOTA_CONFIG_PATH) -> QuotaConfig:
    """Load saved quota calibration config from `data/quota_config.json` or return calibrated defaults."""
    if not config_path.is_file():
        return QuotaConfig()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return QuotaConfig()
    raw_hist = raw.get("carbon_multiplier_history", ())
    hist = tuple(
        (str(item[0]), max(0.1, float(item[1])))
        for item in (raw_hist if isinstance(raw_hist, list) else ())
        if isinstance(item, (list, tuple)) and len(item) == 2
    )
    return QuotaConfig(
        carbon_multiplier=max(0.1, float(raw.get("carbon_multiplier", DEFAULT_CARBON_MULTIPLIER))),
        anchor_start=str(raw.get("anchor_start", "2026-11-14T15:04:01+08:00")),
        anchor_end=str(raw.get("anchor_end", "2026-11-15T23:00:46+08:00")),
        anchor_weekly_used_pct=max(0.01, float(raw.get("anchor_weekly_used_pct", 18.0))),
        subscription_start=str(raw.get("subscription_start", "2026-11-14T15:04:01+08:00")),
        daily_divisor=max(1.0, float(raw.get("daily_divisor", 7.0))),
        weekly_cap_usd_override=max(0.0, float(raw.get("weekly_cap_usd_override", 0.0))),
        notify_switch=bool(raw.get("notify_switch", True)),
        rolling_reset_start=str(raw.get("rolling_reset_start", "")),
        carbon_multiplier_history=hist,
        five_hour_reset_start=str(raw.get("five_hour_reset_start", "")),
        neon_multiplier=max(0.1, float(raw.get("neon_multiplier", DEFAULT_NEON_MULTIPLIER))),
        auto_check_update=bool(raw.get("auto_check_update", True)),
    )


def save_quota_config(cfg: QuotaConfig, config_path: Path = DEFAULT_QUOTA_CONFIG_PATH) -> None:
    """Persist quota calibration settings cleanly under `data/quota_config.json`."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "carbon_multiplier": round(cfg.carbon_multiplier, 4),
        "neon_multiplier": round(cfg.neon_multiplier, 4),
        "carbon_multiplier_history": [[ts, round(m, 4)] for ts, m in cfg.carbon_multiplier_history],
        "anchor_start": cfg.anchor_start,
        "anchor_end": cfg.anchor_end,
        "anchor_weekly_used_pct": round(cfg.anchor_weekly_used_pct, 4),
        "subscription_start": cfg.subscription_start,
        "daily_divisor": round(cfg.daily_divisor, 4),
        "weekly_cap_usd_override": round(cfg.weekly_cap_usd_override, 4),
        "notify_switch": bool(cfg.notify_switch),
        "rolling_reset_start": cfg.rolling_reset_start,
        "five_hour_reset_start": cfg.five_hour_reset_start,
        "auto_check_update": bool(cfg.auto_check_update),
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_carbon_history(cfg: QuotaConfig) -> tuple[tuple[float, float], ...]:
    items = [(dt.timestamp(), float(m)) for iso, m in cfg.carbon_multiplier_history if (dt := parse_iso_utc(iso)) is not None and m > 0.0]
    items.sort(key=lambda item: item[0])
    return tuple(items)


def _carbon_mult_at(ts_sec: int, current_mult: float, history: tuple[tuple[float, float], ...]) -> float:
    for before_ts, hist_mult in history:
        if ts_sec < before_ts:
            return hist_mult
    return current_mult


def _hydrogen_base_usd(in_tok: int, cache_tok: int, out_tok: int) -> float:
    return (
        in_tok * HYDROGEN_INPUT_PER_M
        + cache_tok * HYDROGEN_CACHE_PER_M
        + out_tok * HYDROGEN_OUTPUT_PER_M
    ) / 1_000_000.0


def step_weighted_usd(
    family: str,
    in_tok: int,
    cache_tok: int,
    out_tok: int,
    carbon_multiplier: float,
    neon_multiplier: float = DEFAULT_NEON_MULTIPLIER,
) -> float:
    """Compute calibrated weighted USD cost for one Gemini step (`output_tokens` includes thinking)."""
    if family == "carbon":
        return _hydrogen_base_usd(in_tok, cache_tok, out_tok) * carbon_multiplier
    if family == "neon":
        return _hydrogen_base_usd(in_tok, cache_tok, out_tok) * neon_multiplier
    if family == "hydrogen":
        return _hydrogen_base_usd(in_tok, cache_tok, out_tok)
    if family == "argon":
        return (in_tok * ARGON_INPUT_PER_M + cache_tok * ARGON_CACHE_PER_M + out_tok * ARGON_OUTPUT_PER_M) / 1_000_000.0
    if family == "flash":
        return (in_tok * FLASH_INPUT_PER_M + cache_tok * FLASH_CACHE_PER_M + out_tok * FLASH_OUTPUT_PER_M) / 1_000_000.0
    return 0.0


def _is_midnight_excl(start_dt: datetime, end_dt: datetime) -> bool:
    return (
        end_dt > start_dt
        and end_dt.hour == 0 and end_dt.minute == 0 and end_dt.second == 0 and end_dt.microsecond == 0
    )


def _interval_bounds(
    intervals: tuple[TimeInterval, ...] | list[TimeInterval],
) -> list[tuple[int, int, bool]]:
    """Precompute `(start_sec, end_sec, end_exclusive)` bounds for merged intervals."""
    return [(int(iv.start.timestamp()), int(iv.end.timestamp()), _is_midnight_excl(iv.start, iv.end)) for iv in intervals]


def _accumulate_usage_events(
    events: list[tuple[int, str, int, int, int, int]],
    workspace: str,
    start_ts: float,
    end_ts: float,
    carbon_multiplier: float,
    totals: dict[str, dict[str, list[int | float]]],
    interval_bounds: list[tuple[int, int, bool]] | None,
    iv_totals: list[dict[str, float]],
    carbon_history: tuple[tuple[float, float], ...] = (),
    end_excl: bool = False,
    neon_multiplier: float = DEFAULT_NEON_MULTIPLIER,
) -> None:
    for ts_sec, family, in_tok, cache_tok, out_tok, think_tok in events:
        if ts_sec < start_ts or (ts_sec >= end_ts if end_excl else ts_sec > end_ts):
            continue
        c_mult = _carbon_mult_at(ts_sec, carbon_multiplier, carbon_history) if family == "carbon" else carbon_multiplier
        cost = step_weighted_usd(family, in_tok, cache_tok, out_tok, c_mult, neon_multiplier)
        bucket_key = family if family in ("carbon", "neon", "hydrogen") else "other"
        ws_bucket = totals.setdefault(
            workspace,
            {k: [0, 0, 0, 0, 0.0] for k in ("carbon", "neon", "hydrogen", "other")},
        )
        arr = ws_bucket[bucket_key]
        arr[0] = int(arr[0]) + in_tok
        arr[1] = int(arr[1]) + cache_tok
        arr[2] = int(arr[2]) + out_tok
        arr[3] = int(arr[3]) + think_tok
        arr[4] = float(arr[4]) + cost
        if interval_bounds:
            for iv_idx, (iv_s, iv_e, iv_end_excl) in enumerate(interval_bounds):
                if ts_sec >= iv_s and (ts_sec < iv_e if iv_end_excl else ts_sec <= iv_e):
                    iv_totals[iv_idx][bucket_key] += cost
                    break


def _collect_usage_totals(
    antigravity_dir: Path,
    start_ts: float,
    end_ts: float,
    carbon_multiplier: float,
    meta_map: dict[str, ConversationMeta],
    interval_bounds: list[tuple[int, int, bool]] | None = None,
    eff_ledger: Path | None = None,
    carbon_history: tuple[tuple[float, float], ...] = (),
    end_excl: bool = False,
    neon_multiplier: float = DEFAULT_NEON_MULTIPLIER,
) -> tuple[dict[str, dict[str, list[int | float]]], list[dict[str, float]]]:
    conv_dir = antigravity_dir / "conversations"
    totals: dict[str, dict[str, list[int | float]]] = {}
    iv_totals: list[dict[str, float]] = [
        {"carbon": 0.0, "neon": 0.0, "hydrogen": 0.0, "other": 0.0} for _ in (interval_bounds or ())
    ]
    live_cids: set[str] = set()
    if conv_dir.is_dir():
        for db_path in conv_dir.glob("*.db"):
            cid = db_path.stem
            live_cids.add(cid)
            sig_info = db_stat_signature(db_path)
            if sig_info is None or sig_info[0] < start_ts - 3600.0:
                continue
            root_id = resolve_root_id(cid, meta_map)
            meta = meta_map.get(root_id)
            workspace = meta.workspace if meta else "未绑定工作区"
            _accumulate_usage_events(
                read_db_step_usage(db_path), workspace, start_ts, end_ts,
                carbon_multiplier, totals, interval_bounds, iv_totals, carbon_history, end_excl, neon_multiplier,
            )
            _time.sleep(0)

    if eff_ledger is not None and eff_ledger.is_file():
        for cid, arch_ws, events in load_archived_usage(eff_ledger, live_cids, start_ts - 3600.0):
            root_id = resolve_root_id(cid, meta_map)
            meta = meta_map.get(root_id)
            workspace = meta.workspace if meta else (arch_ws or "未绑定工作区")
            _accumulate_usage_events(
                events, workspace, start_ts, end_ts,
                carbon_multiplier, totals, interval_bounds, iv_totals, carbon_history, end_excl, neon_multiplier,
            )
    return totals, iv_totals


def _collect_rolling_events(
    antigravity_dir: Path,
    scan_start_ts: float,
    end_ts: float,
    carbon_multiplier: float,
    eff_ledger: Path | None = None,
    carbon_history: tuple[tuple[float, float], ...] = (),
    neon_multiplier: float = DEFAULT_NEON_MULTIPLIER,
) -> list[tuple[int, float]]:
    conv_dir = antigravity_dir / "conversations"
    if end_ts < scan_start_ts:
        return []
    out: list[tuple[int, float]] = []
    live_cids: set[str] = set()
    if conv_dir.is_dir():
        for db_path in conv_dir.glob("*.db"):
            live_cids.add(db_path.stem)
            sig_info = db_stat_signature(db_path)
            if sig_info is None or sig_info[0] < scan_start_ts - 3600.0:
                continue
            for ts_sec, family, in_tok, cache_tok, out_tok, _think_tok in read_db_step_usage(db_path):
                if scan_start_ts <= ts_sec <= end_ts:
                    c_mult = _carbon_mult_at(ts_sec, carbon_multiplier, carbon_history) if family == "carbon" else carbon_multiplier
                    cost = step_weighted_usd(family, in_tok, cache_tok, out_tok, c_mult, neon_multiplier)
                    if cost > 0.0:
                        out.append((ts_sec, cost))
            _time.sleep(0)
    if eff_ledger is not None and eff_ledger.is_file():
        for _cid, _ws, events in load_archived_usage(eff_ledger, live_cids, scan_start_ts - 3600.0):
            for ts_sec, family, in_tok, cache_tok, out_tok, _think_tok in events:
                if scan_start_ts <= ts_sec <= end_ts:
                    c_mult = _carbon_mult_at(ts_sec, carbon_multiplier, carbon_history) if family == "carbon" else carbon_multiplier
                    cost = step_weighted_usd(family, in_tok, cache_tok, out_tok, c_mult, neon_multiplier)
                    if cost > 0.0:
                        out.append((ts_sec, cost))
    out.sort(key=lambda item: item[0])
    return out


def resolve_current_cycle_anchor_start(
    cfg: QuotaConfig,
    now: datetime,
    antigravity_dir: Path | None = None,
    ledger_path: Path | None = None,
) -> str:
    """Return the start ISO timestamp of the current 7-day quota reset cycle containing `now`."""
    now_loc = now.astimezone(LOCAL_TZ)
    base_raw = cfg.rolling_reset_start or cfg.subscription_start or cfg.anchor_start
    base_dt = parse_iso_utc(base_raw)
    if antigravity_dir is not None:
        eff_ledger = resolve_ledger_path(antigravity_dir, ledger_path)
        if eff_ledger is not None:
            sync_telemetry_ledger(antigravity_dir, ledger_path=eff_ledger)
        scan_start_ts = base_dt.timestamp() if base_dt is not None else max(0.0, now_loc.timestamp() - WEEKLY_CYCLE_SEC)
        events = _collect_rolling_events(
            antigravity_dir, scan_start_ts, now_loc.timestamp(), cfg.carbon_multiplier,
            eff_ledger=eff_ledger, carbon_history=_resolve_carbon_history(cfg), neon_multiplier=cfg.neon_multiplier,
        )
        if events:
            c_start = 0
            c_end = 0
            for ts, _cost in events:
                if ts >= c_end:
                    c_start = ts
                    c_end = ts + WEEKLY_CYCLE_SEC
            if c_end > now_loc.timestamp():
                return datetime.fromtimestamp(c_start, tz=timezone.utc).astimezone(LOCAL_TZ).isoformat(timespec="seconds")
            return now_loc.isoformat(timespec="seconds")

    if base_dt is None:
        mon, _ = local_week_bounds(now_loc.date())
        return datetime.combine(mon, time.min, tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
    base_loc = base_dt.astimezone(LOCAL_TZ)
    if now_loc <= base_loc:
        return base_loc.isoformat(timespec="seconds")
    cycles_elapsed = int((now_loc - base_loc).total_seconds() // float(WEEKLY_CYCLE_SEC))
    cycle_start = base_loc + timedelta(days=7 * cycles_elapsed)
    return cycle_start.isoformat(timespec="seconds")


def _format_reset_countdown(rem_seconds: float) -> str:
    sec = max(0, int(round(rem_seconds)))
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days > 0:
        return f"{days}天{hours}小时后重置"
    if hours > 0:
        return f"{hours}小时{mins:02d}分后重置"
    return f"{max(1, mins)}分钟后重置"


def _compute_rolling_status(
    antigravity_dir: Path,
    ref_end: datetime,
    cfg: QuotaConfig,
    weekly_cap: float,
    eff_ledger: Path | None = None,
) -> tuple[float, float, str, float, float, str]:
    if weekly_cap <= 0.0:
        return 0.0, 100.0, "", 0.0, 100.0, ""
    ref_ts = ref_end.timestamp()
    reset_dt = parse_iso_utc(cfg.rolling_reset_start) if cfg.rolling_reset_start else None
    sub_dt = parse_iso_utc(cfg.subscription_start or cfg.anchor_start)
    if reset_dt is not None:
        scan_start_ts = reset_dt.timestamp()
    elif sub_dt is not None:
        scan_start_ts = sub_dt.timestamp() - 600.0
    else:
        scan_start_ts = max(0.0, ref_ts - WEEKLY_CYCLE_SEC)
    events = _collect_rolling_events(
        antigravity_dir, scan_start_ts, ref_ts, cfg.carbon_multiplier,
        eff_ledger=eff_ledger, carbon_history=_resolve_carbon_history(cfg), neon_multiplier=cfg.neon_multiplier,
    )
    if not events:
        return 0.0, 100.0, "待触发新周期", 0.0, 100.0, "满额待命"

    fh_dt = parse_iso_utc(cfg.five_hour_reset_start) if cfg.five_hour_reset_start else None
    fh_ts = fh_dt.timestamp() if (fh_dt is not None and fh_dt.timestamp() <= ref_ts) else None
    c_start = c_end = w_start = w_end = 0
    for ts, _cost in events:
        if ts >= c_end:
            c_start = ts
            c_end = ts + WEEKLY_CYCLE_SEC
        if (fh_ts is None or ts >= fh_ts) and ts >= w_end:
            w_start = ts
            w_end = ts + FIVE_HOUR_WINDOW_SEC

    if c_end > ref_ts:
        c_usd = sum(cost for ts, cost in events if c_start <= ts <= ref_ts)
        cycle_used = min(100.0, c_usd / weekly_cap * 100.0)
        cycle_left = max(0.0, 100.0 - cycle_used)
        cycle_reset = _format_reset_countdown(c_end - ref_ts)
    else:
        cycle_used, cycle_left, cycle_reset = 0.0, 100.0, "待触发新周期"

    five_h_cap = weekly_cap * FIVE_HOUR_TO_WEEKLY_RATIO
    if w_end > ref_ts and five_h_cap > 0.0:
        w_usd = sum(cost for ts, cost in events if w_start <= ts <= ref_ts)
        five_used = min(100.0, w_usd / five_h_cap * 100.0)
        five_left = max(0.0, 100.0 - five_used)
        five_reset = _format_reset_countdown(w_end - ref_ts)
    else:
        five_used, five_left, five_reset = 0.0, 100.0, "满额待命"

    return cycle_used, cycle_left, cycle_reset, five_used, five_left, five_reset


def compute_quota_summary(
    antigravity_dir: Path,
    win_start: datetime,
    win_end: datetime,
    quota_config: QuotaConfig | None = None,
    meta_map: dict[str, ConversationMeta] | None = None,
    intervals: tuple[TimeInterval, ...] | list[TimeInterval] = (),
    ledger_path: Path | None = None,
) -> QuotaSummary:
    """Compute calibrated daily/weekly quota percentages split by workspace, Gemini models, and intervals."""
    cfg = quota_config or load_quota_config()
    eff_ledger = resolve_ledger_path(antigravity_dir, ledger_path)
    if meta_map is None:
        meta_map = (
            sync_telemetry_ledger(antigravity_dir, ledger_path=eff_ledger)
            if eff_ledger is not None
            else load_db_metadata(antigravity_dir / "conversation_summaries.db")
        )

    c_hist = _resolve_carbon_history(cfg)
    weekly_cap = cfg.weekly_cap_usd_override
    if weekly_cap <= 0.0:
        anc_start = parse_iso_utc(cfg.anchor_start)
        anc_end = parse_iso_utc(cfg.anchor_end)
        if anc_start is not None and anc_end is not None and cfg.anchor_weekly_used_pct > 0.0:
            anc_totals, _ = _collect_usage_totals(
                antigravity_dir, anc_start.timestamp(), anc_end.timestamp(),
                cfg.carbon_multiplier, meta_map, eff_ledger=eff_ledger, carbon_history=c_hist,
                neon_multiplier=cfg.neon_multiplier,
            )
            anc_usd = sum(
                float(b["carbon"][4]) + float(b["neon"][4]) + float(b["hydrogen"][4]) + float(b["other"][4])
                for b in anc_totals.values()
            )
            if anc_usd > 0.0:
                weekly_cap = anc_usd / (cfg.anchor_weekly_used_pct / 100.0)

    calibrated = weekly_cap > 0.0
    daily_cap = (weekly_cap / max(1.0, cfg.daily_divisor)) if calibrated else 0.0

    eff_start = win_start
    sub_start = parse_iso_utc(cfg.subscription_start) if cfg.subscription_start else None
    if sub_start is not None and sub_start > eff_start:
        eff_start = sub_start

    iv_bounds = _interval_bounds(intervals) if intervals else []
    win_end_excl = _is_midnight_excl(win_start, win_end)
    if eff_start < win_end if win_end_excl else eff_start <= win_end:
        period_totals, iv_totals = _collect_usage_totals(
            antigravity_dir, eff_start.timestamp(), win_end.timestamp(),
            cfg.carbon_multiplier, meta_map, interval_bounds=iv_bounds, eff_ledger=eff_ledger,
            carbon_history=c_hist, end_excl=win_end_excl, neon_multiplier=cfg.neon_multiplier,
        )
    else:
        period_totals = {}
        iv_totals = [{"carbon": 0.0, "neon": 0.0, "hydrogen": 0.0, "other": 0.0} for _ in iv_bounds]

    by_ws: dict[str, ProjectQuotaUsage] = {}
    tot_c_usd = tot_n_usd = tot_h_usd = tot_o_usd = 0.0

    for ws, b in period_totals.items():
        c_arr, n_arr, h_arr, o_arr = b["carbon"], b["neon"], b["hydrogen"], b["other"]
        c_usd, n_usd, h_usd, o_usd = float(c_arr[4]), float(n_arr[4]), float(h_arr[4]), float(o_arr[4])
        tot_c_usd += c_usd
        tot_n_usd += n_usd
        tot_h_usd += h_usd
        tot_o_usd += o_usd

        def _mk(key: str, arr: list[int | float], usd: float) -> ModelQuotaUsage:
            return ModelQuotaUsage(
                model_key=key,
                input_tokens=int(arr[0]),
                cache_read_tokens=int(arr[1]),
                output_tokens=int(arr[2]),
                thinking_tokens=int(arr[3]),
                weighted_usd=usd,
                daily_pct=(usd / daily_cap * 100.0) if daily_cap > 0 else 0.0,
                weekly_pct=(usd / weekly_cap * 100.0) if weekly_cap > 0 else 0.0,
            )

        by_ws[ws] = ProjectQuotaUsage(
            workspace=ws,
            carbon=_mk("carbon", c_arr, c_usd),
            hydrogen=_mk("hydrogen", h_arr, h_usd),
            other=_mk("other", o_arr, o_usd),
            neon=_mk("neon", n_arr, n_usd),
        )

    c_d_pct = (tot_c_usd / daily_cap * 100.0) if daily_cap > 0 else 0.0
    c_w_pct = (tot_c_usd / weekly_cap * 100.0) if weekly_cap > 0 else 0.0
    n_d_pct = (tot_n_usd / daily_cap * 100.0) if daily_cap > 0 else 0.0
    n_w_pct = (tot_n_usd / weekly_cap * 100.0) if weekly_cap > 0 else 0.0
    h_d_pct = (tot_h_usd / daily_cap * 100.0) if daily_cap > 0 else 0.0
    h_w_pct = (tot_h_usd / weekly_cap * 100.0) if weekly_cap > 0 else 0.0
    o_d_pct = (tot_o_usd / daily_cap * 100.0) if daily_cap > 0 else 0.0
    o_w_pct = (tot_o_usd / weekly_cap * 100.0) if weekly_cap > 0 else 0.0

    cycle_used, cycle_left, cycle_reset, five_used, five_left, five_reset = (
        _compute_rolling_status(antigravity_dir, win_end, cfg, weekly_cap, eff_ledger=eff_ledger)
        if calibrated
        else (0.0, 100.0, "", 0.0, 100.0, "")
    )

    interval_quotas = tuple(
        IntervalQuotaUsage(
            carbon_weekly_pct=(b["carbon"] / weekly_cap * 100.0) if weekly_cap > 0 else 0.0,
            hydrogen_weekly_pct=(b["hydrogen"] / weekly_cap * 100.0) if weekly_cap > 0 else 0.0,
            other_weekly_pct=(b["other"] / weekly_cap * 100.0) if weekly_cap > 0 else 0.0,
            neon_weekly_pct=(b["neon"] / weekly_cap * 100.0) if weekly_cap > 0 else 0.0,
        )
        for b in iv_totals
    )

    return QuotaSummary(
        calibrated=calibrated,
        carbon_multiplier=cfg.carbon_multiplier,
        weekly_cap_usd=weekly_cap,
        daily_cap_usd=daily_cap,
        total_daily_pct=c_d_pct + n_d_pct + h_d_pct + o_d_pct,
        total_weekly_pct=c_w_pct + n_w_pct + h_w_pct + o_w_pct,
        carbon_daily_pct=c_d_pct,
        carbon_weekly_pct=c_w_pct,
        hydrogen_daily_pct=h_d_pct,
        hydrogen_weekly_pct=h_w_pct,
        by_workspace=by_ws,
        other_daily_pct=o_d_pct,
        other_weekly_pct=o_w_pct,
        neon_daily_pct=n_d_pct,
        neon_weekly_pct=n_w_pct,
        cycle_used_pct=cycle_used,
        cycle_left_pct=cycle_left,
        cycle_reset_str=cycle_reset,
        five_hour_used_pct=five_used,
        five_hour_left_pct=five_left,
        five_hour_reset_str=five_reset,
        interval_quotas=interval_quotas,
    )
