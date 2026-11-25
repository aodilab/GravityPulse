"""Tray action handlers and hover rules tooltip mixin for GravityPulse."""
from __future__ import annotations

import threading
import tkinter as tk

from config.settings import FONT_UI
from src.core.watcher import (
    ensure_watcher_running,
    install_startup_registry,
    is_startup_installed,
    uninstall_startup_registry,
)
from src.ui.i18n import save_ui_language, tr


class TrayActionsMixin:
    """Provides rules hover tooltip, language toggle, and tray menu actions."""

    def _show_tooltip(self, _event: tk.Event) -> None:
        if self._tooltip_win is not None:
            return
        tw = tk.Toplevel(self.root)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        tw.configure(bg="#1c1c1e")

        rx = self.help_canvas.winfo_rootx()
        ry = self.help_canvas.winfo_rooty() + self.help_canvas.winfo_height() + self.px(8)
        box = tk.Frame(tw, bg="#1c1c1e", padx=self.px(14), pady=self.px(12), highlightbackground="#3a3a3c", highlightthickness=1)
        box.pack(fill=tk.BOTH, expand=True)

        tk.Label(box, text=tr("rules_title", self._lang), bg="#1c1c1e", fg="#ffffff", font=(FONT_UI, 9, "bold")).pack(anchor="w", pady=(0, self.px(4)))
        c_mult_s = f"{self._quota_cfg.carbon_multiplier:g}x"
        n_mult_s = f"{self._quota_cfg.neon_multiplier:g}x"
        rules = tr("rules_body", self._lang, c_mult=c_mult_s, n_mult=n_mult_s)
        tk.Label(box, text=rules, bg="#1c1c1e", fg="#e5e5ea", justify="left", font=(FONT_UI, 9)).pack(anchor="w")

        tw.update_idletasks()
        tip_w = tw.winfo_reqwidth()
        sw = self.root.winfo_screenwidth()
        x = max(8, min(sw - tip_w - 8, rx + self.help_canvas.winfo_width() - tip_w))
        tw.geometry(f"+{x}+{ry}")
        self._tooltip_win = tw

    def _hide_tooltip(self, _event: tk.Event | None = None) -> None:
        if self._tooltip_win is not None:
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win = None

    def _get_tray_menu_state(self) -> dict[str, object]:
        rel = getattr(self, "_available_release", None)
        return {
            "lang": self._lang,
            "startup_enabled": is_startup_installed(),
            "available_version": rel.version if rel is not None else "",
        }

    def _toggle_language(self) -> None:
        self._lang = save_ui_language("en" if self._lang == "zh" else "zh")
        self._hide_tooltip()
        self._apply_language_static_ui()
        if self._current_payload is not None:
            self._apply_payload(self._current_payload, preserve_scroll=True)

    def _handle_tray_action(self, cmd: str) -> None:
        if cmd == "toggle_lang":
            self._toggle_language()
        elif cmd == "toggle_startup":
            try:
                if is_startup_installed():
                    uninstall_startup_registry()
                    self._show_switch_toast(
                        tr("toast_startup_off_head", self._lang), tr("toast_startup_off_body", self._lang),
                        badge_text=tr("toast_info_badge", self._lang), badge_color="#34c759",
                    )
                else:
                    install_startup_registry()
                    ensure_watcher_running()
                    self._show_switch_toast(
                        tr("toast_startup_on_head", self._lang), tr("toast_startup_on_body", self._lang),
                        badge_text=tr("toast_info_badge", self._lang), badge_color="#34c759",
                    )
            except Exception as exc:
                self._show_switch_toast(
                    tr("toast_action_err_head", self._lang), str(exc),
                    badge_text=tr("toast_info_badge", self._lang), badge_color="#ff6961",
                )
        elif cmd == "create_shortcut":
            lang = self._lang

            def _make() -> None:
                try:
                    from scripts.icon_builder import create_desktop_shortcut

                    create_desktop_shortcut()
                    self.root.after(
                        0, lambda: self._show_switch_toast(
                            tr("toast_shortcut_head", lang), tr("toast_shortcut_body", lang),
                            badge_text=tr("toast_info_badge", lang), badge_color="#34c759",
                        ),
                    )
                except Exception as exc:
                    err = str(exc)
                    self.root.after(
                        0, lambda: self._show_switch_toast(
                            tr("toast_action_err_head", lang), err,
                            badge_text=tr("toast_info_badge", lang), badge_color="#ff6961",
                        ),
                    )

            threading.Thread(target=_make, daemon=True).start()
