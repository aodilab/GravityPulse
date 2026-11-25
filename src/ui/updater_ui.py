"""Background GitHub Releases check scheduling and bilingual cross-platform update dialog mixin."""
from __future__ import annotations

from pathlib import Path
import threading
import tkinter as tk
import webbrowser

from config.settings import (
    APP_VERSION,
    BG_CARD,
    BG_PAGE,
    FONT_UI,
    GITHUB_RELEASES_URL,
    ICON_PATH,
    INK,
    INK_2,
    INK_3,
    IS_FROZEN,
    IS_MACOS,
    IS_WINDOWS,
    LINE_HEX,
    PROJECT_ROOT,
    UPDATE_CHECK_INTERVAL_MS,
    UPDATE_STARTUP_DELAY_MS,
)
from src.core.updater import (
    ReleaseInfo,
    UpdateApplyResult,
    UpdateCheckResult,
    apply_update,
    check_for_update,
    inspect_git_worktree,
)
from src.core.updater_pkg import open_macos_dmg
from src.ui.i18n import tr
from src.ui.primitives import make_dialog_button


class UpdaterUiMixin:
    """Schedules silent GitHub Releases checks and renders the bilingual one-click update modal."""

    def _init_updater(self) -> None:
        self._update_after_id: str | None = None
        self._update_win: tk.Toplevel | None = None
        self._update_apply_btn: tk.Widget | None = None
        self._update_later_btn: tk.Widget | None = None
        self._update_checking: bool = False
        self._update_applying: bool = False
        self._available_release: ReleaseInfo | None = None
        self._last_update_check: UpdateCheckResult | None = None
        if getattr(self._quota_cfg, "auto_check_update", True):
            self._schedule_update_check(UPDATE_STARTUP_DELAY_MS)

    def _schedule_update_check(self, delay_ms: int = UPDATE_CHECK_INTERVAL_MS) -> None:
        self._cancel_after("_update_after_id")
        if not getattr(self._quota_cfg, "auto_check_update", True):
            return
        self._update_after_id = self.root.after(delay_ms, self._on_scheduled_update_check)

    def _on_update_config_changed(self) -> None:
        if getattr(self._quota_cfg, "auto_check_update", True):
            if self._update_after_id is None:
                self._schedule_update_check(UPDATE_STARTUP_DELAY_MS)
        else:
            self._cancel_after("_update_after_id")

    def _close_update_window(self) -> None:
        win = getattr(self, "_update_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.destroy()
            except Exception:
                pass
            self._update_win = None
            self._update_apply_btn = None
            self._update_later_btn = None

    def _stop_updater(self) -> None:
        self._cancel_after("_update_after_id")
        self._close_update_window()

    def _on_scheduled_update_check(self) -> None:
        self._update_after_id = None
        self._schedule_update_check(UPDATE_CHECK_INTERVAL_MS)
        self._trigger_update_check(manual=False)

    def _trigger_update_check(self, manual: bool = False) -> None:
        if self._update_checking or self._update_applying:
            return
        self._update_checking = True
        lang = getattr(self, "_lang", "zh")

        def worker() -> None:
            res = check_for_update(current_version=APP_VERSION, lang=lang)
            try:
                self.root.after(0, lambda r=res, m=manual: self._on_update_check_done(r, manual=m))
            except Exception:
                self._update_checking = False

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_check_done(self, result: UpdateCheckResult, manual: bool = False) -> None:
        self._update_checking = False
        self._last_update_check = result
        lang = getattr(self, "_lang", "zh")
        if result.status == "available" and result.release is not None:
            self._available_release = result.release
            self._set_update_btn_visible(True, tr("upd_pill", lang, ver=result.release.version))
        elif result.status == "up_to_date":
            self._available_release = None
            self._set_update_btn_visible(False)

        win = self._update_win
        if win is not None and win.winfo_exists():
            self._render_update_dialog_body(result)
        elif manual and result.status != "up_to_date":
            self._open_update_dialog(force_check=False)

    def _open_update_dialog(self, force_check: bool = False) -> None:
        if not self._window_visible:
            self.show_window()
        if self._update_win is not None and self._update_win.winfo_exists():
            self._update_win.lift()
            if force_check and not self._update_checking and not self._update_applying:
                self._render_update_dialog_checking()
                self._trigger_update_check(manual=True)
            return

        lang = getattr(self, "_lang", "zh")
        win = tk.Toplevel(self.root)
        win.title(tr("upd_win_title", lang))
        win.configure(bg=BG_PAGE)
        win.resizable(False, False)
        if IS_WINDOWS and ICON_PATH.exists():
            try:
                win.iconbitmap(str(ICON_PATH))
            except tk.TclError:
                pass
        root_shown = str(self.root.state()) != "withdrawn"
        if root_shown:
            win.transient(self.root)
        else:
            win.withdraw()
        win.protocol("WM_DELETE_WINDOW", self._on_update_win_close)

        pad = self.px(18)
        self._update_card = tk.Frame(
            win, bg=BG_CARD, padx=pad, pady=pad, highlightbackground=LINE_HEX, highlightthickness=1, width=self.px(440),
        )
        self._update_card.pack(padx=self.px(14), pady=self.px(14))
        self._update_win = win

        if force_check or (self._last_update_check is None and self._available_release is None):
            self._render_update_dialog_checking()
            self._trigger_update_check(manual=True)
        elif self._available_release is not None:
            self._render_update_dialog_body(
                UpdateCheckResult(status="available", current_version=APP_VERSION, release=self._available_release)
            )
        elif self._last_update_check is not None:
            self._render_update_dialog_body(self._last_update_check)

        self._center_update_dialog(win)
        if root_shown:
            try:
                win.grab_set()
            except tk.TclError:
                pass

    def _on_update_win_close(self) -> None:
        if self._update_applying:
            return
        self._close_update_window()

    def _center_update_dialog(self, win: tk.Toplevel) -> None:
        win.update_idletasks()
        px_x = self.root.winfo_rootx() + (self.win_w - win.winfo_reqwidth()) // 2
        px_y = self.root.winfo_rooty() + (self.win_h - win.winfo_reqheight()) // 2
        win.geometry(f"+{max(0, px_x)}+{max(0, px_y)}")

    def _clear_update_card(self) -> None:
        self._update_apply_btn = None
        self._update_later_btn = None
        for child in self._update_card.winfo_children():
            child.destroy()

    def _render_update_dialog_checking(self) -> None:
        self._clear_update_card()
        card = self._update_card
        lang = getattr(self, "_lang", "zh")
        tk.Label(card, text=tr("upd_checking_title", lang), bg=BG_CARD, fg=INK, font=(FONT_UI, 11, "bold")).pack(anchor="w")
        tk.Label(
            card, text=tr("upd_checking_body", lang, ver=APP_VERSION),
            bg=BG_CARD, fg=INK_2, font=(FONT_UI, 9), justify="left", wraplength=self.px(400),
        ).pack(anchor="w", pady=(self.px(6), self.px(14)))
        btn_row = tk.Frame(card, bg=BG_CARD)
        btn_row.pack(fill=tk.X)
        make_dialog_button(
            btn_row, text=tr("upd_btn_close", lang), command=self._on_update_win_close,
            bg=BG_PAGE, fg=INK_2, padx=self.px(14), pady=self.px(5), font=(FONT_UI, 9),
        ).pack(side=tk.RIGHT)
        if self._update_win is not None and self._update_win.winfo_exists():
            self._center_update_dialog(self._update_win)

    def _render_update_dialog_body(self, result: UpdateCheckResult) -> None:
        self._clear_update_card()
        card = self._update_card
        lang = getattr(self, "_lang", "zh")

        if result.status == "up_to_date":
            tk.Label(card, text=tr("upd_latest_title", lang), bg=BG_CARD, fg=INK, font=(FONT_UI, 11, "bold")).pack(anchor="w")
            tk.Label(
                card, text=tr("upd_latest_body", lang, ver=result.current_version),
                bg=BG_CARD, fg=INK_2, font=(FONT_UI, 9),
            ).pack(anchor="w", pady=(self.px(6), self.px(14)))
            btn_row = tk.Frame(card, bg=BG_CARD)
            btn_row.pack(fill=tk.X)
            make_dialog_button(
                btn_row, text=tr("upd_btn_close", lang), command=self._on_update_win_close,
                bg=INK, fg="#ffffff", padx=self.px(16), pady=self.px(5), font=(FONT_UI, 9, "bold"),
            ).pack(side=tk.RIGHT)
        elif result.status == "error" or result.release is None:
            tk.Label(card, text=tr("upd_fail_title", lang), bg=BG_CARD, fg=INK, font=(FONT_UI, 11, "bold")).pack(anchor="w")
            tk.Label(
                card, text=result.error or tr("upd_fail_default", lang),
                bg=BG_CARD, fg=INK_2, font=(FONT_UI, 9), justify="left", wraplength=self.px(400),
            ).pack(anchor="w", pady=(self.px(6), self.px(14)))
            btn_row = tk.Frame(card, bg=BG_CARD)
            btn_row.pack(fill=tk.X)
            make_dialog_button(
                btn_row, text=tr("upd_btn_close", lang), command=self._on_update_win_close,
                bg=BG_PAGE, fg=INK_2, padx=self.px(14), pady=self.px(5), font=(FONT_UI, 9),
            ).pack(side=tk.LEFT)
            make_dialog_button(
                btn_row, text=tr("upd_btn_retry", lang),
                command=lambda: (self._render_update_dialog_checking(), self._trigger_update_check(manual=True)),
                bg=INK, fg="#ffffff", padx=self.px(16), pady=self.px(5), font=(FONT_UI, 9, "bold"),
            ).pack(side=tk.RIGHT)
            make_dialog_button(
                btn_row, text=tr("upd_btn_open_page", lang), command=lambda: webbrowser.open(GITHUB_RELEASES_URL),
                bg=BG_PAGE, fg=INK_2, padx=self.px(12), pady=self.px(5), font=(FONT_UI, 9),
            ).pack(side=tk.RIGHT, padx=(0, self.px(8)))
        else:
            self._render_available_release(card, result.release)

        if self._update_win is not None and self._update_win.winfo_exists():
            self._center_update_dialog(self._update_win)

    def _render_available_release(self, card: tk.Frame, rel: ReleaseInfo) -> None:
        lang = getattr(self, "_lang", "zh")
        tk.Label(card, text=tr("upd_avail_title", lang, ver=rel.version), bg=BG_CARD, fg=INK, font=(FONT_UI, 11, "bold")).pack(anchor="w")
        sub_key = "upd_avail_sub_mac" if (IS_FROZEN and IS_MACOS) else "upd_avail_sub"
        tk.Label(
            card, text=tr(sub_key, lang, cur=APP_VERSION),
            bg=BG_CARD, fg=INK_3, font=(FONT_UI, 8), justify="left", wraplength=self.px(400),
        ).pack(anchor="w", pady=(self.px(3), self.px(10)))

        if rel.notes:
            notes_box = tk.Text(
                card, width=54, height=7, bg=BG_PAGE, fg=INK, bd=0,
                highlightbackground=LINE_HEX, highlightthickness=1,
                font=(FONT_UI, 8), wrap=tk.WORD, padx=self.px(8), pady=self.px(6),
            )
            notes_box.insert("1.0", rel.notes)
            notes_box.configure(state="disabled")
            notes_box.pack(fill=tk.X, pady=(0, self.px(10)))

        blocked_msg = ""
        if IS_FROZEN:
            if not rel.download_url:
                blocked_msg = tr("upd_blocked_no_pkg", lang)
        else:
            is_git, is_dirty, git_ok = inspect_git_worktree(PROJECT_ROOT)
            if is_git and not git_ok:
                blocked_msg = tr("upd_blocked_no_git", lang)
            elif is_git and is_dirty:
                blocked_msg = tr("upd_blocked_dirty", lang)

        status_var = tk.StringVar(value=blocked_msg)
        status_fg = "#b25000" if blocked_msg else INK_2
        status_lbl = tk.Label(
            card, textvariable=status_var, bg=BG_CARD, fg=status_fg,
            font=(FONT_UI, 8), anchor="w", justify="left", wraplength=self.px(400),
        )
        status_lbl.pack(fill=tk.X, pady=(0, self.px(10)))

        btn_row = tk.Frame(card, bg=BG_CARD)
        btn_row.pack(fill=tk.X)
        later_btn = make_dialog_button(
            btn_row, text=tr("upd_btn_later", lang), command=self._on_update_win_close,
            bg=BG_PAGE, fg=INK_2, padx=self.px(14), pady=self.px(5), font=(FONT_UI, 9),
        )
        later_btn.pack(side=tk.LEFT)

        apply_label = tr("upd_btn_apply_mac", lang) if (IS_FROZEN and IS_MACOS) else tr("upd_btn_apply", lang)
        apply_btn = make_dialog_button(
            btn_row, text=apply_label,
            bg=INK if not blocked_msg else "#c7c7cc", fg="#ffffff",
            padx=self.px(16), pady=self.px(5), font=(FONT_UI, 9, "bold"),
            state="disabled" if blocked_msg else "normal",
            cursor="arrow" if blocked_msg else "hand2",
        )
        apply_btn.pack(side=tk.RIGHT)
        self._update_apply_btn = apply_btn
        self._update_later_btn = later_btn

        open_btn = make_dialog_button(
            btn_row, text=tr("upd_btn_open_page", lang), command=lambda u=rel.html_url: webbrowser.open(u),
            bg=BG_PAGE, fg=INK_2, padx=self.px(12), pady=self.px(5), font=(FONT_UI, 9),
        )
        open_btn.pack(side=tk.RIGHT, padx=(0, self.px(8)))

        def on_apply() -> None:
            if self._update_applying:
                return
            self._update_applying = True
            apply_btn.configure(state="disabled", text=tr("upd_btn_applying", lang), bg="#6e6e73", cursor="arrow")
            later_btn.configure(state="disabled", cursor="arrow")
            status_lbl.configure(fg=INK_2)
            status_var.set(tr("upd_preparing", lang))

            def post_status(msg: str) -> None:
                try:
                    self.root.after(0, lambda m=msg: status_var.set(m))
                except Exception:
                    pass

            def finish(res: UpdateApplyResult) -> None:
                self._update_applying = False
                if not apply_btn.winfo_exists():
                    return
                if res.ok:
                    status_lbl.configure(fg="#1b6d2f")
                    status_var.set(res.message)
                    if res.needs_quit:
                        self.root.after(350, self._quit_completely)
                    elif res.needs_restart:
                        self.root.after(350, lambda: self._restart_resident_app(show_ui=True))
                    else:
                        later_btn.configure(state="normal", text=tr("upd_btn_close", lang), cursor="hand2")
                        if res.method == "macos_dmg" and res.downloaded_path:
                            dmg_path = res.downloaded_path

                            def on_reopen_dmg() -> None:
                                if not open_macos_dmg(dmg_path) and not Path(dmg_path).is_file():
                                    on_apply()

                            apply_btn.configure(
                                state="normal",
                                text=tr("upd_btn_dmg_opened", lang),
                                bg=INK,
                                cursor="hand2",
                                command=on_reopen_dmg,
                            )
                        else:
                            done_key = "upd_btn_dmg_opened" if res.method == "macos_dmg" else "upd_btn_done"
                            apply_btn.configure(
                                state="disabled",
                                text=tr(done_key, lang),
                                bg="#c7c7cc",
                                cursor="arrow",
                            )
                else:
                    status_lbl.configure(fg="#c9342b")
                    status_var.set(res.message)
                    apply_btn.configure(state="normal", text=tr("upd_btn_retry_apply", lang), bg=INK, cursor="hand2")
                    later_btn.configure(state="normal", cursor="hand2")

            def worker() -> None:
                res = apply_update(rel, project_root=PROJECT_ROOT, current_version=APP_VERSION, status_cb=post_status, lang=lang)
                try:
                    self.root.after(0, lambda r=res: finish(r))
                except Exception:
                    self._update_applying = False

            threading.Thread(target=worker, daemon=True).start()

        apply_btn.configure(command=on_apply)
