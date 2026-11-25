"""Per-project sliding-window EOQ learning of reopen cost and pause-time 512K switch thresholds."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
import os
from pathlib import Path
import threading
import time as _time

from config import settings as _cfg
from src.core.models import ConversationMeta, ProjectContextProfile
from src.core.quota import step_weighted_usd
from src.core.storage import db_stat_signature, read_db_dialog_telemetry

_FAMILY_LABELS: dict[str, str] = {"carbon": "Carbon", "neon": "Neon", "hydrogen": "Hydrogen"}

# (base_wctx, base_reopen_usd, base_zone, base_idle_sw, base_run_sw, base_span,
#  base_growth, base_steps, base_step_growth, sw_floor, sw_ceil, run_ceil,
#  emp_miss_rate, in_per_m, cache_per_m)
_MODEL_BASELINES: dict[
    str, tuple[int, float, int, int, int, int, int, float, float, int, int, int, float, float, float]
] = {
    "Carbon": (
        100_000, 0.95,
        _cfg.CARBON_512K_WATCH_TOKENS, _cfg.CARBON_512K_SWITCH_TOKENS, _cfg.CARBON_512K_RUN_SWITCH_TOKENS,
        140_000, 75_000, 14.0, 4_500.0, 210_000, 340_000, _cfg.CARBON_512K_CRITICAL_TOKENS,
        0.028,
        _cfg.HYDROGEN_INPUT_PER_M * _cfg.DEFAULT_CARBON_MULTIPLIER,
        _cfg.HYDROGEN_CACHE_PER_M * _cfg.DEFAULT_CARBON_MULTIPLIER,
    ),
    "Neon": (
        70_000, 0.42,
        _cfg.NEON_512K_WATCH_TOKENS, _cfg.NEON_512K_SWITCH_TOKENS, _cfg.NEON_512K_RUN_SWITCH_TOKENS,
        100_000, 55_000, 14.0, 3_600.0, 190_000, 340_000, _cfg.NEON_512K_CRITICAL_TOKENS,
        0.007,
        _cfg.NEON_INPUT_PER_M,
        _cfg.NEON_CACHE_PER_M,
    ),
    "Hydrogen": (
        55_000, 0.18,
        _cfg.HYDROGEN_512K_WATCH_TOKENS, _cfg.HYDROGEN_512K_SWITCH_TOKENS, _cfg.HYDROGEN_512K_RUN_SWITCH_TOKENS,
        80_000, 40_000, 12.0, 3_000.0, 170_000, 300_000, _cfg.HYDROGEN_512K_CRITICAL_TOKENS,
        0.018,
        _cfg.HYDROGEN_INPUT_PER_M,
        _cfg.HYDROGEN_CACHE_PER_M,
    ),
}

_EOQ_HANDOVER_SMOOTHING_TOKENS = 20_000.0
_PRIOR_N = 3.0
_PROFILE_LOCK = threading.Lock()
_PROFILE_CACHE: dict[Path, tuple[float, float, int, dict[tuple[str, str], ProjectContextProfile]]] = {}


def _count_conv_dbs(conv_dir: Path) -> int:
    if not conv_dir.is_dir():
        return 0
    with os.scandir(conv_dir) as it:
        return sum(1 for e in it if e.name.endswith(".db"))


def _weighted_median(samples: list[tuple[float, float]]) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples, key=lambda x: x[0])
    half = sum(w for _, w in ordered) * 0.5
    acc = 0.0
    for val, w in ordered:
        acc += w
        if acc >= half:
            return val
    return ordered[-1][0]


def _round_10k(val: float) -> int:
    return int(round(val / 10_000.0)) * 10_000


def _recent_weighted(values: list[float]) -> list[tuple[float, float]]:
    """Assign 2x weight to the newer half of an already time-sorted sliding window."""
    half = len(values) // 2
    return [(val, 2.0 if idx >= half else 1.0) for idx, val in enumerate(values)]


def compute_project_profile_from_samples(
    model_label: str,
    warmup_ctx_samples: list[tuple[float, float]],
    warmup_usd_samples: list[tuple[float, float]],
    turn_span_samples: list[tuple[float, float]],
    turn_growth_samples: list[tuple[float, float]] | None = None,
    turn_steps_samples: list[tuple[float, float]] | None = None,
    step_growth_samples: list[tuple[float, float]] | None = None,
) -> ProjectContextProfile | None:
    """Derive EOQ pause-time switch threshold, reopen cost, and safety ceiling from recent per-project samples."""
    base = _MODEL_BASELINES.get(model_label)
    n = len(warmup_ctx_samples)
    if base is None or n == 0:
        return None

    (
        b_wctx, b_wusd, _b_zone, _b_isw, _b_rsw, b_span,
        b_grow, b_steps, b_step_g, sw_floor, sw_ceil, run_ceil,
        miss_rate, in_per_m, cache_per_m,
    ) = base
    eff_w = sum(w for _, w in warmup_ctx_samples)
    shrink = eff_w / (eff_w + _PRIOR_N)

    raw_wctx = (1.0 - shrink) * b_wctx + shrink * _weighted_median(warmup_ctx_samples)
    raw_wusd = (1.0 - shrink) * b_wusd + shrink * _weighted_median(warmup_usd_samples)
    raw_span = (1.0 - shrink) * b_span + shrink * _weighted_median(turn_span_samples)
    if turn_growth_samples:
        g_w = sum(w for _, w in turn_growth_samples)
        g_shrink = g_w / (g_w + _PRIOR_N)
        raw_grow = (1.0 - g_shrink) * b_grow + g_shrink * _weighted_median(turn_growth_samples)
    else:
        raw_grow = float(b_grow)
    if turn_steps_samples:
        s_w = sum(w for _, w in turn_steps_samples)
        s_shrink = s_w / (s_w + _PRIOR_N)
        raw_steps = (1.0 - s_shrink) * b_steps + s_shrink * _weighted_median(turn_steps_samples)
    else:
        raw_steps = float(b_steps)

    wctx = int(round(max(b_wctx * 0.45, min(b_wctx * 1.85, raw_wctx))))
    wusd = round(max(b_wusd * 0.50, min(b_wusd * 2.00, raw_wusd)), 4)
    span = int(round(max(40_000.0, min(260_000.0, raw_span))))
    turn_grow = int(round(max(20_000.0, min(240_000.0, raw_grow))))
    turn_steps = round(max(6.0, min(45.0, raw_steps)), 1)

    if step_growth_samples:
        sg_w = sum(w for _, w in step_growth_samples)
        sg_shrink = sg_w / (sg_w + _PRIOR_N)
        raw_step_g = (1.0 - sg_shrink) * b_step_g + sg_shrink * _weighted_median(step_growth_samples)
    elif turn_growth_samples and turn_steps_samples:
        raw_step_g = float(turn_grow) / max(1.0, float(turn_steps))
    else:
        raw_step_g = float(b_step_g)
    step_growth = max(1_800.0, min(12_000.0, raw_step_g))

    p_eff_per_m = max(0.01, cache_per_m + miss_rate * (in_per_m - cache_per_m))
    eoq_delta = math.sqrt(2.0 * wusd * step_growth / (p_eff_per_m / 1_000_000.0))
    eoq_target = float(wctx) + eoq_delta + _EOQ_HANDOVER_SMOOTHING_TOKENS
    eff_burst = max(float(turn_grow), 0.75 * float(span))
    comp_safe = float(_cfg.COMPACTION_512K_TOKENS) - min(140_000.0, eff_burst) - 40_000.0

    idle_switch = max(sw_floor, min(sw_ceil, _round_10k(min(eoq_target, comp_safe))))
    run_switch = run_ceil
    zone_lead = _round_10k(max(40_000.0, min(60_000.0, 0.45 * float(turn_grow))))
    zone_start = max(140_000, idle_switch - zone_lead)

    return ProjectContextProfile(
        sample_count=n,
        working_ctx_tokens=wctx,
        new_window_cost_usd=wusd,
        turn_span_tokens=span,
        zone_start_tokens=zone_start,
        idle_switch_tokens=idle_switch,
        run_switch_tokens=run_switch,
        idle_zone_tokens=zone_start,
        turn_growth_tokens=turn_grow,
        turn_steps=turn_steps,
    )


def learn_project_context_profiles(
    antigravity_dir: Path,
    meta_map: dict[str, ConversationMeta],
    ref_now: datetime | None = None,
) -> dict[tuple[str, str], ProjectContextProfile]:
    """Learn per-(workspace, model) reopen cost & thresholds from the most recent N dialogs and M turns."""
    now_ts = (ref_now or datetime.now(_cfg.LOCAL_TZ)).timestamp()
    conv_dir = antigravity_dir / "conversations"
    if not conv_dir.is_dir():
        return {}
    db_count = _count_conv_dbs(conv_dir)
    now_mono = _time.monotonic()
    with _PROFILE_LOCK:
        cached = _PROFILE_CACHE.get(antigravity_dir)
        if (
            cached is not None
            and (now_mono - cached[0]) < _cfg.CACHE_LEARN_TTL_SEC
            and abs(now_ts - cached[1]) <= 120.0
            and cached[2] == db_count
        ):
            return cached[3]

    min_scan_ts = now_ts - 30 * 86400.0
    dialog_rows: dict[tuple[str, str], list[tuple[int, float, float, float]]] = defaultdict(list)
    turn_rows: dict[tuple[str, str], list[tuple[int, float, float]]] = defaultdict(list)
    step_growth_rows: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)

    for cid, meta in meta_map.items():
        if meta.nesting_depth != 0 or meta.parent_conversation_id:
            continue
        if meta.workspace in ("未绑定工作区", "未知工作区"):
            continue
        db_path = conv_dir / f"{cid}.db"
        if not db_path.is_file():
            continue
        sig_info = db_stat_signature(db_path)
        if sig_info is None or sig_info[0] < min_scan_ts:
            continue
        ev, comps, user_ts, _ = read_db_dialog_telemetry(db_path)
        if len(ev) < 3:
            continue
        first_ts = ev[0][0]
        if first_ts > now_ts + 1.0 or first_ts < min_scan_ts:
            continue
        fam = ev[0][2]
        model_label = _FAMILY_LABELS.get(fam)
        if model_label is None:
            continue

        t2 = float(user_ts[1]) if len(user_ts) >= 2 else float("inf")
        t1_ev = [e for e in ev if e[0] < t2] or ev[:8]
        w_ev = t1_ev[:8]
        w_ctx = float(w_ev[-1][3] + w_ev[-1][4])
        warmup_usd = sum(
            step_weighted_usd(
                e[2], e[3], e[4], e[5],
                _cfg.DEFAULT_CARBON_MULTIPLIER, _cfg.DEFAULT_NEON_MULTIPLIER,
            )
            for e in w_ev
        )
        handover_usd = 1.5 * step_weighted_usd(
            fam, 2_500, 250_000, 3_500,
            _cfg.DEFAULT_CARBON_MULTIPLIER, _cfg.DEFAULT_NEON_MULTIPLIER,
        )
        reopen_usd = 0.80 * warmup_usd + handover_usd
        t1_span = float(max(e[3] + e[4] for e in t1_ev))
        dialog_rows[(meta.workspace, model_label)].append((first_ts, w_ctx, reopen_usd, t1_span))

        if not comps and len(ev) >= len(w_ev) + 4:
            c_end = float(ev[-1][3] + ev[-1][4])
            if c_end > w_ctx + 10_000.0:
                step_growth_rows[(meta.workspace, model_label)].append(
                    (first_ts, (c_end - w_ctx) / float(len(ev) - len(w_ev)))
                )

        bounds = list(user_ts) + [10**12]
        if not user_ts or ev[0][0] < user_ts[0]:
            bounds = [0] + bounds
        per_deltas: list[float] = []
        per_steps: list[float] = []
        for ti in range(len(bounds) - 1):
            tev = [e for e in ev if bounds[ti] <= e[0] < bounds[ti + 1]]
            if len(tev) >= 2:
                delta = float((tev[-1][3] + tev[-1][4]) - (tev[0][3] + tev[0][4]))
                if delta >= 10_000.0:
                    per_deltas.append(delta)
                    per_steps.append(float(len(tev)))
        if per_deltas:
            per_deltas.sort()
            per_steps.sort()
            turn_rows[(meta.workspace, model_label)].append(
                (first_ts, per_deltas[len(per_deltas) // 2], per_steps[len(per_steps) // 2])
            )

    profiles: dict[tuple[str, str], ProjectContextProfile] = {}
    for key, drows in dialog_rows.items():
        _ws, model_label = key
        drows.sort(key=lambda item: item[0])
        recent_d = drows[-_cfg.PROFILE_RECENT_DIALOGS :]
        trows = sorted(turn_rows.get(key, ()), key=lambda item: item[0])[-_cfg.PROFILE_RECENT_DIALOGS :]
        sgrows = sorted(step_growth_rows.get(key, ()), key=lambda item: item[0])[-_cfg.PROFILE_RECENT_TURNS :]
        prof = compute_project_profile_from_samples(
            model_label,
            _recent_weighted([r[1] for r in recent_d]),
            _recent_weighted([r[2] for r in recent_d]),
            _recent_weighted([r[3] for r in recent_d]),
            _recent_weighted([r[1] for r in trows]) if trows else None,
            _recent_weighted([r[2] for r in trows]) if trows else None,
            _recent_weighted([r[1] for r in sgrows]) if sgrows else None,
        )
        if prof is not None:
            profiles[key] = prof

    with _PROFILE_LOCK:
        _PROFILE_CACHE[antigravity_dir] = (_time.monotonic(), now_ts, db_count, profiles)
    return profiles
