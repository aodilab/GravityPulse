"""Live execution phase & task-checklist detector for safe mid-run window-switch guidance."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading

_CODE_EDIT_TOOLS = frozenset({
    "edit_file",
    "write_file",
    "replace_file_content",
    "multi_replace_file_content",
    "write_to_file",
})
_READ_TOOLS = frozenset({
    "read_file",
    "grep_search",
    "find_by_name",
    "list_dir",
    "view_file",
    "view_file_outline",
    "view_code_item",
})
_VERIFY_TOOLS = frozenset({"run_command", "invoke_subagent"})
_WAIT_USER_TOOLS = frozenset({"ask_question"})
_HANDOVER_RE = re.compile(
    r"(新建对话|新开对话|新开一个|开新对话|开新窗|换.{0,4}新.{0,3}(?:窗|对话)|写.{0,6}交接|该交接了"
    r"|new\s+(?:chat|window|conversation)|handover|handoff)",
    re.IGNORECASE,
)
_DONE_TASK_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]")
_OPEN_TASK_RE = re.compile(r"^\s*[-*]\s+\[[\s/]\]")

_PHASE_LOCK = threading.Lock()
_TRANSCRIPT_PHASE_CACHE: dict[Path, tuple[int, int, str, int, int, bool]] = {}
_TASK_MD_CACHE: dict[Path, tuple[int, int, int, int]] = {}


@dataclass(frozen=True)
class DialogWorkPhase:
    """Inferred execution stage of the current turn plus `task.md` checklist progress."""

    phase: str = ""  # "idle" | "waiting_user" | "waiting_bg" | "new_stage" | "exploring" | "editing" | "verifying" | ""
    code_edits: int = 0
    read_calls: int = 0
    task_done: int = 0
    task_remaining: int = 0
    is_handover_turn: bool = False


def _unquote_arg(val: object) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            decoded = json.loads(s)
            if isinstance(decoded, str):
                return decoded
        except json.JSONDecodeError:
            return s[1:-1]
    return s


def _is_workspace_code_edit(tool_call: dict) -> bool:
    name = str(tool_call.get("name") or "")
    if name not in _CODE_EDIT_TOOLS:
        return False
    args = tool_call.get("args")
    if not isinstance(args, dict):
        args = tool_call.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if not isinstance(args, dict):
        return True
    if "ArtifactMetadata" in args and args.get("ArtifactMetadata"):
        return False
    raw_path = _unquote_arg(args.get("Path") or args.get("TargetFile") or args.get("path") or "")
    norm_path = raw_path.replace("\\", "/").lower()
    if "/.gemini/antigravity/brain/" in norm_path:
        return False
    return True


def _read_task_progress(task_md_path: Path) -> tuple[int, int]:
    try:
        st = task_md_path.stat()
    except OSError:
        return 0, 0
    with _PHASE_LOCK:
        cached = _TASK_MD_CACHE.get(task_md_path)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2], cached[3]
    done = 0
    remaining = 0
    try:
        with task_md_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if _DONE_TASK_RE.match(line):
                    done += 1
                elif _OPEN_TASK_RE.match(line):
                    remaining += 1
    except OSError:
        return 0, 0
    with _PHASE_LOCK:
        _TASK_MD_CACHE[task_md_path] = (st.st_mtime_ns, st.st_size, done, remaining)
    return done, remaining


def _parse_transcript_turn_phase(transcript_path: Path) -> tuple[str, int, int, bool]:
    try:
        st = transcript_path.stat()
    except OSError:
        return "", 0, 0, False
    with _PHASE_LOCK:
        cached = _TRANSCRIPT_PHASE_CACHE.get(transcript_path)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2], cached[3], cached[4], cached[5]

    user_turns_seen = 0
    is_handover_turn = False
    code_edits = 0
    stage_edits = 0
    read_calls = 0
    saw_verify_after_edit = False
    completed_stages = 0
    pending_wait_user = False
    try:
        with transcript_path.open("r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                stype = item.get("type")
                is_user = item.get("source") == "USER_EXPLICIT" or stype == "USER_INPUT"
                if is_user:
                    user_turns_seen += 1
                    is_handover_turn = user_turns_seen >= 2 and bool(_HANDOVER_RE.search(str(item.get("content") or "")))
                    code_edits = 0
                    stage_edits = 0
                    read_calls = 0
                    saw_verify_after_edit = False
                    completed_stages = 0
                    pending_wait_user = False
                    continue
                if stype == "GENERIC":
                    pending_wait_user = False
                    continue
                if stype != "PLANNER_RESPONSE":
                    continue
                pending_wait_user = False
                tcs = item.get("tool_calls")
                if not isinstance(tcs, list) or not tcs:
                    continue
                step_has_edit = False
                step_has_read = False
                step_has_verify = False
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    tname = str(tc.get("name") or "")
                    if tname in _WAIT_USER_TOOLS:
                        pending_wait_user = True
                    elif _is_workspace_code_edit(tc):
                        code_edits += 1
                        stage_edits += 1
                        step_has_edit = True
                    elif tname in _READ_TOOLS:
                        read_calls += 1
                        step_has_read = True
                    elif tname in _VERIFY_TOOLS:
                        step_has_verify = True
                if step_has_edit:
                    saw_verify_after_edit = False
                elif stage_edits > 0 and step_has_verify:
                    saw_verify_after_edit = True
                elif saw_verify_after_edit and step_has_read:
                    completed_stages += 1
                    stage_edits = 0
                    saw_verify_after_edit = False
    except OSError:
        return "", 0, 0, False

    if code_edits > 0:
        is_handover_turn = False

    if pending_wait_user:
        phase = "waiting_user"
    elif stage_edits > 0:
        phase = "verifying" if saw_verify_after_edit else "editing"
    elif completed_stages > 0:
        phase = "new_stage"
    else:
        phase = "exploring"

    with _PHASE_LOCK:
        _TRANSCRIPT_PHASE_CACHE[transcript_path] = (st.st_mtime_ns, st.st_size, phase, code_edits, read_calls, is_handover_turn)
    return phase, code_edits, read_calls, is_handover_turn


def detect_dialog_work_phase(
    antigravity_dir: Path,
    conversation_id: str,
    is_running: bool,
    self_running: bool = True,
    active_subtasks: int = 0,
) -> DialogWorkPhase:
    """Detect a dialog's live execution phase (`waiting_user`/`waiting_bg`/`new_stage`/`exploring`/`editing`/`verifying`/`idle`)."""
    brain_dir = antigravity_dir / "brain" / conversation_id
    done, remaining = _read_task_progress(brain_dir / "task.md")
    tp = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
    phase, edits, reads, is_handover = _parse_transcript_turn_phase(tp)
    if not is_running:
        return DialogWorkPhase(
            phase="idle", task_done=done, task_remaining=remaining, is_handover_turn=is_handover,
        )
    if not self_running and active_subtasks > 0 and not is_handover:
        phase = "waiting_bg"
    return DialogWorkPhase(
        phase=phase,
        code_edits=edits,
        read_calls=reads,
        task_done=done,
        task_remaining=remaining,
        is_handover_turn=is_handover,
    )


def phase_short_tag(phase: str) -> str:
    """Compact Chinese label for table subtext."""
    return {
        "waiting_user": "待确认",
        "waiting_bg": "等后台",
        "new_stage": "新阶段起步",
        "exploring": "读码中",
        "editing": "改码中",
        "verifying": "验证中",
    }.get(phase, "")


def build_running_zone_copy(
    model_name: str,
    ctx_str: str,
    near_str: str,
    run_limit_str: str,
    phase: str,
    task_done: int,
    task_remaining: int,
) -> tuple[str, str]:
    """Return `(short_reason, notify_detail)` when a running dialog enters the breakeven switch zone."""
    del model_name
    ptag = phase_short_tag(phase)
    short = f"换窗区 {near_str}-{run_limit_str}" + (f" · {ptag}" if ptag else "")
    if phase == "waiting_user":
        return (
            f"待确认 · 换窗区 {near_str}-{run_limit_str}",
            f"{ctx_str} 待确认 · 长任务换新，短任务可续",
        )
    if phase == "waiting_bg":
        return (
            f"等后台 · 换窗区 {near_str}-{run_limit_str}",
            f"{ctx_str} 等后台 · 长任务换新，短任务可续",
        )
    if phase == "new_stage":
        return short, f"当前 {ctx_str}，新阶段未改码，可直接暂停换新。"
    if phase == "exploring":
        return short, f"当前 {ctx_str}（区间 {near_str}–{run_limit_str}），读码中，出结论后可换新。"
    if phase == "editing":
        return short, f"当前 {ctx_str}，联动改码中勿硬停，等改完再换新。"
    if phase == "verifying":
        if task_remaining == 1 or (task_done > 0 and task_remaining == 0):
            return short, f"当前 {ctx_str}，收尾验证中，等跑完再换新。"
        rem_note = f"（剩 {task_remaining} 项）" if task_remaining >= 2 else ""
        return short, f"当前 {ctx_str}，验证中{rem_note}，验完可换新。"
    return short, f"已入 {near_str}–{run_limit_str} 换窗区（{ctx_str}），收尾可换新。"


def build_running_switch_copy(
    model_name: str,
    ctx_str: str,
    limit_str: str,
    safe_str: str,
    compaction_count: int,
    phase: str,
    task_done: int,
    task_remaining: int,
    is_compaction: bool,
    is_critical: bool = False,
    safe_pct: int = 0,
) -> tuple[str, str]:
    """Return `(short_reason, notify_detail)` with phase-aware interruption guidance for running dialogs."""
    del model_name
    if is_critical and not is_compaction:
        pct_s = f"（{safe_pct}%）" if safe_pct > 0 else ""
        if phase == "waiting_user":
            return "快压缩了 · 待确认立即换", f"{ctx_str}{pct_s} 待确认 · 临近压缩，请换新"
        if phase == "waiting_bg":
            return "快压缩了 · 后台中立即换", f"{ctx_str}{pct_s} 等后台 · 临近压缩，可交接换新"
        if phase == "new_stage":
            return "快压缩了 · 立即停并换窗", f"{ctx_str}{pct_s} · 临近压缩，请立即换新"
        if safe_pct >= 88:
            return "即将压缩 · 立即停并交接", f"{ctx_str}{pct_s} · 即将压缩，请停下交接换新"
        return "快压缩了 · 建议暂停交接", f"{ctx_str}{pct_s} · 临近压缩，建议暂停交接换新"

    head = f"已压缩 {compaction_count} 次（{ctx_str}）" if is_compaction else f"已达 {ctx_str}"
    prefix = f"已压缩 {compaction_count} 次" if is_compaction else f"超 {limit_str}"

    if phase == "waiting_user":
        return f"{prefix} · 待确认直接换窗", f"{head}，正等你确认，可直接换新。"
    if phase == "waiting_bg":
        return f"{prefix} · 等后台可直接换", f"{head}，主对话已停等后台，可直接发交接换新。"
    if phase == "new_stage":
        return f"{prefix} · 新阶段可直接停", f"{head}，新阶段未改码，可直接暂停换新。"
    if phase == "exploring":
        return f"{prefix} · 读码排查勿硬停", f"{head}，读码排查中勿硬停，定位后可写交接单。"
    if phase == "editing":
        return f"{prefix} · 改码中勿硬停", f"{head}，改码中勿硬停，等改完立刻写交接单。"
    if phase == "verifying":
        if task_remaining == 1 or (task_done > 0 and task_remaining == 0):
            return f"{prefix} · 收尾验证等跑完", f"{head}，收尾验证中，等跑完再换新。"
        rem_note = f"（剩 {task_remaining} 项）" if task_remaining >= 2 else ""
        return f"{prefix} · 验证期可交接", f"{head}，验证中{rem_note}，跑完或直接暂停写交接单。"
    return f"{prefix} · 临近压缩可交接", f"{head}（临近 {safe_str}），阶段落盘后可写交接单换新。"
