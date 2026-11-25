"""Active/recent child subagent & swarm worker context collection and reuse/overload advice."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time as _time

from config import settings as _cfg
from src.core.models import ConversationMeta, DialogContextInfo, ProjectContextProfile, format_tokens
from src.core.storage import (
    db_stat_signature,
    get_cached_db_context_limit,
    read_db_dialog_telemetry,
    resolve_root_id,
)

_FAMILY_LABELS: dict[str, str] = {"carbon": "Carbon", "neon": "Neon", "hydrogen": "Hydrogen"}
SUBDIALOG_IDLE_VISIBLE_SEC = 3600.0
SUBDIALOG_MAX_IDLE_PER_ROOT = 3
SUBDIALOG_REUSE_CEIL_TOKENS = 150_000
SUBDIALOG_RESPAWN_TOKENS = 250_000


def evaluate_subdialog_advice_and_kind(
    dialog: DialogContextInfo,
    cache_state: str,
    safe_limit: int,
    switch_cc: int,
    overflow_limit: int,
    is_critical: bool,
) -> tuple[str, str, str, str, str]:
    """Return `(advice_key, advice_text, advice_reason, notify_detail, switch_kind)` for a child worker."""
    ctx, cc = dialog.context_tokens, dialog.compaction_count
    ctx_s, safe_s, lim_s = format_tokens(ctx), format_tokens(safe_limit), format_tokens(overflow_limit)
    safe_pct = round(ctx * 100.0 / safe_limit) if safe_limit > 0 else 0

    if cc >= switch_cc:
        return (
            "switch", "建议换新",
            f"子窗已压缩 {cc} 次 · 建议换新",
            f"子窗口已压缩 {cc} 次（{ctx_s}），建议在主窗让其收口并 kill 换新 worker。",
            "structural",
        )
    if is_critical:
        if dialog.is_running:
            return (
                "switch", "建议换新",
                "子窗临近压缩 · 建议主窗喊停",
                f"子窗口运行至 {ctx_s}（{safe_pct}%），临近 {safe_s} 压缩，建议在主窗叫停并 kill 换新 worker。",
                "critical",
            )
        return (
            "switch", "建议换新",
            "子窗临近压缩 · 下轮换新",
            f"子窗口停在 {ctx_s}（临近 {safe_s} 压缩），下轮任务请 kill 换新 worker。",
            "critical",
        )
    if ctx >= overflow_limit:
        if dialog.is_running:
            return (
                "switch", "建议换新",
                f"子窗超 {lim_s} · 建议阶段收口",
                f"子窗口运行至 {ctx_s}（超 {lim_s} 高耗线），建议在主窗让其阶段收口并 kill 换新 worker。",
                "structural",
            )
        return (
            "switch", "建议换新",
            f"子窗超 {lim_s} · 下轮换新",
            f"子窗口已达 {ctx_s}（超 {lim_s}），下轮任务请 kill 换新 worker。",
            "structural",
        )
    if ctx >= SUBDIALOG_RESPAWN_TOKENS:
        if dialog.is_running:
            return ("watch", "可考虑换新", "子窗偏重 · 完成后换新", "", "zone")
        return ("watch", "下轮换新", "子窗超 250K · 下轮 kill 换新", "", "zone")
    if ctx > SUBDIALOG_REUSE_CEIL_TOKENS:
        if dialog.is_running:
            return ("ok", "状态健康", "子窗运行中", "", "")
        if cache_state == "fresh":
            return ("ok", "状态健康", "热缓小收尾可复用", "", "")
        return ("watch", "可考虑换新", "子窗已冷 · 大任务换新", "", "")
    if dialog.is_running:
        return ("ok", "状态健康", "子窗运行中", "", "")
    return ("ok", "状态健康", "轻量待命 · 同模块可复用", "", "")


def interleave_subdialog_contexts(
    antigravity_dir: Path,
    meta_map: dict[str, ConversationMeta],
    live_meta: dict[str, ConversationMeta] | None,
    archived_ids: set[str],
    root_items: list[DialogContextInfo],
    learned_thresholds: dict[str, tuple[int, int, int]],
    proj_profiles: dict[tuple[str, str], ProjectContextProfile],
    win_end: datetime,
    is_active_period: bool,
) -> list[DialogContextInfo]:
    """Attach running and recently active child/swarm worker contexts directly beneath their root dialog."""
    if not is_active_period or not root_items:
        return root_items
    conv_dir = antigravity_dir / "conversations"
    if not conv_dir.is_dir():
        return root_items

    root_by_id = {d.conversation_id: d for d in root_items}
    end_ts = win_end.timestamp()
    children_by_root: dict[str, list[DialogContextInfo]] = {}

    for cid, meta in meta_map.items():
        if live_meta is not None and cid not in live_meta:
            continue
        if cid in archived_ids or (meta.nesting_depth == 0 and not meta.parent_conversation_id):
            continue
        root_id = resolve_root_id(cid, meta_map)
        parent_dialog = root_by_id.get(root_id)
        if parent_dialog is None or root_id == cid:
            continue

        is_running = meta.status == _cfg.RUNNING_STATUS
        db_path = conv_dir / f"{cid}.db"
        if not db_path.is_file():
            continue
        sig_info = db_stat_signature(db_path)
        if sig_info is None:
            continue
        if not is_running and sig_info[0] < end_ts - SUBDIALOG_IDLE_VISIBLE_SEC:
            continue

        events, compaction_ts, user_turn_ts, post_finish_turn_ts = read_db_dialog_telemetry(db_path)
        _time.sleep(0)
        if not events:
            if not is_running:
                continue
            lmt_loc = meta.last_modified_time.astimezone(_cfg.LOCAL_TZ) if meta.last_modified_time else win_end
            children_by_root.setdefault(root_id, []).append(
                DialogContextInfo(
                    conversation_id=cid,
                    title=meta.title or "子窗口",
                    workspace=parent_dialog.workspace,
                    model_label="Neon",
                    is_running=True,
                    input_tokens=0, cache_read_tokens=0, output_tokens=0, thinking_tokens=0,
                    last_active=lmt_loc,
                    compaction_count=len(compaction_ts),
                    cache_thresholds=learned_thresholds.get("Neon"),
                    user_turn_count=len(user_turn_ts),
                    post_finish_turn_count=len(post_finish_turn_ts),
                    self_running=True,
                    context_limit=get_cached_db_context_limit(db_path, "neon"),
                    project_profile=proj_profiles.get((parent_dialog.workspace, "Neon")),
                    is_subdialog=True,
                    parent_conversation_id=root_id,
                    parent_title=parent_dialog.title,
                )
            )
            continue

        _, (_s0, step_end_ts, family, in_tok, cache_tok, out_tok, think_tok) = max(
            enumerate(events), key=lambda item: (item[1][0], item[0]),
        )
        last_dt = datetime.fromtimestamp(step_end_ts, tz=timezone.utc).astimezone(_cfg.LOCAL_TZ)
        if meta.last_modified_time is not None:
            lmt_loc = meta.last_modified_time.astimezone(_cfg.LOCAL_TZ)
            if 0.0 < (lmt_loc - last_dt).total_seconds() <= 120.0:
                last_dt = lmt_loc
        if not is_running and (end_ts - last_dt.timestamp()) > SUBDIALOG_IDLE_VISIBLE_SEC:
            continue

        model_label = _FAMILY_LABELS.get(family, "其他")
        ctx_limit = get_cached_db_context_limit(db_path, family)
        children_by_root.setdefault(root_id, []).append(
            DialogContextInfo(
                conversation_id=cid,
                title=meta.title or f"{model_label} 子窗口",
                workspace=parent_dialog.workspace,
                model_label=model_label,
                is_running=is_running,
                input_tokens=in_tok, cache_read_tokens=cache_tok,
                output_tokens=out_tok, thinking_tokens=think_tok,
                last_active=last_dt,
                total_input_tokens=sum(e[3] for e in events),
                total_cache_read_tokens=sum(e[4] for e in events),
                compaction_count=len(compaction_ts),
                cache_thresholds=learned_thresholds.get(model_label),
                user_turn_count=len(user_turn_ts),
                post_finish_turn_count=len(post_finish_turn_ts),
                self_running=is_running,
                context_limit=ctx_limit,
                project_profile=proj_profiles.get((parent_dialog.workspace, model_label)),
                is_subdialog=True,
                parent_conversation_id=root_id,
                parent_title=parent_dialog.title,
            )
        )

    if not children_by_root:
        return root_items

    out: list[DialogContextInfo] = []
    for root in root_items:
        out.append(root)
        raw_children = children_by_root.get(root.conversation_id)
        if not raw_children:
            continue
        raw_children.sort(
            key=lambda d: (
                1 if d.is_running else 0,
                d.last_active.timestamp() if d.last_active else 0.0,
                d.context_tokens,
            ),
            reverse=True,
        )
        running_c = [c for c in raw_children if c.is_running]
        idle_c = [c for c in raw_children if not c.is_running][:SUBDIALOG_MAX_IDLE_PER_ROOT]
        out.extend(running_c)
        out.extend(idle_c)
    return out
