"""Native macOS menu bar status item (NSStatusBar via PyObjC) running on Tkinter's Cocoa main loop."""
from __future__ import annotations

import io
from pathlib import Path
import queue
from typing import Callable

from PIL import Image, ImageDraw

from src.ui.i18n import tr


def render_macos_menubar_template_png(size: int = 36) -> bytes:
    """Render a crisp 2x monochrome clock glyph PNG for macOS menu bar template icons."""
    ss = 2
    px = size * ss
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = px / 2.0
    r = px * 0.37
    lw = max(3, int(round(px * 0.085)))
    ink = (0, 0, 0, 255)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ink, width=lw)
    d.line([cx, cy, cx, cy - r * 0.56], fill=ink, width=lw)
    d.line([cx, cy, cx + r * 0.46, cy + r * 0.24], fill=ink, width=lw)
    dot_r = px * 0.095
    dx = cx + r * 0.78
    dy = cy + r * 0.78
    d.ellipse([dx - dot_r, dy - dot_r, dx + dot_r, dy + dot_r], fill=ink)
    out = img.resize((size, size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def activate_macos_app() -> None:
    """Bring the macOS Cocoa application to the foreground cleanly."""
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


def configure_macos_root(app: object) -> None:
    """Apply macOS Tk scaling, shortcuts, Dock reopen handler, and native top app menu."""
    import tkinter as tk

    root = app.root  # type: ignore[attr-defined]
    try:
        root.tk.call("tk", "scaling", 4.0 / 3.0)
    except tk.TclError:
        pass
    root.bind("<Command-w>", lambda _e: app._on_close_to_tray())  # type: ignore[attr-defined]
    root.bind("<Command-q>", lambda _e: app._quit_completely())  # type: ignore[attr-defined]
    root.bind("<Command-r>", lambda _e: app.refresh(force_recompute=True, schedule_next=True))  # type: ignore[attr-defined]
    try:
        root.createcommand("::tk::mac::ReopenApplication", app.show_window)  # type: ignore[attr-defined]
        root.createcommand("::tk::mac::Quit", app._quit_completely)  # type: ignore[attr-defined]
    except tk.TclError:
        pass
    setup_macos_app_menu(app)


def setup_macos_app_menu(app: object) -> None:
    """Attach a minimal native macOS top menu bar complementary to the status bar icon."""
    import tkinter as tk

    try:
        root = app.root  # type: ignore[attr-defined]
        lang = app._lang  # type: ignore[attr-defined]
        menubar = tk.Menu(root)
        app_menu = tk.Menu(menubar, name="apple", tearoff=0)
        app_menu.add_command(
            label=tr("tray_refresh", lang),
            accelerator="Command+R",
            command=lambda: app.refresh(force_recompute=True, schedule_next=True),  # type: ignore[attr-defined]
        )
        app_menu.add_command(label=tr("tray_lang", lang), command=app._toggle_language)  # type: ignore[attr-defined]
        app_menu.add_separator()
        app_menu.add_command(
            label=tr("tray_startup", lang),
            command=lambda: app._handle_tray_action("toggle_startup"),  # type: ignore[attr-defined]
        )
        app_menu.add_separator()
        app_menu.add_command(label=tr("tray_quit", lang), accelerator="Command+Q", command=app._quit_completely)  # type: ignore[attr-defined]
        menubar.add_cascade(menu=app_menu)
        root.configure(menu=menubar)
    except Exception:
        pass


def get_macos_visible_frame() -> tuple[int, int, int, int] | None:
    """Return `(left, top, right, bottom)` in top-left screen coordinates for macOS main screen."""
    try:
        from AppKit import NSScreen

        screen = NSScreen.mainScreen()
        if screen is None:
            return None
        full = screen.frame()
        vis = screen.visibleFrame()
        left = int(round(vis.origin.x))
        right = int(round(vis.origin.x + vis.size.width))
        top = int(round(full.size.height - (vis.origin.y + vis.size.height)))
        bottom = int(round(full.size.height - vis.origin.y))
        return left, top, right, bottom
    except Exception:
        return None


def _make_delegate_class():
    from Foundation import NSObject
    import objc

    class _MacMenuBarDelegate(NSObject):
        def initWithOwner_(self, owner):
            self = objc.super(_MacMenuBarDelegate, self).init()
            if self is None:
                return None
            self._owner = owner
            return self

        def menuNeedsUpdate_(self, menu):
            try:
                self._owner._rebuild_menu(menu)
            except Exception:
                pass

        def onMenuAction_(self, sender):
            try:
                cmd = sender.representedObject()
                if cmd:
                    self._owner._emit(str(cmd))
            except Exception:
                pass

    return _MacMenuBarDelegate


_DELEGATE_CLS = None


class MacMenuBarTray:
    """Native macOS top menu bar icon backed by AppKit NSStatusBar on the main thread."""

    def __init__(
        self,
        icon_path: Path,
        tooltip: str,
        cmd_queue: "queue.SimpleQueue[str]",
        menu_state_fn: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self._icon_path = icon_path
        self._tooltip = tooltip[:127]
        self._cmd_queue = cmd_queue
        self._menu_state_fn = menu_state_fn
        self._status_item = None
        self._menu = None
        self._delegate = None

    def _emit(self, cmd: str) -> None:
        self._cmd_queue.put(cmd)

    def start(self) -> None:
        global _DELEGATE_CLS
        try:
            from AppKit import NSImage, NSMenu, NSSize, NSStatusBar, NSVariableStatusItemLength
            from Foundation import NSData

            if _DELEGATE_CLS is None:
                _DELEGATE_CLS = _make_delegate_class()

            status_bar = NSStatusBar.systemStatusBar()
            item = status_bar.statusItemWithLength_(NSVariableStatusItemLength)
            btn = item.button()
            if btn is not None:
                png_bytes = render_macos_menubar_template_png(36)
                ns_data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
                ns_img = NSImage.alloc().initWithData_(ns_data)
                if ns_img is not None:
                    ns_img.setSize_(NSSize(18.0, 18.0))
                    ns_img.setTemplate_(True)
                    btn.setImage_(ns_img)
                else:
                    btn.setTitle_("GP")
                btn.setToolTip_(self._tooltip)

            delegate = _DELEGATE_CLS.alloc().initWithOwner_(self)
            menu = NSMenu.alloc().init()
            menu.setDelegate_(delegate)
            self._rebuild_menu(menu)
            item.setMenu_(menu)

            self._delegate = delegate
            self._menu = menu
            self._status_item = item
        except Exception:
            self._status_item = None

    def _rebuild_menu(self, menu) -> None:
        from AppKit import NSControlStateValueOff, NSControlStateValueOn, NSMenuItem

        state = self._menu_state_fn() if self._menu_state_fn else {}
        lang = str(state.get("lang", "en"))
        startup_on = bool(state.get("startup_enabled", False))
        menu.removeAllItems()

        def add_item(title: str, cmd: str, checked: bool = False) -> None:
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "onMenuAction:", "")
            mi.setTarget_(self._delegate)
            mi.setRepresentedObject_(cmd)
            if checked:
                mi.setState_(NSControlStateValueOn)
            else:
                mi.setState_(NSControlStateValueOff)
            menu.addItem_(mi)

        add_item(tr("tray_open", lang), "show")
        add_item(tr("tray_refresh", lang), "refresh")
        menu.addItem_(NSMenuItem.separatorItem())
        add_item(tr("tray_startup", lang), "toggle_startup", checked=startup_on)
        add_item(tr("tray_shortcut", lang), "create_shortcut")
        add_item(tr("tray_lang", lang), "toggle_lang")
        menu.addItem_(NSMenuItem.separatorItem())
        add_item(tr("tray_quit", lang), "quit")

    def set_tooltip(self, tooltip: str) -> None:
        self._tooltip = tooltip[:127]
        if self._status_item is not None:
            try:
                btn = self._status_item.button()
                if btn is not None:
                    btn.setToolTip_(self._tooltip)
            except Exception:
                pass

    def stop(self) -> None:
        if self._status_item is not None:
            try:
                from AppKit import NSStatusBar

                NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)
            except Exception:
                pass
            self._status_item = None
            self._menu = None
            self._delegate = None
