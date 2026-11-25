"""Deduplicating state machine for high-value window-switch alerts (imminent compaction, high-cost stop, cold cache)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from config.settings import (
    CRITICAL_MAX_ALERTS_PER_CYCLE,
    CRITICAL_REALERT_DELTA_TOKENS,
    FOLLOWUP_TURN_MIN_DELTA_TOKENS,
    STRUCTURAL_MAX_ALERTS_PER_CYCLE,
    STRUCTURAL_REALERT_DELTA_TOKENS,
)
from src.core.models import format_tokens

_DEFERRED_RUN_PHASES = frozenset({"exploring", "editing", "verifying"})
_HIGH_KINDS = frozenset({"structural", "critical"})


@dataclass
class _DialogAlertState:
    switch_kind: str
    baseline_compactions: int
    baseline_user_turns: int
    baseline_post_finish_turns: int
    baseline_user_ts: float
    baseline_context_tokens: int
    alert_count: int = 1
    was_running: bool = False
    baseline_phase: str = ""


def _resolve_alert_kind(dialog: dict) -> str:
    """Return desktop-toast alert kind (`critical`/`structural`/`cache`/`zone`) or `''` (quiet panel-only watch)."""
    if dialog.get("is_handover_turn"):
        return ""
    advice_key = str(dialog.get("advice_key", ""))
    raw_kind = str(dialog.get("switch_kind") or "")
    if advice_key == "switch":
        return raw_kind or "structural"
    if advice_key == "watch" and raw_kind == "zone" and not dialog.get("is_subdialog"):
        return "zone"
    return ""


class SwitchAlertTracker:
    """Low-noise alert state machine: quiet below breakeven, strict on imminent compaction, high-cost stop & cold cache."""

    def __init__(
        self,
        realert_delta_tokens: int = STRUCTURAL_REALERT_DELTA_TOKENS,
        max_structural_alerts: int = STRUCTURAL_MAX_ALERTS_PER_CYCLE,
        critical_realert_delta_tokens: int = CRITICAL_REALERT_DELTA_TOKENS,
        max_critical_alerts: int = CRITICAL_MAX_ALERTS_PER_CYCLE,
        followup_min_delta_tokens: int = FOLLOWUP_TURN_MIN_DELTA_TOKENS,
    ) -> None:
        self.seeded: bool = False
        self._realert_delta_tokens: int = max(1, int(realert_delta_tokens))
        self._critical_realert_delta_tokens: int = max(1, int(critical_realert_delta_tokens))
        self._max_structural_alerts: int = max(1, int(max_structural_alerts))
        self._max_critical_alerts: int = max(1, int(max_critical_alerts))
        self._followup_min_delta_tokens: int = max(0, int(followup_min_delta_tokens))
        self._states: dict[str, _DialogAlertState] = {}

    def process(self, dialogs: Iterable[dict], notify_enabled: bool = True) -> tuple[str, str] | None:
        alerts = self._process_impl(dialogs, notify_enabled=notify_enabled, single_per_call=True)
        return alerts[0][:2] if alerts else None

    def process_batch(self, dialogs: Iterable[dict], notify_enabled: bool = True) -> list[tuple[str, str]]:
        """Process all dialogs and return every newly triggered alert without dropping parallel windows."""
        return [a[:2] for a in self._process_impl(dialogs, notify_enabled=notify_enabled, single_per_call=False)]

    def process_batch_with_kind(self, dialogs: Iterable[dict], notify_enabled: bool = True) -> list[tuple[str, str, bool]]:
        """Return `(head, detail, is_zone)` tuples so UI badges stay accurate without noisy prefixes."""
        return self._process_impl(dialogs, notify_enabled=notify_enabled, single_per_call=False)

    def _process_impl(
        self, dialogs: Iterable[dict], notify_enabled: bool, single_per_call: bool,
    ) -> list[tuple[str, str, bool]]:
        dialog_list = list(dialogs)
        if not self.seeded:
            for d in dialog_list:
                kind = _resolve_alert_kind(d)
                if kind:
                    cid = str(d.get("conversation_id", ""))
                    if cid:
                        is_run = bool(d.get("is_running"))
                        init_count = 0 if (is_run and kind == "critical") else 1
                        self._states[cid] = _DialogAlertState(
                            switch_kind=kind,
                            baseline_compactions=int(d.get("compaction_count", 0) or 0),
                            baseline_user_turns=int(d.get("user_turn_count", 0) or 0),
                            baseline_post_finish_turns=int(d.get("post_finish_turn_count", 0) or 0),
                            baseline_user_ts=float(d.get("last_user_input_ts", 0.0) or 0.0),
                            baseline_context_tokens=int(d.get("context_tokens", 0) or 0),
                            alert_count=init_count,
                            was_running=is_run,
                            baseline_phase=str(d.get("work_phase") or ""),
                        )
            self.seeded = True
            return []

        pending_alerts: list[tuple[str, str, bool]] = []
        for d in dialog_list:
            cid = str(d.get("conversation_id", ""))
            if not cid:
                continue
            advice_key = str(d.get("advice_key", ""))
            cache_state = str(d.get("cache_state", ""))
            is_running = bool(d.get("is_running"))
            work_phase = str(d.get("work_phase") or "")
            compactions = int(d.get("compaction_count", 0) or 0)
            user_turns = int(d.get("user_turn_count", 0) or 0)
            post_finish_turns = int(d.get("post_finish_turn_count", 0) or 0)
            last_user_ts = float(d.get("last_user_input_ts", 0.0) or 0.0)
            ctx_tokens = int(d.get("context_tokens", 0) or 0)

            prev = self._states.get(cid)
            switch_kind = _resolve_alert_kind(d)
            if not switch_kind:
                if prev is not None and (
                    advice_key == "ok"
                    or (prev.switch_kind == "cache" and cache_state in ("active", "fresh"))
                ):
                    self._states.pop(cid, None)
                continue

            idle_sec = int(d.get("idle_seconds", 0) or 0)

            if is_running and switch_kind not in _HIGH_KINDS and not (switch_kind == "zone" and work_phase in ("waiting_user", "waiting_bg")):
                continue

            is_followup = False
            followup_delta = 0
            next_alert_count = 1
            stopped_live = False
            if prev is not None:
                if prev.switch_kind == "cache" and switch_kind == "cache":
                    continue
                if prev.switch_kind == "zone" and switch_kind == "zone":
                    continue

                ctx_delta = ctx_tokens - prev.baseline_context_tokens
                has_more_compaction = switch_kind in _HIGH_KINDS and compactions > prev.baseline_compactions
                raw_new_turn = (
                    post_finish_turns > prev.baseline_post_finish_turns
                    or user_turns > prev.baseline_user_turns
                    or (last_user_ts > 0.0 and prev.baseline_user_ts > 0.0 and last_user_ts > prev.baseline_user_ts + 0.5)
                )
                has_heavy_new_turn = raw_new_turn and (
                    switch_kind == "critical"
                    or ctx_delta >= self._followup_min_delta_tokens
                )
                upgraded_to_critical = prev.switch_kind != "critical" and switch_kind == "critical"
                upgraded_to_structural = prev.switch_kind not in _HIGH_KINDS and switch_kind == "structural"
                upgraded_to_cache = prev.switch_kind == "zone" and switch_kind == "cache"
                stopped_after_deferred_structural = (
                    prev.switch_kind in _HIGH_KINDS
                    and switch_kind in _HIGH_KINDS
                    and prev.was_running
                    and not is_running
                    and prev.baseline_phase in _DEFERRED_RUN_PHASES
                )
                returned_to_pause = (
                    prev.switch_kind == "structural"
                    and switch_kind == "structural"
                    and prev.was_running
                    and not is_running
                    and prev.baseline_phase in ("waiting_user", "waiting_bg")
                    and ctx_delta >= self._followup_min_delta_tokens
                )
                stopped_live = stopped_after_deferred_structural or returned_to_pause
                hit_pause_node = (
                    prev.switch_kind in _HIGH_KINDS
                    and prev.switch_kind == switch_kind
                    and prev.was_running
                    and is_running
                    and work_phase != prev.baseline_phase
                    and prev.baseline_phase in _DEFERRED_RUN_PHASES
                    and work_phase in ("waiting_user", "waiting_bg", "new_stage")
                )
                is_new_cycle = (
                    has_more_compaction
                    or has_heavy_new_turn
                    or upgraded_to_critical
                    or upgraded_to_structural
                    or upgraded_to_cache
                    or stopped_live
                    or hit_pause_node
                )

                if not is_new_cycle:
                    req_delta = self._critical_realert_delta_tokens if switch_kind == "critical" else self._realert_delta_tokens
                    max_alerts = self._max_critical_alerts if switch_kind == "critical" else self._max_structural_alerts
                    has_ctx_growth = (
                        switch_kind in _HIGH_KINDS
                        and prev.switch_kind == switch_kind
                        and prev.alert_count < max_alerts
                        and ctx_delta >= req_delta
                    )
                    if not has_ctx_growth:
                        continue
                    is_followup = prev.alert_count >= 1
                    followup_delta = ctx_delta
                    next_alert_count = prev.alert_count + 1

            if switch_kind in _HIGH_KINDS or switch_kind == "zone":
                if not is_running and not stopped_live and idle_sec > 600:
                    continue
            elif idle_sec > 2700:
                continue

            if single_per_call and notify_enabled and pending_alerts:
                continue

            self._states[cid] = _DialogAlertState(
                switch_kind=switch_kind,
                baseline_compactions=compactions,
                baseline_user_turns=user_turns,
                baseline_post_finish_turns=post_finish_turns,
                baseline_user_ts=last_user_ts,
                baseline_context_tokens=ctx_tokens,
                alert_count=next_alert_count,
                was_running=is_running,
                baseline_phase=work_phase,
            )
            if notify_enabled:
                if d.get("is_subdialog"):
                    p_title = str(d.get("parent_title") or d.get("title") or "")
                    head = f"{d.get('workspace', '')} · {p_title} · 子窗口 ({d.get('model_label', '')})"
                else:
                    head = f"{d.get('workspace', '')} · {d.get('title', '')} ({d.get('model_label', '')})"
                base_detail = str(d.get("notify_detail") or d.get("advice_reason") or "上下文已达换窗线，建议开新窗。")
                if is_followup:
                    delta_fmt = format_tokens(followup_delta)
                    ctx_fmt = format_tokens(ctx_tokens)
                    prefix = (
                        f"二次提醒（+{delta_fmt}）："
                        if ctx_fmt in base_detail
                        else f"二次提醒（+{delta_fmt}，当前 {ctx_fmt}）："
                    )
                    detail = f"{prefix}{base_detail}"
                else:
                    detail = base_detail
                is_step1_pause = (
                    not bool(d.get("is_subdialog"))
                    and switch_kind != "critical"
                    and compactions == 0
                    and (not is_running or work_phase in ("waiting_user", "waiting_bg"))
                )
                pending_alerts.append((head, detail, is_step1_pause))

        return pending_alerts
