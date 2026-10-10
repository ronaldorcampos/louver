"""Louver desktop shell: app window (WebKitGTK) + tray icon (AppIndicator).

Fan and RGB control live in the daemon (``louver daemon``, a systemd user
service); this process is only the window and the tray. Quitting it never
stops fan control. The window loads the UI from ``louver://app/``: each of
its requests is passed on to the daemon's Unix socket (apiclient.proxy).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

# WebKitGTK renders a blank window on NVIDIA + Wayland with the DMA-BUF
# renderer; must be set before WebKit is loaded.
if Path("/proc/driver/nvidia").exists():
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("Soup", "3.0")
from gi.repository import Gio, GLib, Gtk, Soup, WebKit2  # noqa: E402

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
except (ValueError, ImportError):
    AppIndicator = None

from . import apiclient, tray  # noqa: E402
from .gui_ids import APP_ID, TRAY_ICON  # noqa: E402

ICON_DIR = Path(__file__).resolve().parent.parent / "packaging" / "icons" / "hicolor"  # running from a checkout
SCHEME = "louver"
API_ERRORS = (OSError, ValueError, apiclient.ApiError)


class LouverApp(Gtk.Application):
    def __init__(self, sock_path: Path):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.sock = sock_path
        self.base = apiclient.BASE
        self._requests: set = set()  # louver:// requests in flight: kept alive until answered
        self.window: Gtk.Window | None = None
        self.indicator = None
        self.menu: Gtk.Menu | None = None
        self.menu_sig: tuple | None = None
        self.status_item: Gtk.MenuItem | None = None
        self.zone_items: dict[str, Gtk.MenuItem] = {}
        self.themes: list[str] = []
        self.sensor_labels: dict[str, str] = {}
        self._meta_at = -1e9
        self.app_cfg = {"close_to_tray": True, "start_minimized": True}
        self._first = True

    # --- daemon API ---------------------------------------------------------
    def api(self, path: str, body: dict | None = None):
        return apiclient.call(path, body, sock_path=self.sock, timeout=3)

    # --- louver://app/ ------------------------------------------------------
    def _register_scheme(self, ctx: WebKit2.WebContext) -> None:
        ctx.register_uri_scheme(SCHEME, self._serve_scheme)
        sec = ctx.get_security_manager()
        sec.register_uri_scheme_as_secure(SCHEME)
        # For the page's fetch() of louver://app/api/...: WebKit's loader lets
        # fetch() at a non-HTTP scheme only if it's CORS-enabled. The handler
        # serves louver://app alone, so this opens nothing to other origins.
        sec.register_uri_scheme_as_cors_enabled(SCHEME)

    def _serve_scheme(self, request, *_):
        """Runs on the main loop: read the request here, ask the daemon from a
        thread (a slow answer mustn't freeze the window), finish back here."""
        method = request.get_http_method() or "GET"
        uri = request.get_uri()
        headers = request.get_http_headers()
        ctype = headers.get_one("Content-Type") if headers is not None else None
        try:
            body = _read_body(request.get_http_body())
        except GLib.Error as e:
            return self._finish(request, apiclient.ProxyError(f"can't read the request: {e.message}"))
        self._requests.add(request)

        def work():
            try:
                reply = apiclient.proxy(method, uri, body, ctype, sock_path=self.sock)
            except Exception as e:  # anything, so the request is always finished
                reply = e
            GLib.idle_add(self._finish, request, reply)

        threading.Thread(target=work, name="louver-scheme", daemon=True).start()

    def _finish(self, request, reply) -> bool:
        self._requests.discard(request)
        if isinstance(reply, Exception):
            request.finish_error(GLib.Error.new_literal(Gio.io_error_quark(), str(reply), Gio.IOErrorEnum.FAILED))
            return False
        resp = WebKit2.URISchemeResponse.new(
            Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(reply.body)), len(reply.body)
        )
        resp.set_status(reply.status, reply.reason or None)
        resp.set_content_type(reply.content_type)
        headers = Soup.MessageHeaders.new(Soup.MessageHeadersType.RESPONSE)
        for k, v in reply.headers:  # the type again (this replaces it) and the CSP
            headers.append(k, v)
        resp.set_http_headers(headers)
        request.finish_with_response(resp)
        return False

    def refresh_settings(self) -> None:
        try:
            self.app_cfg = self.api("/api/config").get("app", self.app_cfg)
        except API_ERRORS:
            pass  # daemon down: keep the last known settings
        self._sync_tray_visibility()

    def _sync_tray_visibility(self) -> None:
        """The tray icon only appears while the window is hidden: it is the
        way back to a closed (or start-minimized) window, nothing more."""
        if self.indicator is not None:
            hidden = self.window is None or not self.window.get_visible()
            self.indicator.set_status(
                AppIndicator.IndicatorStatus.ACTIVE if hidden else AppIndicator.IndicatorStatus.PASSIVE
            )

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
        self.open_at("")

    def open_at(self, hash_: str) -> None:
        """Show the window; hash_ ("#hub-2", or "" for wherever it was) picks the page."""
        if self.window is None:
            self.window = self.build_window(hash_)
        elif hash_:
            self.view.load_uri(self.base + "/" + hash_)  # same page, new #hash: no reload
        self.window.show_all()
        self.window.present()
        # Without focus the page isn't active yet, and WebKit can spend the
        # first click activating it instead of delivering it.
        self.view.grab_focus()
        self._sync_tray_visibility()

    def build_window(self, hash_: str = "") -> Gtk.Window:
        win = Gtk.ApplicationWindow(application=self, title="Louver")
        win.set_default_size(1180, 820)
        win.set_icon_name(APP_ID)
        if not Gtk.IconTheme.get_default().has_icon(APP_ID):
            icon = ICON_DIR / "scalable" / "apps" / f"{APP_ID}.svg"
            if icon.exists():
                win.set_icon_from_file(str(icon))
        view = WebKit2.WebView()
        self._register_scheme(view.get_context())  # once: the window is built once and kept
        settings = view.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_enable_back_forward_navigation_gestures(False)
        view.connect("decide-policy", self._keep_local)
        view.connect("load-failed", self._load_failed)
        view.load_uri(self.base + "/" + hash_)
        win.add(view)
        win.connect("delete-event", self.on_close)
        self.view = view
        return win

    def _keep_local(self, view, decision, kind):
        # Never navigate the app window away from the local UI (or its error page).
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if not apiclient.window_may_open(uri):
                decision.ignore()
                return True
        return False

    def _load_failed(self, view, event, uri, error):
        view.load_html(
            "<body style='font-family:sans-serif;background:#03110f;color:#dcfff4;padding:40px'>"
            "<h2>Louver service isn't running</h2><p>Start it with "
            "<code>systemctl --user start louver</code>. Retrying…</p></body>",
            None,
        )
        GLib.timeout_add_seconds(3, lambda: view.load_uri(self.base + "/") or False)  # one-shot retry
        return True

    def on_close(self, win, _event):
        self.refresh_settings()
        if self.tray_on and self.app_cfg.get("close_to_tray", True):
            win.hide()
            self._sync_tray_visibility()
            self.update_status()  # the menu wasn't kept current while the window was up
            return True  # keep the window (and its page state) for next time
        self.quit()
        return False

    # --- tray ---------------------------------------------------------------
    # The menu: Open Louver, a status line, one submenu per zone with the quick
    # speeds, Themes, Quit. It's rebuilt only when its shape or a radio mark
    # changes (tray.signature); live rpm and temperatures are relabelled in place.
    def build_tray(self) -> None:
        if AppIndicator is None:
            return
        ind = AppIndicator.Indicator.new("louver", TRAY_ICON, AppIndicator.IndicatorCategory.HARDWARE)
        if not Gtk.IconTheme.get_default().has_icon(TRAY_ICON):
            ind.set_icon_theme_path(str(ICON_DIR / "symbolic" / "apps"))
        ind.set_title("Louver")
        self.indicator = ind
        self.update_status()
        self.refresh_settings()

    @staticmethod
    def _radio(label: str, active: bool) -> Gtk.CheckMenuItem:
        # A check item drawn as a radio, not a RadioMenuItem: a radio group
        # can't show "none of these" (a custom theme, a custom curve).
        item = Gtk.CheckMenuItem(label=label)
        item.set_draw_as_radio(True)
        item.set_active(active)  # before connecting: set_active fires "activate"
        return item

    def build_menu(self, cfg: dict | None, state: dict | None) -> None:
        menu = Gtk.Menu()
        open_item = Gtk.MenuItem(label="Open Louver")
        open_item.connect("activate", self.show_window)
        menu.append(open_item)
        self.status_item = Gtk.MenuItem(label=tray.status_line(cfg, state, self.sensor_labels))
        self.status_item.set_sensitive(False)
        menu.append(self.status_item)
        menu.append(Gtk.SeparatorMenuItem())

        self.zone_items = {}
        zones = tray.zones(cfg, state) if cfg is not None and state is not None else []
        for z in zones:
            sub = Gtk.Menu()
            for mode, label in tray.speeds(z):
                item = self._radio(label, z.mode == mode)
                item.connect("activate", self.set_zone_mode, z.key, mode)
                sub.append(item)
            if z.mode not in dict(tray.speeds(z)):  # set in the window: show it, but it's not a tray choice
                item = self._radio(tray.mode_label(z), True)
                item.set_sensitive(False)
                sub.append(item)
            sub.append(Gtk.SeparatorMenuItem())
            edit = Gtk.MenuItem(label="Open in Louver…")
            edit.connect("activate", lambda _i, key=z.key: self.open_at("#" + key))
            sub.append(edit)
            item = Gtk.MenuItem(label=tray.zone_label(z))
            item.set_submenu(sub)
            menu.append(item)
            self.zone_items[z.key] = item
        if zones:
            menu.append(Gtk.SeparatorMenuItem())

        themes_item = Gtk.MenuItem(label="Themes")
        themes_menu = Gtk.Menu()
        current = cfg.get("theme") if cfg is not None else None
        for name in self.themes:
            item = self._radio(name, name == current)
            item.connect("activate", self.apply_theme, name)
            themes_menu.append(item)
        themes_item.set_submenu(themes_menu)
        themes_item.set_sensitive(bool(self.themes) and cfg is not None)
        menu.append(themes_item)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit (fan control keeps running)")
        quit_item.connect("activate", lambda *_: self.quit())
        menu.append(quit_item)
        menu.show_all()
        self.indicator.set_menu(menu)
        self.indicator.set_secondary_activate_target(open_item)  # middle-click opens
        self.menu = menu

    def _put(self, path: str, body: dict) -> None:
        try:
            self.api(path, body)
        except API_ERRORS as e:
            print(f"{path} failed: {e}", file=sys.stderr)
        # Rebuild either way: a click un-ticks a check item before we get
        # here, and a failed change must show the old choice again.
        self.menu_sig = None
        self.update_status()

    def set_zone_mode(self, _item, zone: str, mode: str) -> None:
        self._put("/api/zone-mode", {"zone": zone, "mode": mode})

    def apply_theme(self, _item, name: str) -> None:
        # The window (open or hidden) notices the new config revision itself.
        self._put("/api/theme", {"name": name})

    def _refresh_meta(self) -> None:
        """Theme names and sensor labels change rarely; /api/meta is costly
        (it reads every sensor), so fetch it every 30 s at most."""
        if self.themes and time.monotonic() - self._meta_at < 30:
            return
        try:
            meta = self.api("/api/meta")
        except API_ERRORS:
            return
        self.themes = [t["name"] for t in meta.get("themes", [])]
        self.sensor_labels = {s["id"]: s["label"] for s in meta.get("sensors", [])}
        self._meta_at = time.monotonic()

    def update_status(self) -> bool:
        """Every 2 s while the tray is showing (the window is hidden)."""
        if self.indicator is None or (self.window is not None and self.window.get_visible()):
            return True
        try:
            state = self.api("/api/state")
            cfg = self.api("/api/config")
        except API_ERRORS:
            state = cfg = None
        if cfg is not None:
            self.app_cfg = cfg.get("app", self.app_cfg)
            self._refresh_meta()
        sig = tray.signature(cfg, state, self.themes)
        if sig != self.menu_sig:
            self.build_menu(cfg, state)
            self.menu_sig = sig
        else:
            self.status_item.set_label(tray.status_line(cfg, state, self.sensor_labels))
            for z in tray.zones(cfg, state) if cfg is not None and state is not None else []:
                self.zone_items[z.key].set_label(tray.zone_label(z))
        return True


def _read_body(stream) -> bytes | None:
    """The request body, cut off just past apiclient.MAX_BODY: proxy() then
    refuses it as too large without the rest being read."""
    if stream is None:
        return None
    data = bytearray()
    try:
        while len(data) <= apiclient.MAX_BODY:
            chunk = stream.read_bytes(min(65536, apiclient.MAX_BODY + 1 - len(data)), None).get_data()
            if not chunk:
                break
            data += chunk
    finally:
        stream.close(None)
    return bytes(data)


def run(sock_path: Path, argv: list[str]) -> int:
    app = LouverApp(sock_path)
    return app.run([sys.argv[0], *argv])
