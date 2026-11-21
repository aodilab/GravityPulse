"""Deduplicating state machine for structural (2-tier +50K) and idle cold-cache window-switch notifications."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from config.settings import (
    STRUCTURAL_MAX_ALERTS_PER_CYCLE,
    STRUCTURAL_REALERT_DELTA_TOKENS,
)
from src.core.models import format_tokens


@dataclass
class _DialogAlertState:
    switch_kind: str
    baseline_compactions: int
    baseline_user_turns: int
    baseline_post_finish_turns: int
    baseline_user_ts: float
    baseline_context_tokens: int
    alert_count: int = 1


class SwitchAlertTracker:
    """Fires structural alerts up to twice per round (+50K context growth) and cold-cache alerts once per idle cycle."""

    def __init__(
        self,
        realert_delta_tokens: int = STRUCTURAL_REALERT_DELTA_TOKENS,
        max_structural_alerts: int = STRUCTURAL_MAX_ALERTS_PER_CYCLE,
    ) -> None:
        self.seeded: bool = False
        self._realert_delta_tokens: int = max(1, int(realert_delta_tokens))
        self._max_structural_alerts: int = max(1, int(max_structural_alerts))
        self._states: dict[str, _DialogAlertState] = {}

    def process(self, dialogs: Iterable[dict], notify_enabled: bool = True) -> tuple[str, str] | None:
        dialog_list = list(dialogs)
        if not self.seeded:
            for d in dialog_list:
                if d.get("advice_key") == "switch":
                    cid = str(d.get("conversation_id", ""))
                    if cid:
                        self._states[cid] = _DialogAlertState(
                            switch_kind=str(d.get("switch_kind") or "structural"),
                            baseline_compactions=int(d.get("compaction_count", 0) or 0),
                            baseline_user_turns=int(d.get("user_turn_count", 0) or 0),
                            baseline_post_finish_turns=int(d.get("post_finish_turn_count", 0) or 0),
                            baseline_user_ts=float(d.get("last_user_input_ts", 0.0) or 0.0),
                            baseline_context_tokens=int(d.get("context_tokens", 0) or 0),
                            alert_count=1,
                        )
            self.seeded = True
            return None

        pending_alert: tuple[str, str] | None = None
        for d in dialog_list:
            cid = str(d.get("conversation_id", ""))
            if not cid:
                continue
            advice_key = str(d.get("advice_key", ""))
            cache_state = str(d.get("cache_state", ""))
            is_running = bool(d.get("is_running"))
            compactions = int(d.get("compaction_count", 0) or 0)
            user_turns = int(d.get("user_turn_count", 0) or 0)
            post_finish_turns = int(d.get("post_finish_turn_count", 0) or 0)
            last_user_ts = float(d.get("last_user_input_ts", 0.0) or 0.0)
            ctx_tokens = int(d.get("context_tokens", 0) or 0)

            prev = self._states.get(cid)
            if advice_key != "switch":
                if prev is not None and (
                    advice_key == "ok" or (prev.switch_kind == "cache" and cache_state in ("active", "fresh"))
                ):
                    self._states.pop(cid, None)
                continue

            switch_kind = str(d.get("switch_kind") or "structural")
            idle_sec = int(d.get("idle_seconds", 0) or 0)

            # Cold-cache alerts remain idle-only; structural threshold alerts fire immediately even while running.
            if is_running and switch_kind != "structural":
                continue

            is_followup = False
            followup_delta = 0
            next_alert_count = 1
            if prev is not None:
                if prev.switch_kind == "cache" and switch_kind == "cache":
                    continue

                has_more_compaction = switch_kind == "structural" and compactions > prev.baseline_compactions
                has_new_turn = (
                    post_finish_turns > prev.baseline_post_finish_turns
                    or user_turns > prev.baseline_user_turns
                    or (last_user_ts > 0.0 and prev.baseline_user_ts > 0.0 and last_user_ts > prev.baseline_user_ts + 0.5)
                )
                upgraded_to_structural = prev.switch_kind != "structural" and switch_kind == "structural"
                is_new_cycle = has_more_compaction or has_new_turn or upgraded_to_structural

                if not is_new_cycle:
                    ctx_delta = ctx_tokens - prev.baseline_context_tokens
                    has_ctx_growth = (
                        switch_kind == "structural"
                        and prev.switch_kind == "structural"
                        and prev.alert_count < self._max_structural_alerts
                        and ctx_delta >= self._realert_delta_tokens
                    )
                    if not has_ctx_growth:
                        continue
                    is_followup = True
                    followup_delta = ctx_delta
                    next_alert_count = prev.alert_count + 1

            if switch_kind == "structural":
                if not is_running and idle_sec > 600:
                    continue
            elif idle_sec > 2700:
                continue

            if notify_enabled and pending_alert is not None:
                continue

            self._states[cid] = _DialogAlertState(
                switch_kind=switch_kind,
                baseline_compactions=compactions,
                baseline_user_turns=user_turns,
                baseline_post_finish_turns=post_finish_turns,
                baseline_user_ts=last_user_ts,
                baseline_context_tokens=ctx_tokens,
                alert_count=next_alert_count,
            )
            if notify_enabled:
                head = f"{d.get('workspace', '')} · {d.get('title', '')} ({d.get('model_label', '')})"
                base_detail = str(d.get("notify_detail") or d.get("advice_reason") or "当前上下文已达换窗阈值，建议开启新对话。")
                if is_followup:
                    delta_fmt = format_tokens(followup_delta)
                    ctx_fmt = format_tokens(ctx_tokens)
                    prefix = (
                        f"【二次提醒 · 续增 +{delta_fmt}】"
                        if ctx_fmt in base_detail
                        else f"【二次提醒 · 续增 +{delta_fmt}，当前 {ctx_fmt}】"
                    )
                    detail = f"{prefix}{base_detail}"
                else:
                    detail = base_detail
                pending_alert = (head, detail)

        return pending_alert
