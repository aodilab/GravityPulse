"""Implicit KV cache survival learning, cold-cache breakeven evaluation, and dialog switching advice."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import time as _time

from config import settings as _cfg
from src.core.models import ConversationMeta, DialogContextInfo, ProjectContextProfile, format_tokens
from src.core.phase import build_running_switch_copy, build_running_zone_copy, detect_dialog_work_phase
from src.core.project_profile import learn_project_context_profiles
from src.core.subdialog import evaluate_subdialog_advice_and_kind, interleave_subdialog_contexts
from src.core.storage import (
    collect_active_subtask_counts, db_stat_signature, get_cached_db_context_limit,
    load_archived_conversation_ids, load_db_metadata, read_db_dialog_telemetry,
)

_SURVIVAL_BINS: tuple[tuple[int, int], ...] = (
    (60, 180), (180, 300), (300, 360), (360, 420), (420, 480),
    (480, 900), (900, 1200), (1200, 1800), (1800, 3600),
)
_PRIOR_RATES: dict[str, tuple[float, ...]] = {
    "Carbon": (0.92, 0.36, 0.24, 0.20, 0.16, 0.12, 0.08, 0.04, 0.02),
    "Neon": (0.94, 0.88, 0.45, 0.22, 0.12, 0.08, 0.04, 0.02, 0.01),
    "Hydrogen": (0.90, 0.82, 0.56, 0.52, 0.48, 0.45, 0.26, 0.14, 0.05),
    "其他": (0.90, 0.82, 0.56, 0.52, 0.48, 0.45, 0.26, 0.14, 0.05),
}
_FAMILY_LABELS: dict[str, str] = {"carbon": "Carbon", "neon": "Neon", "hydrogen": "Hydrogen"}
_LEARN_LOCK = threading.Lock()
_LEARN_CACHE: dict[Path, tuple[float, float, int, dict[str, tuple[int, int, int]]]] = {}


def _count_conv_dbs(conv_dir: Path) -> int:
    if not conv_dir.is_dir():
        return 0
    with os.scandir(conv_dir) as it:
        return sum(1 for e in it if e.name.endswith(".db"))


def learn_empirical_cache_thresholds(antigravity_dir: Path, ref_now: datetime | None = None) -> dict[str, tuple[int, int, int]]:
    """Infer `(fresh_sec, cooling_sec, expired_sec)` per model bucket with 60s TTL cache."""
    now_ts = (ref_now or datetime.now(_cfg.LOCAL_TZ)).timestamp()
    conv_dir = antigravity_dir / "conversations"
    db_count = _count_conv_dbs(conv_dir)
    now_mono = _time.monotonic()
    with _LEARN_LOCK:
        cached = _LEARN_CACHE.get(antigravity_dir)
        if cached is not None and (now_mono - cached[0]) < _cfg.CACHE_LEARN_TTL_SEC and abs(now_ts - cached[1]) <= 120.0 and cached[2] == db_count:
            return cached[3]

    min_scan_ts = now_ts - 30 * 86400.0
    recent_cutoff = now_ts - 7 * 86400.0

    hit_w: dict[str, list[float]] = {k: [0.0] * len(_SURVIVAL_BINS) for k in _PRIOR_RATES}
    tot_w: dict[str, list[float]] = {k: [0.0] * len(_SURVIVAL_BINS) for k in _PRIOR_RATES}

    if conv_dir.is_dir():
        for db_path in conv_dir.glob("*.db"):
            sig_info = db_stat_signature(db_path)
            if sig_info is None or sig_info[0] < min_scan_ts:
                continue
            ev, _, _user_tuple, _ = read_db_dialog_telemetry(db_path)
            _time.sleep(0)
            for idx in range(1, len(ev)):
                _s0, e0, f0, in0, c0, _o0, _th0 = ev[idx - 1]
                s1, _e1, f1, in1, c1, _o1, _th1 = ev[idx]
                if s1 > now_ts + 1.0 or s1 < min_scan_ts or f0 != f1:
                    continue
                gap = s1 - e0
                ctx0 = in0 + c0
                if gap < 60 or gap >= 3600 or ctx0 < 10_000 or (in1 + c1) < ctx0 * 0.7:
                    continue
                label = _FAMILY_LABELS.get(f1, "其他")
                if label == "Neon" and now_ts >= _cfg.NEON_CACHE_POLICY_EPOCH_TS and s1 < _cfg.NEON_CACHE_POLICY_EPOCH_TS:
                    continue
                w = 2.0 if s1 >= recent_cutoff else 1.0
                is_hit = 1.0 if c1 >= ctx0 * 0.5 else 0.0
                for b_idx, (lo, hi) in enumerate(_SURVIVAL_BINS):
                    if lo <= gap < hi:
                        tot_w[label][b_idx] += w
                        hit_w[label][b_idx] += w * is_hit
                        break

    prior_n = 6.0
    learned: dict[str, tuple[int, int, int]] = {}
    for label, priors in _PRIOR_RATES.items():
        has_later_obs = False
        blocks: list[list[float]] = []
        for b_idx in range(len(priors) - 1, -1, -1):
            tw = tot_w[label][b_idx]
            hw = hit_w[label][b_idx]
            pw = 0.0 if (tw == 0.0 and has_later_obs) else prior_n
            if tw > 0.0:
                has_later_obs = True
            blocks.insert(0, [pw * priors[b_idx] + hw, pw + tw, float(b_idx), float(b_idx)])
        idx_b = 0
        while idx_b < len(blocks) - 1:
            r_cur = blocks[idx_b][0] / blocks[idx_b][1] if blocks[idx_b][1] > 0 else 0.0
            r_nxt = blocks[idx_b + 1][0] / blocks[idx_b + 1][1] if blocks[idx_b + 1][1] > 0 else 0.0
            if r_cur < r_nxt:
                blocks[idx_b][0] += blocks[idx_b + 1][0]
                blocks[idx_b][1] += blocks[idx_b + 1][1]
                blocks[idx_b][3] = blocks[idx_b + 1][3]
                blocks.pop(idx_b + 1)
                if idx_b > 0:
                    idx_b -= 1
            else:
                idx_b += 1
        rates = [0.0] * len(priors)
        for num, den, b_lo, b_hi in blocks:
            val = num / den if den > 0 else 0.0
            for bi in range(int(b_lo), int(b_hi) + 1):
                rates[bi] = val

        is_c, is_n = label == "Carbon", label == "Neon"
        fresh_s = min(1800, max([120] + [hi for (_lo, hi), r in zip(_SURVIVAL_BINS, rates) if r >= 0.70]))
        cool_floor = _cfg.CARBON_CACHE_COOLING_SEC if is_c else (_cfg.NEON_CACHE_COOLING_SEC if is_n else _cfg.IMPLICIT_CACHE_COOLING_SEC)
        cool_min = fresh_s + (60 if is_c else (30 if is_n else 120))
        cool_s = min(2700, max([cool_min, cool_floor] + [hi for (_lo, hi), r in zip(_SURVIVAL_BINS, rates) if r >= 0.42]))
        exp_floor = _cfg.CARBON_CACHE_EXPIRED_SEC if is_c else (_cfg.NEON_CACHE_EXPIRED_SEC if is_n else _cfg.IMPLICIT_CACHE_EXPIRED_SEC)
        exp_step = 60 if is_c else (30 if is_n else 300)
        exp_s = min(3600, max([exp_floor, cool_s + exp_step] + [hi for (_lo, hi), r in zip(_SURVIVAL_BINS, rates) if r >= 0.32]))
        learned[label] = (fresh_s, cool_s, exp_s)
    with _LEARN_LOCK:
        _LEARN_CACHE[antigravity_dir] = (_time.monotonic(), now_ts, db_count, learned)
    return learned


def collect_dialog_contexts(
    antigravity_dir: Path,
    win_start: datetime,
    win_end: datetime,
    is_active_period: bool,
    meta_map: dict[str, ConversationMeta] | None = None,
    valid_conv_ids: set[str] | None = None,
) -> tuple[DialogContextInfo, ...]:
    """Collect latest turn context token counts for non-archived root project dialogs."""
    summary_db = antigravity_dir / "conversation_summaries.db"
    live_meta = load_db_metadata(summary_db) if summary_db.is_file() else None
    if meta_map is None:
        meta_map = live_meta or {}
    conv_dir = antigravity_dir / "conversations"
    if not conv_dir.is_dir():
        return ()

    archived_ids = load_archived_conversation_ids(antigravity_dir)
    learned_thresholds = learn_empirical_cache_thresholds(antigravity_dir, win_end)
    proj_profiles = learn_project_context_profiles(antigravity_dir, meta_map, win_end)
    subtask_counts = collect_active_subtask_counts(antigravity_dir, meta_map, win_end) if is_active_period else {}
    start_ts = win_start.timestamp()
    end_ts = win_end.timestamp()
    items: list[DialogContextInfo] = []

    for cid, meta in meta_map.items():
        if live_meta is not None and cid not in live_meta:
            continue
        if cid in archived_ids or meta.nesting_depth != 0 or meta.parent_conversation_id:
            continue
        if meta.workspace in ("未绑定工作区", "未知工作区"):
            continue
        if meta.last_user_input_time is None and (valid_conv_ids is None or cid not in valid_conv_ids):
            continue

        self_running = bool(is_active_period and meta.status == _cfg.RUNNING_STATUS)
        active_subtasks = subtask_counts.get(cid, 0) if is_active_period else 0
        is_running = bool(self_running or active_subtasks > 0)
        db_path = conv_dir / f"{cid}.db"
        if not db_path.is_file():
            continue
        sig_info = db_stat_signature(db_path)
        if sig_info is None or (not is_active_period and sig_info[0] < start_ts - 3600.0):
            continue

        events, compaction_ts, user_turn_ts, post_finish_turn_ts = read_db_dialog_telemetry(db_path)
        _time.sleep(0)
        scoped_events = events if is_active_period else [e for e in events if e[0] <= end_ts + 1.0]
        compaction_count = len(compaction_ts) if is_active_period else sum(1 for cts in compaction_ts if cts <= end_ts + 1.0)
        scoped_user_ts = user_turn_ts if is_active_period else tuple(uts for uts in user_turn_ts if uts <= end_ts + 1.0)
        user_turn_count = len(scoped_user_ts)
        post_finish_turn_count = len(post_finish_turn_ts) if is_active_period else sum(1 for pts in post_finish_turn_ts if pts <= end_ts + 1.0)
        meta_user_ts = (
            meta.last_user_input_time.timestamp()
            if meta.last_user_input_time is not None and (is_active_period or meta.last_user_input_time <= win_end)
            else 0.0
        )
        last_user_ts = max(meta_user_ts, float(scoped_user_ts[-1]) if scoped_user_ts else 0.0)
        if user_turn_count == 0 and last_user_ts > 0.0:
            user_turn_count = 1
        if not scoped_events:
            if not is_running:
                continue
            lmt_loc = meta.last_modified_time.astimezone(_cfg.LOCAL_TZ) if meta.last_modified_time else win_end
            wp = detect_dialog_work_phase(antigravity_dir, cid, True, self_running=self_running, active_subtasks=active_subtasks)
            items.append(DialogContextInfo(
                conversation_id=cid, title=meta.title or f"对话 {cid[:8]}", workspace=meta.workspace,
                model_label="Carbon", is_running=True, input_tokens=0, cache_read_tokens=0,
                output_tokens=0, thinking_tokens=0, last_active=lmt_loc, compaction_count=compaction_count,
                cache_thresholds=learned_thresholds.get("Carbon"), user_turn_count=user_turn_count,
                post_finish_turn_count=post_finish_turn_count, last_user_input_ts=last_user_ts,
                active_subtasks=active_subtasks, self_running=self_running,
                context_limit=get_cached_db_context_limit(db_path, "carbon"),
                work_phase=wp.phase, task_done=wp.task_done, task_remaining=wp.task_remaining,
                project_profile=proj_profiles.get((meta.workspace, "Carbon")),
                is_handover_turn=wp.is_handover_turn,
            ))
            continue

        if not is_active_period and not any(e[0] >= start_ts - 1.0 for e in scoped_events):
            continue

        _, (step_start_ts, step_end_ts, family, in_tok, cache_tok, out_tok, think_tok) = max(
            enumerate(scoped_events), key=lambda item: (item[1][0], item[0]),
        )
        model_label = _FAMILY_LABELS.get(family, "其他")
        ctx_limit = get_cached_db_context_limit(db_path, family)
        eff_touch_ts = step_end_ts if (is_active_period or step_end_ts <= end_ts + 1.0) else step_start_ts
        last_dt = datetime.fromtimestamp(eff_touch_ts, tz=timezone.utc).astimezone(_cfg.LOCAL_TZ)
        if meta.last_modified_time is not None:
            lmt_loc = meta.last_modified_time.astimezone(_cfg.LOCAL_TZ)
            if 0.0 < (lmt_loc - last_dt).total_seconds() <= 120.0 and (is_active_period or lmt_loc <= win_end):
                last_dt = lmt_loc
        wp = detect_dialog_work_phase(
            antigravity_dir, cid, is_running if is_active_period else False,
            self_running=self_running if is_active_period else False,
            active_subtasks=active_subtasks if is_active_period else 0,
        )
        items.append(DialogContextInfo(
            conversation_id=cid, title=meta.title or f"对话 {cid[:8]}", workspace=meta.workspace,
            model_label=model_label, is_running=is_running, input_tokens=in_tok, cache_read_tokens=cache_tok,
            output_tokens=out_tok, thinking_tokens=think_tok, last_active=last_dt,
            total_input_tokens=sum(e[3] for e in scoped_events), total_cache_read_tokens=sum(e[4] for e in scoped_events),
            compaction_count=compaction_count, cache_thresholds=learned_thresholds.get(model_label),
            user_turn_count=user_turn_count, post_finish_turn_count=post_finish_turn_count,
            last_user_input_ts=last_user_ts, active_subtasks=active_subtasks, self_running=self_running,
            context_limit=ctx_limit, work_phase=wp.phase, task_done=wp.task_done, task_remaining=wp.task_remaining,
            project_profile=proj_profiles.get((meta.workspace, model_label)), is_handover_turn=wp.is_handover_turn,
        ))

    items.sort(
        key=lambda d: (1 if d.is_running else 0, d.context_tokens, d.last_active.timestamp() if d.last_active else 0.0),
        reverse=True,
    )
    return tuple(interleave_subdialog_contexts(antigravity_dir, meta_map, live_meta, archived_ids, items, learned_thresholds, proj_profiles, win_end, is_active_period))


def _format_idle_ago(idle_seconds: int) -> str:
    if idle_seconds < 60:
        return "刚刚"
    if idle_seconds < 3600:
        return f"{idle_seconds // 60}分钟前"
    if idle_seconds < 86400:
        hours = idle_seconds / 3600.0
        return f"{hours:.1f}小时前" if abs(hours - round(hours)) >= 0.05 else f"{int(round(hours))}小时前"
    return f"{idle_seconds // 86400}天前"


def evaluate_implicit_cache_status(
    dialog: DialogContextInfo,
    now: datetime,
) -> tuple[str, str, str, int]:
    """Estimate implicit prompt cache freshness from empirically learned or baseline model KV decay."""
    idle_sec = max(0, int(round((now - dialog.last_active).total_seconds()))) if dialog.last_active is not None else 0
    ago_txt = _format_idle_ago(idle_sec)
    if dialog.context_tokens < _cfg.IMPLICIT_CACHE_MIN_TOKENS:
        if dialog.is_running and not dialog.self_running and dialog.active_subtasks > 0:
            return "none", "未达4K门槛", f"子任务 {dialog.active_subtasks} · {ago_txt}", idle_sec
        return "none", "未达4K门槛", "运行中" if dialog.is_running else ago_txt, idle_sec
    if dialog.cache_thresholds is not None:
        fresh_s, cool_s, exp_s = dialog.cache_thresholds
    elif dialog.model_label == "Carbon":
        fresh_s, cool_s, exp_s = _cfg.CARBON_CACHE_FRESH_SEC, _cfg.CARBON_CACHE_COOLING_SEC, _cfg.CARBON_CACHE_EXPIRED_SEC
    elif dialog.model_label == "Neon":
        fresh_s, cool_s, exp_s = _cfg.NEON_CACHE_FRESH_SEC, _cfg.NEON_CACHE_COOLING_SEC, _cfg.NEON_CACHE_EXPIRED_SEC
    else:
        fresh_s, cool_s, exp_s = _cfg.IMPLICIT_CACHE_FRESH_SEC, _cfg.IMPLICIT_CACHE_COOLING_SEC, _cfg.IMPLICIT_CACHE_EXPIRED_SEC
    if dialog.is_running and dialog.self_running:
        return "active", "保活中", "运行中", 0
    has_sub = bool(dialog.is_running and not dialog.self_running and dialog.active_subtasks > 0)
    sub_prefix = f"子任务 {dialog.active_subtasks} · " if has_sub else ""
    if idle_sec < fresh_s:
        return "fresh", "主缓有效" if has_sub else "缓存有效", f"{sub_prefix}{ago_txt}", idle_sec
    if idle_sec < cool_s:
        return "cooling", "主缓衰减" if has_sub else "缓存衰减", f"{sub_prefix}{ago_txt}", idle_sec
    if idle_sec < exp_s:
        return "decay", "主缓将冷" if has_sub else "可能失效", f"{sub_prefix}{ago_txt}", idle_sec
    return "expired", "主缓已冷" if has_sub else "基本失效", f"{sub_prefix}{ago_txt}", idle_sec


def dialog_safe_context_limit(dialog: DialogContextInfo) -> int:
    """Return per-conversation auto-compaction window (`256K` or `512K` from `gen_metadata`)."""
    return dialog.context_limit if dialog.context_limit > 0 else _cfg.COMPACTION_LIMIT_TOKENS


_512K_LIMITS: dict[str, tuple[int, int, int, int, int]] = {
    "Carbon": (_cfg.CARBON_512K_IDLE_WATCH_TOKENS, _cfg.CARBON_512K_WATCH_TOKENS, _cfg.CARBON_512K_SWITCH_TOKENS, _cfg.CARBON_512K_RUN_SWITCH_TOKENS, _cfg.CARBON_512K_CRITICAL_TOKENS),
    "Neon": (_cfg.NEON_512K_IDLE_WATCH_TOKENS, _cfg.NEON_512K_WATCH_TOKENS, _cfg.NEON_512K_SWITCH_TOKENS, _cfg.NEON_512K_RUN_SWITCH_TOKENS, _cfg.NEON_512K_CRITICAL_TOKENS),
    "Hydrogen": (_cfg.HYDROGEN_512K_IDLE_WATCH_TOKENS, _cfg.HYDROGEN_512K_WATCH_TOKENS, _cfg.HYDROGEN_512K_SWITCH_TOKENS, _cfg.HYDROGEN_512K_RUN_SWITCH_TOKENS, _cfg.HYDROGEN_512K_CRITICAL_TOKENS),
}


def _near_watch_tokens(safe_limit: int, model_label: str = "", prof: ProjectContextProfile | None = None, is_paused: bool = False) -> int:
    if safe_limit >= _cfg.COMPACTION_512K_TOKENS:
        if is_paused and prof is not None and prof.idle_zone_tokens > 0:
            return prof.idle_zone_tokens
        if not is_paused and prof is not None and prof.zone_start_tokens > 0:
            return prof.zone_start_tokens
        if model_label in _512K_LIMITS:
            return _512K_LIMITS[model_label][0 if is_paused else 1]
    if safe_limit == _cfg.COMPACTION_LIMIT_TOKENS:
        return _cfg.NEAR_COMPACT_WATCH_TOKENS
    return round(safe_limit * _cfg.NEAR_COMPACT_WATCH_TOKENS / _cfg.COMPACTION_LIMIT_TOKENS)


def _overflow_switch_tokens(safe_limit: int, model_label: str = "", relaxed_run: bool = False, prof: ProjectContextProfile | None = None) -> int:
    if safe_limit >= _cfg.COMPACTION_512K_TOKENS:
        if prof is not None and prof.idle_switch_tokens > 0:
            return prof.run_switch_tokens if (relaxed_run and prof.run_switch_tokens > 0) else prof.idle_switch_tokens
        if model_label in _512K_LIMITS:
            return _512K_LIMITS[model_label][3 if relaxed_run else 2]
    if safe_limit == _cfg.COMPACTION_LIMIT_TOKENS:
        return _cfg.OVERFLOW_SWITCH_TOKENS
    return round(safe_limit * _cfg.OVERFLOW_SWITCH_TOKENS / _cfg.COMPACTION_LIMIT_TOKENS)


def _critical_switch_tokens(safe_limit: int, model_label: str, overflow_limit: int) -> int:
    if safe_limit < _cfg.COMPACTION_512K_TOKENS:
        return overflow_limit
    base = _512K_LIMITS[model_label][4] if model_label in _512K_LIMITS else _cfg.HYDROGEN_512K_CRITICAL_TOKENS
    return min(round(safe_limit * 0.88), max(overflow_limit, base))


def _is_pause_checkpoint(dialog: DialogContextInfo) -> bool:
    return (not dialog.is_running) or (dialog.work_phase in ("waiting_user", "waiting_bg"))


def evaluate_cold_cache_breakeven(dialog: DialogContextInfo, cache_state: str, idle_seconds: int | None = None) -> dict[str, float | bool]:
    """Compare expected quota cost of continuing a cooled/expired dialog vs opening a brand-new dialog."""
    ctx, cc, safe_limit = dialog.context_tokens, dialog.compaction_count, dialog_safe_context_limit(dialog)
    eff_cc = cc * max(1.0, safe_limit / _cfg.COMPACTION_LIMIT_TOKENS)
    eff_idle = int(idle_seconds) if idle_seconds is not None else (1200 if cache_state == "expired" else 0)

    if dialog.model_label == "Carbon":
        p_in = _cfg.HYDROGEN_INPUT_PER_M * _cfg.DEFAULT_CARBON_MULTIPLIER
        p_cache = _cfg.HYDROGEN_CACHE_PER_M * _cfg.DEFAULT_CARBON_MULTIPLIER
        base_sys_tok, working_ctx_tok = 25_000, 75_000
        new_window_cost, baggage_rate_per_m, min_net_save = 0.80, 3.50, 0.16
        compaction_drag = 0.045 * min(eff_cc, 5.0) if eff_cc >= 1.5 else 0.0
        miss_map = {"active": 0.05, "fresh": 0.08, "cooling": 0.65, "decay": 0.78, "expired": 0.88 if eff_idle < 900 else 0.96, "none": 0.0}
    elif dialog.model_label == "Neon":
        p_in, p_cache = _cfg.NEON_INPUT_PER_M, _cfg.NEON_CACHE_PER_M
        base_sys_tok, working_ctx_tok = 23_000, 60_000
        new_window_cost, baggage_rate_per_m, min_net_save = 0.40, 1.65, 0.09
        compaction_drag = 0.030 * min(eff_cc, 4.0) if eff_cc >= 2.0 else 0.0
        miss_map = {"active": 0.05, "fresh": 0.08, "cooling": 0.55, "decay": 0.78, "expired": 0.86 if eff_idle < 600 else 0.94, "none": 0.0}
    elif dialog.model_label == "Hydrogen":
        p_in, p_cache = _cfg.HYDROGEN_INPUT_PER_M, _cfg.HYDROGEN_CACHE_PER_M
        base_sys_tok, working_ctx_tok = 21_000, 50_000
        new_window_cost, baggage_rate_per_m, min_net_save = 0.20, 0.55, 0.05
        compaction_drag = 0.022 * min(eff_cc, 4.0) if eff_cc >= 2.0 else 0.0
        miss_map = {"active": 0.05, "fresh": 0.08, "cooling": 0.55, "decay": 0.72, "expired": 0.82 if eff_idle < 1800 else 0.90, "none": 0.0}
    else:
        p_in, p_cache = 1.35, 0.085
        base_sys_tok, working_ctx_tok = 21_000, 50_000
        new_window_cost, baggage_rate_per_m, min_net_save = 0.14, 0.40, 0.04
        compaction_drag = 0.018 * min(eff_cc, 4.0) if eff_cc >= 2.0 else 0.0
        miss_map = {"active": 0.05, "fresh": 0.08, "cooling": 0.50, "decay": 0.68, "expired": 0.82, "none": 0.0}

    prof = dialog.project_profile
    if prof is not None and prof.new_window_cost_usd > 0.0 and new_window_cost > 0.0:
        min_net_save = round(min_net_save * (prof.new_window_cost_usd / new_window_cost), 4)
        new_window_cost = prof.new_window_cost_usd
        if prof.working_ctx_tokens > 0:
            working_ctx_tok = prof.working_ctx_tokens

    p_miss = miss_map.get(cache_state, 0.85)
    d_step1_m = max(0.0, ctx - base_sys_tok) / 1_000_000.0
    d_bag_m = max(0.0, ctx - working_ctx_tok) / 1_000_000.0

    step1_extra = d_step1_m * (p_miss * p_in + (1.0 - p_miss) * p_cache)
    baggage_extra = d_bag_m * baggage_rate_per_m + compaction_drag
    continue_extra = step1_extra + baggage_extra
    net_save = continue_extra - new_window_cost
    ratio = continue_extra / new_window_cost if new_window_cost > 0 else 0.0
    should_switch = cache_state == "expired" and ratio >= _cfg.COLD_CACHE_SWITCH_RATIO and net_save >= min_net_save
    gentle_switch = cache_state == "expired" and not should_switch and ratio >= _cfg.COLD_CACHE_IDLE_GENTLE_RATIO
    return {
        "continue_extra_usd": round(continue_extra, 4), "new_window_cost_usd": round(new_window_cost, 4),
        "net_save_usd": round(net_save, 4), "ratio": round(ratio, 2), "p_miss": round(p_miss, 2),
        "should_switch": should_switch, "gentle_switch": gentle_switch,
    }


def _model_switch_compactions(model_label: str, safe_limit: int = _cfg.COMPACTION_LIMIT_TOKENS) -> int:
    if safe_limit <= _cfg.COMPACTION_LIMIT_TOKENS:
        return {"Carbon": _cfg.CARBON_SWITCH_COMPACTIONS, "Neon": _cfg.NEON_SWITCH_COMPACTIONS}.get(model_label, _cfg.HYDROGEN_SWITCH_COMPACTIONS)
    if model_label in ("Carbon", "Neon"):
        return _cfg.CARBON_512K_SWITCH_COMPACTIONS if model_label == "Carbon" else _cfg.NEON_512K_SWITCH_COMPACTIONS
    if safe_limit == _cfg.COMPACTION_512K_TOKENS:
        return _cfg.HYDROGEN_512K_SWITCH_COMPACTIONS
    return max(1, int(_cfg.HYDROGEN_SWITCH_COMPACTIONS * _cfg.COMPACTION_LIMIT_TOKENS / safe_limit + 0.5))


def _watch_compactions(switch_cc: int) -> int:
    return min(3, max(1, switch_cc - 1))


def classify_switch_kind(dialog: DialogContextInfo, cache_state: str, idle_seconds: int | None = None) -> str:
    """Two-step classifier: Step-1 pause economic/cold-cache switch, Step-2 running/paused safety-net (`critical`/compaction)."""
    if dialog.is_handover_turn:
        return ""
    ctx, cc = dialog.context_tokens, dialog.compaction_count
    safe_limit = dialog_safe_context_limit(dialog)
    prof = dialog.project_profile
    is_paused = _is_pause_checkpoint(dialog)
    overflow_limit = _overflow_switch_tokens(safe_limit, dialog.model_label, not is_paused, prof)
    is_critical = safe_limit >= _cfg.COMPACTION_512K_TOKENS and ctx >= _critical_switch_tokens(safe_limit, dialog.model_label, overflow_limit)
    if dialog.is_subdialog:
        sub_lim = _overflow_switch_tokens(safe_limit, dialog.model_label, True, prof)
        sub_crit = safe_limit >= _cfg.COMPACTION_512K_TOKENS and ctx >= _critical_switch_tokens(safe_limit, dialog.model_label, sub_lim)
        return evaluate_subdialog_advice_and_kind(dialog, cache_state, safe_limit, _model_switch_compactions(dialog.model_label, safe_limit), sub_lim, sub_crit)[4]
    if is_critical:
        return "critical"
    if cc >= _model_switch_compactions(dialog.model_label, safe_limit) or ctx >= overflow_limit:
        return "structural"
    be = evaluate_cold_cache_breakeven(dialog, cache_state, idle_seconds)
    if not dialog.is_running and bool(be["should_switch"]):
        return "cache"
    if not dialog.is_running and bool(be["gentle_switch"]):
        return "gentle_cache"
    is_high_cost_cap = dialog.model_label in ("Carbon", "Neon", "Hydrogen") and safe_limit >= _cfg.COMPACTION_512K_TOKENS
    if is_high_cost_cap and ctx >= _near_watch_tokens(safe_limit, dialog.model_label, prof, is_paused):
        return "zone" if is_paused else "zone_armed"
    return ""


def evaluate_dialog_advice_detail(
    dialog: DialogContextInfo, cache_state: str, idle_seconds: int | None = None,
) -> tuple[str, str, str, str]:
    """Two-step per-project/model EOQ pause advice plus running compaction safety-net guidance."""
    ctx, cc, safe_limit, prof = dialog.context_tokens, dialog.compaction_count, dialog_safe_context_limit(dialog), dialog.project_profile
    ctx_s, safe_s = format_tokens(ctx), format_tokens(safe_limit)
    is_paused = _is_pause_checkpoint(dialog)
    near_limit = _near_watch_tokens(safe_limit, dialog.model_label, prof, is_paused)
    idle_limit = _overflow_switch_tokens(safe_limit, dialog.model_label, False, prof)
    overflow_limit = idle_limit if is_paused else _overflow_switch_tokens(safe_limit, dialog.model_label, True, prof)
    is_critical = safe_limit >= _cfg.COMPACTION_512K_TOKENS and ctx >= _critical_switch_tokens(safe_limit, dialog.model_label, overflow_limit)
    safe_pct = round(ctx * 100.0 / safe_limit) if safe_limit > 0 else 0
    lim_s, idle_s = format_tokens(overflow_limit), format_tokens(idle_limit)
    is_high_cost_cap = dialog.model_label in ("Carbon", "Neon", "Hydrogen") and safe_limit >= _cfg.COMPACTION_512K_TOKENS
    be = evaluate_cold_cache_breakeven(dialog, cache_state, idle_seconds)
    be_ratio = float(be["ratio"])
    idle_tag = f"静置 {int(idle_seconds) // 60} 分钟" if (idle_seconds and idle_seconds >= 60) else ""
    m_name = dialog.model_label if dialog.model_label in ("Carbon", "Neon", "Hydrogen") else "当前模型"
    switch_cc = _model_switch_compactions(dialog.model_label, safe_limit)
    watch_cc = _watch_compactions(switch_cc)

    if dialog.is_subdialog:
        sub_lim = _overflow_switch_tokens(safe_limit, dialog.model_label, True, prof)
        sub_crit = safe_limit >= _cfg.COMPACTION_512K_TOKENS and ctx >= _critical_switch_tokens(safe_limit, dialog.model_label, sub_lim)
        return evaluate_subdialog_advice_and_kind(dialog, cache_state, safe_limit, switch_cc, sub_lim, sub_crit)[:4]
    if dialog.is_running and dialog.is_handover_turn:
        return ("watch", "交接中", "正在写交接 · 完成后换新", "")
    if cc >= switch_cc:
        if dialog.is_running:
            sr, nd = build_running_switch_copy(m_name, ctx_s, lim_s, safe_s, cc, dialog.work_phase, dialog.task_done, dialog.task_remaining, True)
            return ("switch", "建议换新", sr, nd)
        return ("switch", "建议换新", f"已压缩 {cc} 次 · 换窗零损耗", f"已压缩 {cc} 次 · 建议开新窗")
    if is_critical:
        if dialog.is_running:
            sr, nd = build_running_switch_copy(m_name, ctx_s, lim_s, safe_s, cc, dialog.work_phase, dialog.task_done, dialog.task_remaining, False, True, safe_pct)
            return ("switch", "建议换新", sr, nd)
        return ("switch", "建议换新", "临近压缩 · 建议立即换窗", f"{ctx_s} · 临近压缩，请立即换新")
    if ctx >= overflow_limit:
        if is_high_cost_cap and is_paused:
            if dialog.work_phase == "waiting_user":
                return ("switch", "建议换新", f"待确认 · 达 {lim_s} 经济线", f"{ctx_s} 待确认 · 已达经济线，建议换新")
            if dialog.work_phase == "waiting_bg":
                return ("switch", "建议换新", f"等后台 · 达 {lim_s} 经济线", f"{ctx_s} 等后台 · 已达经济线，可写交接换新")
            return ("switch", "建议换新", f"已停顿 · 达 {lim_s} 经济线", f"{ctx_s} · 已达经济线，建议换新")
        if dialog.is_running:
            sr, nd = build_running_switch_copy(m_name, ctx_s, lim_s, safe_s, cc, dialog.work_phase, dialog.task_done, dialog.task_remaining, False, False, safe_pct)
            return ("switch", "建议换新", sr, nd)
        return ("switch", "建议换新", f"超 {lim_s} · 窗口过载", f"{ctx_s} · 窗口过载，建议换新")
    if not dialog.is_running and bool(be["should_switch"]):
        return ("switch", "建议换新", f"缓存失效 · 重算约新窗{be_ratio:.1f}x", f"{ctx_s} 缓存已冷 · 重算约 {be_ratio:.1f}x，建议换新")
    if not dialog.is_running and bool(be["gentle_switch"]):
        return ("watch", "可考虑换新", f"缓存失效 · 约新窗{be_ratio:.1f}x", f"{idle_tag}缓存已冷 · 约新窗 {be_ratio:.1f}x，可考虑换新")
    if switch_cc > 1 and cc == switch_cc - 1 and ctx >= near_limit:
        return ("watch", "临近压缩", f"已压缩 {cc} 次 · 临近第{switch_cc}次压缩", "")
    if switch_cc > 1 and cc >= watch_cc:
        return ("watch", "注意控制", f"已压缩 {cc} 次 · 可按阶段收尾", "")
    if is_high_cost_cap and not is_paused and ctx >= idle_limit:
        return ("watch", "停顿换新", f"达 {idle_s} 经济线 · 停顿后换新", "")
    if ctx >= near_limit:
        if is_high_cost_cap:
            near_s = format_tokens(near_limit)
            if not dialog.is_running:
                return ("watch", "可考虑换新", f"接近 {idle_s} 经济线", f"{ctx_s} · 长任务换新，短任务可续")
            sr, nd = build_running_zone_copy(m_name, ctx_s, near_s, idle_s, dialog.work_phase, dialog.task_done, dialog.task_remaining)
            return ("watch", "可考虑换新", sr, nd)
        prefix = f"已压缩 {cc} 次 · " if cc > 0 else ""
        return ("watch", "临近压缩", f"{prefix}临近 {safe_s} 压缩", "")
    if not dialog.is_running:
        if cache_state == "expired" and be_ratio >= 0.85:
            return ("watch", "注意控制", "缓存失效 · 续聊新开相近", "")
        if cache_state == "decay" and be_ratio >= 1.15:
            return ("watch", "注意控制", "缓存转冷 · 趁热或换窗", "")
        if cache_state == "cooling" and be_ratio >= 1.15:
            return ("watch", "注意控制", "缓存衰减 · 趁热续聊", "")
        if cache_state == "expired" and ctx >= _cfg.IMPLICIT_CACHE_MIN_TOKENS:
            return ("ok", "状态健康", "缓存虽冷 · 短文续聊更省", "")
    return ("ok", "状态健康", f"已压缩 {cc} 次" if cc > 0 else "未触发压缩", "")


def evaluate_dialog_advice(dialog: DialogContextInfo, cache_state: str, idle_seconds: int | None = None) -> tuple[str, str]:
    """Return `(advice_key, advice_text)` from the dual-mode Carbon/Neon/Hydrogen window algorithm."""
    return evaluate_dialog_advice_detail(dialog, cache_state, idle_seconds)[:2]
