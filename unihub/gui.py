"""UniHub desktop shell: app window (WebKitGTK) + tray icon (AppIndicator).

Fan and RGB control live in the daemon (``unihub daemon``, a systemd user
service); this process is only the window and the tray. Quitting it never
stops fan control.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# WebKitGTK renders a blank window on NVIDIA + Wayland with the DMA-BUF
# renderer; must be set before WebKit is loaded.
if Path("/proc/driver/nvidia").exists():
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gio, GLib, Gtk, WebKit2  # noqa: E402

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
except (ValueError, ImportError):
    AppIndicator = None

APP_ID = "io.github.unihub"
ICON_DIR = Path(__file__).resolve().parent.parent / "packaging"


class UniHubApp(Gtk.Application):
    def __init__(self, base_url: str):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.base = base_url.rstrip("/")
        self.window: Gtk.Window | None = None
        self.indicator = None
        self.status_item: Gtk.MenuItem | None = None
        self.app_cfg = {"close_to_tray": True, "start_minimized": True}
        self._first = True

    # --- daemon API ---------------------------------------------------------
    def api(self, path: str, body: dict | None = None):
        req = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            method="GET" if body is None else "PUT",
            headers={"Content-Type": "application/json", "X-UniHub": "1"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())

    def refresh_settings(self) -> None:
        try:
            self.app_cfg = self.api("/api/config").get("app", self.app_cfg)
        except (urllib.error.URLError, OSError, ValueError):
            pass  # daemon down: keep the last known settings
        self._sync_tray_visibility()

    def _sync_tray_visibility(self) -> None:
        """The tray icon only appears while the window is hidden: it is the
        way back to a closed (or start-minimized) window, nothing more."""
        if self.indicator is not None:
            hidden = self.window is None or not self.window.get_visible()
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE if hidden else AppIndicator.IndicatorStatus.PASSIVE)

    @property
    def tray_on(self) -> bool:
        return self.indicator is not None

    # --- lifecycle ----------------------------------------------------------
    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        autostart = "--autostart" in args
        if self._first:
            self._first = False
            self.hold()  # keep running while hidden in the tray
            self.refresh_settings()
            self.build_tray()
            GLib.timeout_add_seconds(2, self.update_status)
            if autostart and self.app_cfg.get("start_minimized", True):
                if not self.tray_on:  # nowhere to hide: start iconified instead
                    self.show_window()
                    self.window.iconify()
                return 0
        self.show_window()
        return 0

    def show_window(self, *_):
        if self.window is None:
            self.window = self.build_window()
        self.window.show_all()
        self.window.present()
        self._sync_tray_visibility()

    def build_window(self) -> Gtk.Window:
        win = Gtk.ApplicationWindow(application=self, title="UniHub")
        win.set_default_size(1180, 820)
        win.set_icon_name("unihub")
        if not Gtk.IconTheme.get_default().has_icon("unihub"):
            icon = ICON_DIR / "unihub.svg"
            if icon.exists():
                win.set_icon_from_file(str(icon))
        view = WebKit2.WebView()
        settings = view.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_enable_back_forward_navigation_gestures(False)
        view.connect("decide-policy", self._keep_local)
        view.connect("load-failed", self._load_failed)
        view.load_uri(self.base + "/")
        win.add(view)
        win.connect("delete-event", self.on_close)
        self.view = view
        return win

    def _keep_local(self, view, decision, kind):
        # Never navigate the app window away from the local UI.
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if not uri.startswith(self.base):
                decision.ignore()
                return True
        return False

    def _load_failed(self, view, event, uri, error):
        view.load_html(
            "<body style='font-family:sans-serif;background:#0f1115;color:#e7e9ee;padding:40px'>"
            "<h2>UniHub service isn't running</h2><p>Start it with "
            "<code>systemctl --user start unihub</code>. Retrying…</p></body>",
            None,
        )
        GLib.timeout_add_seconds(3, lambda: view.load_uri(self.base + "/") or False)  # one-shot retry
        return True

    def on_close(self, win, _event):
        self.refresh_settings()
        if self.tray_on and self.app_cfg.get("close_to_tray", True):
            win.hide()
            self._sync_tray_visibility()
            return True  # keep the window (and its page state) for next time
        self.quit()
        return False

    # --- tray ---------------------------------------------------------------
    def build_tray(self) -> None:
        if AppIndicator is None:
            return
        ind = AppIndicator.Indicator.new("unihub", "unihub", AppIndicator.IndicatorCategory.HARDWARE)
        if not Gtk.IconTheme.get_default().has_icon("unihub"):
            ind.set_icon_theme_path(str(ICON_DIR))
        ind.set_title("UniHub")
        menu = Gtk.Menu()
        open_item = Gtk.MenuItem(label="Open UniHub")
        open_item.connect("activate", self.show_window)
        menu.append(open_item)
        self.status_item = Gtk.MenuItem(label="Connecting…")
        self.status_item.set_sensitive(False)
        menu.append(self.status_item)
        menu.append(Gtk.SeparatorMenuItem())
        themes_item = Gtk.MenuItem(label="Themes")
        themes_menu = Gtk.Menu()
        try:
            names = [t["name"] for t in self.api("/api/meta")["themes"]]
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            names = []
        for name in names:
            item = Gtk.MenuItem(label=name)
            item.connect("activate", self.apply_theme, name)
            themes_menu.append(item)
        themes_item.set_submenu(themes_menu)
        themes_item.set_sensitive(bool(names))
        menu.append(themes_item)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit (fan control keeps running)")
        quit_item.connect("activate", lambda *_: self.quit())
        menu.append(quit_item)
        menu.show_all()
        ind.set_menu(menu)
        ind.set_secondary_activate_target(open_item)  # middle-click opens
        self.indicator = ind
        self.refresh_settings()

    def apply_theme(self, _item, name: str) -> None:
        try:
            self.api("/api/theme", {"name": name})
        except (urllib.error.URLError, OSError) as e:
            print(f"theme failed: {e}", file=sys.stderr)
            return
        if self.window is not None and self.window.get_visible():
            self.view.reload()

    def update_status(self) -> bool:
        if self.status_item is None:
            return True
        try:
            st = self.api("/api/state")
            cfg = self.api("/api/config")
        except (urllib.error.URLError, OSError, ValueError):
            self.status_item.set_label("Service not running")
            return True
        self.app_cfg = cfg.get("app", self.app_cfg)
        self._sync_tray_visibility()
        temps = [g["temp"] for g in st["hub"]["groups"] if g.get("temp") is not None]
        parts = [f"{max(temps):.0f}°C"] if temps else []
        for g, gc in zip(st["hub"]["groups"], cfg["hub"]["groups"]):
            if gc["fans"] > 0:
                parts.append(f"{gc['name']} {g['rpm']} rpm")
        if not st["hub"]["connected"]:
            parts = ["Hub disconnected"]
        self.status_item.set_label(" · ".join(parts) or "UniHub")
        return True



def run(base_url: str, argv: list[str]) -> int:
    app = UniHubApp(base_url)
    return app.run([sys.argv[0], *argv])
