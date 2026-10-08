"""Control loop + local HTTP API/UI server."""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import config as cfgmod
from .curves import PRESETS, Governor, interpolate
from .devices import DRIVERS, VENDOR_ID, LightingEffect, StrimerPlus, UniHubALV2
from .hidraw import HidrawTransport, enumerate_hidraw
from .mbfans import MotherboardFans
from .rgbfx import SOFTWARE_MODES, render_hub_port, render_strimer_port
from .sensors import SensorHub
from .themes import THEMES, apply_theme

log = logging.getLogger("unihub")

WEB_DIR = Path(__file__).with_name("web")
RECONNECT_EVERY = 5.0
KEEPALIVE_EVERY = 10.0
TEMP_LIGHT_STEP = 2.0  # deg C a Temperature-mode colour must move before it's re-sent


def open_devices(skip: frozenset[int] = frozenset()) -> dict[int, object]:
    """Open every supported Lian Li controller not in ``skip``; keyed by PID."""
    found: dict[int, object] = {}
    first_error: OSError | None = None
    for info in enumerate_hidraw():
        if info.vid != VENDOR_ID or info.pid not in DRIVERS or info.pid in found or info.pid in skip:
            continue
        try:
            found[info.pid] = DRIVERS[info.pid](HidrawTransport(info.devnode))
        except OSError as e:
            log.warning("cannot open %s: %s", info.devnode, e)
            first_error = first_error or e
            continue
        log.info("opened %s at %s", DRIVERS[info.pid].NAME, info.devnode)
    if not found and first_error is not None:
        raise first_error
    return found


class Controller:
    def __init__(
        self,
        cfg: dict,
        cfg_path: Path | None,
        sensors: SensorHub,
        opener: Callable[[frozenset[int]], dict[int, object]] = open_devices,
        clock: Callable[[], float] = time.monotonic,
        mbfans: MotherboardFans | None = None,
    ):
        self.cfg = cfgmod.validate(cfg)
        self.mb = mbfans if mbfans is not None and mbfans.present else None
        self._mb_governors: dict[int, Governor] = {}
        self._mb_sent: dict[int, tuple[int, float]] = {}
        self.mb_state: dict[int, dict] = {}
        self.mb_error = ""
        self.cfg_path = cfg_path
        self.sensors = sensors
        self._open = opener
        self._clock = clock
        self.lock = threading.RLock()
        self.hub: UniHubALV2 | None = None
        self.strimer: StrimerPlus | None = None
        self.hub_firmware = ""
        self.strimer_firmware = ""
        self.error = ""
        self._last_connect = -1e9
        self._last_tick: float | None = None
        self._governors = [Governor() for _ in range(4)]
        self._sent: list[int | None] = [None] * 4
        self._sent_at = [0.0] * 4
        self.lab_groups: set[int] = set()  # ports showing a Lighting Lab effect
        self._hub_temp_key: list[float | None] = [None] * 4
        self._strimer_temp_key: list[float | None] = [None] * 12
        self.groups_state = [
            {"rpm": 0, "percent": None, "temp": None, "target": None, "failsafe": False, "missing": []}
            for _ in range(4)
        ]

    # --- device lifecycle ---------------------------------------------------
    def connect(self) -> None:
        self._last_connect = self._clock()
        held = frozenset(
            pid for pid, dev in ((UniHubALV2.PID, self.hub), (StrimerPlus.PID, self.strimer)) if dev is not None
        )
        try:
            devs = self._open(held)
        except OSError as e:
            self.error = str(e)
            log.warning("open failed: %s", e)
            return
        self.error = ""
        hub = devs.get(UniHubALV2.PID)
        if hub is not None:
            try:
                self._init_hub(hub)  # type: ignore[arg-type]
                self.hub = hub  # type: ignore[assignment]
            except OSError as e:
                self.error = f"hub init failed: {e}"
                log.warning(self.error)
                hub.close()  # type: ignore[attr-defined]
        strimer = devs.get(StrimerPlus.PID)
        if strimer is not None:
            try:
                self._init_strimer(strimer)  # type: ignore[arg-type]
                self.strimer = strimer  # type: ignore[assignment]
            except OSError as e:
                log.warning("strimer init failed: %s", e)
                strimer.close()  # type: ignore[attr-defined]

    def _init_hub(self, hub: UniHubALV2) -> None:
        fw = hub.read_firmware()
        self.hub_firmware = fw.version if fw.valid_alv2 else f"{fw.version} (unrecognised id)"
        hcfg = self.cfg["hub"]
        hub.set_mode_overrides(hcfg["mode_map"]["inner"], hcfg["mode_map"]["outer"])
        self.lab_groups.clear()
        for i, g in enumerate(hcfg["groups"]):
            hub.set_fan_count(i, int(g["fans"]))
            hub.set_mb_pwm_sync(i, g["mode"] == "mb_sync")
        self._sent = [None] * 4
        for gov in self._governors:
            gov.reset()
        self._push_hub_lighting(hub, range(4))

    def _light_temp(self, entry: dict) -> float | None:
        return self.sensors.read(entry.get("sensor", "cpu")) if entry["mode"] == "Temperature" else None

    @staticmethod
    def _temp_key(temp: float | None) -> float | None:
        return None if temp is None else round(temp / TEMP_LIGHT_STEP)

    def _push_hub_lighting(self, hub: UniHubALV2, groups) -> None:
        hcfg = self.cfg["hub"]
        hub.set_mb_rgb_sync(bool(hcfg["mb_rgb_sync"]))
        if hcfg["mb_rgb_sync"]:
            return
        for i in groups:
            if i not in self.lab_groups:
                self._push_hub_group(hub, i)

    def _push_hub_group(self, hub: UniHubALV2, i: int) -> None:
        entry = self.cfg["hub"]["lighting"][i]
        if entry["mode"] in SOFTWARE_MODES:
            temp = self._light_temp(entry)
            fans = [int(g["fans"]) for g in self.cfg["hub"]["groups"]]
            inner, outer = render_hub_port(entry, i, fans, temp)
            hub.set_leds(i, inner, outer, int(entry.get("brightness", 4)))
            self._hub_temp_key[i] = self._temp_key(temp)
        else:
            hub.set_lighting(i, LightingEffect.from_dict(entry))

    def _init_strimer(self, s: StrimerPlus) -> None:
        self.strimer_firmware = s.read_firmware()
        s.read_channel2_ports()
        self._push_strimer_lighting(s)

    def _strimer_entries(self, s: StrimerPlus) -> list[dict]:
        scfg = self.cfg["strimer"]
        if scfg["per_port"]:
            return scfg["ports"][: s.port_count]
        return [scfg["lighting"]] * s.port_count

    def _push_strimer_lighting(self, s: StrimerPlus, ports=None) -> None:
        scfg = self.cfg["strimer"]
        if scfg["mb_rgb_sync"]:
            s.set_mb_rgb_sync(True)
            return
        entries = self._strimer_entries(s)
        if ports is None:
            s.set_mb_rgb_sync(False)
            ports = range(len(entries))
            if not scfg["per_port"] and scfg["lighting"]["mode"] not in SOFTWARE_MODES:
                s.set_lighting(LightingEffect.from_dict(scfg["lighting"]))
                return
        for p in ports:
            entry = entries[p]
            if entry["mode"] in SOFTWARE_MODES:
                temp = self._light_temp(entry)
                s.set_leds({p: render_strimer_port(entry, p, len(entries), temp)}, int(entry.get("brightness", 4)))
                self._strimer_temp_key[p] = self._temp_key(temp)
            else:
                s.set_lighting(LightingEffect.from_dict(entry), ports=[p])

    def _refresh_temperature_lighting(self) -> None:
        """Re-render Temperature-mode zones whose colour bucket moved."""
        hub = self.hub
        if hub is not None and not self.cfg["hub"]["mb_rgb_sync"]:
            for i, entry in enumerate(self.cfg["hub"]["lighting"]):
                if i in self.lab_groups:
                    continue
                if entry["mode"] == "Temperature" and self._temp_key(self._light_temp(entry)) != self._hub_temp_key[i]:
                    try:
                        self._push_hub_group(hub, i)
                    except OSError as e:
                        self._drop("hub", e)
                        break
        s = self.strimer
        if s is not None and not self.cfg["strimer"]["mb_rgb_sync"]:
            stale = [
                p for p, entry in enumerate(self._strimer_entries(s))
                if entry["mode"] == "Temperature" and self._temp_key(self._light_temp(entry)) != self._strimer_temp_key[p]
            ]
            if stale:
                try:
                    self._push_strimer_lighting(s, stale)
                except OSError as e:
                    self._drop("strimer", e)

    def _drop(self, which: str, err: Exception) -> None:
        log.warning("%s I/O error, will reconnect: %s", which, err)
        self.error = f"{which} disconnected: {err}"
        dev = getattr(self, which)
        if dev is not None:
            try:
                dev.close()
            except OSError:
                pass
        setattr(self, which, None)

    # --- control ------------------------------------------------------------
    def target_for(self, i: int) -> tuple[float | None, float | None, bool]:
        """(target percent or None for mb_sync, temp, failsafe)."""
        return self._target(self.cfg["hub"]["groups"][i])

    def _target(self, g: dict) -> tuple[float | None, float | None, bool]:
        mode = g["mode"]
        if mode in ("mb_sync", "bios"):
            return None, None, False
        temp = self.sensors.read(g["sensor"])
        if mode == "manual":
            return float(g["manual_percent"]), temp, False
        if temp is None:
            return 100.0, None, True  # can't see the temperature: run flat out
        points = g["curve"] if mode == "curve" else PRESETS[mode]
        return interpolate(points, temp), temp, False

    def tick(self) -> None:
        with self.lock:
            now = self._clock()
            dt = 0.0 if self._last_tick is None else now - self._last_tick
            self._last_tick = now
            if (self.hub is None or self.strimer is None) and now - self._last_connect >= RECONNECT_EVERY:
                self.connect()
            floor = int(self.cfg["min_percent"])
            for i in range(4):
                target, temp, failsafe = self.target_for(i)
                st = self.groups_state[i]
                sensor = self.cfg["hub"]["groups"][i]["sensor"]
                missing = self.sensors.missing(sensor) if target is not None else []
                st.update(temp=temp, target=target, failsafe=failsafe, missing=missing)
                if target is None:
                    self._governors[i].reset()
                    st["percent"] = None
                    continue
                target = max(target, float(floor))
                gov = self._governors[i]
                if self.cfg["hub"]["groups"][i]["mode"] == "manual":
                    gov.reset()  # a fixed speed the user picked applies at once
                pct = int(round(gov.step(target, dt)))
                if failsafe:
                    pct = 100
                st["percent"] = pct
                if self.hub is None:
                    continue
                if pct != self._sent[i] or now - self._sent_at[i] >= KEEPALIVE_EVERY:
                    try:
                        self.hub.set_speed(i, pct)
                    except OSError as e:
                        self._drop("hub", e)
                        break
                    self._sent[i], self._sent_at[i] = pct, now
            if self.hub is not None:
                try:
                    for i, rpm in enumerate(self.hub.read_rpms()):
                        self.groups_state[i]["rpm"] = rpm
                except OSError as e:
                    self._drop("hub", e)
            self._refresh_temperature_lighting()
            self._tick_motherboard(now, dt, floor)

    def _tick_motherboard(self, now: float, dt: float, floor: int) -> None:
        if self.mb is None:
            return
        configured = {h["index"]: h for h in self.cfg["motherboard"]["headers"]}
        for idx, header in self.mb.headers.items():
            g = configured.get(idx)
            st = self.mb_state.setdefault(idx, {})
            st.update(rpm=header.rpm, percent=None, temp=None, failsafe=False, missing=[])
            target, temp, failsafe = self._target(g) if g else (None, None, False)
            if target is None:  # BIOS control
                self._mb_governors.pop(idx, None)
                self._mb_sent.pop(idx, None)
                if header.original_enable is not None:
                    try:
                        self.mb.release(idx)
                    except OSError as e:
                        self.mb_error = f"could not hand header {idx} back to BIOS: {e}"
                continue
            gov = self._mb_governors.setdefault(idx, Governor())
            if g["mode"] == "manual":
                gov.reset()
            pct = 100 if failsafe else int(round(gov.step(max(target, float(floor)), dt)))
            st.update(percent=pct, temp=temp, failsafe=failsafe,
                      missing=self.sensors.missing(g["sensor"]) if not failsafe else [])
            last = self._mb_sent.get(idx)
            if last is None or last[0] != pct or now - last[1] >= KEEPALIVE_EVERY:
                try:
                    self.mb.set_percent(idx, pct)
                    self._mb_sent[idx] = (pct, now)
                    self.mb_error = ""
                except PermissionError:
                    self.mb_error = "motherboard fan controls aren't writable: install the hwmon udev rule (README)"
                except OSError as e:
                    self.mb_error = f"motherboard header {idx}: {e}"

    # --- config -------------------------------------------------------------
    def apply_config(self, new: dict) -> dict:
        new = cfgmod.validate(new)
        with self.lock:
            old = self.cfg
            self.cfg = new
            if self.cfg_path is not None:
                cfgmod.save(new, self.cfg_path)
                if old["app"]["start_on_login"] != new["app"]["start_on_login"]:
                    from . import autostart

                    try:
                        autostart.sync(new["app"]["start_on_login"])
                    except OSError as e:
                        log.warning("autostart update failed: %s", e)
            hub = self.hub
            try:
                if hub is not None:
                    relight = []
                    for i in range(4):
                        og, ng = old["hub"]["groups"][i], new["hub"]["groups"][i]
                        if og["fans"] != ng["fans"]:
                            hub.set_fan_count(i, int(ng["fans"]))
                            relight.append(i)
                        if og["mode"] != ng["mode"]:
                            self._governors[i].reset()
                        if (og["mode"] == "mb_sync") != (ng["mode"] == "mb_sync"):
                            hub.set_mb_pwm_sync(i, ng["mode"] == "mb_sync")
                            self._sent[i] = None
                        if old["hub"]["lighting"][i] != new["hub"]["lighting"][i]:
                            relight.append(i)
                    if relight and any(i in relight for i in range(4) if old["hub"]["groups"][i]["fans"] != new["hub"]["groups"][i]["fans"]):
                        # A cross-port gradient depends on every port's fan count.
                        relight += [
                            i for i, e in enumerate(new["hub"]["lighting"])
                            if e["mode"] == "Gradient" and e.get("span_all")
                        ]
                    if old["hub"]["mode_map"] != new["hub"]["mode_map"]:
                        mm = new["hub"]["mode_map"]
                        hub.set_mode_overrides(mm["inner"], mm["outer"])
                        relight = list(range(4))
                    if old["hub"]["mb_rgb_sync"] != new["hub"]["mb_rgb_sync"]:
                        relight = list(range(4))
                    if relight:
                        self._push_hub_lighting(hub, sorted(set(relight)))
            except OSError as e:
                self._drop("hub", e)
            try:
                if self.strimer is not None and old["strimer"] != new["strimer"]:
                    self._push_strimer_lighting(self.strimer)
            except OSError as e:
                self._drop("strimer", e)
        self.tick()
        return new

    # --- Lighting Lab -------------------------------------------------------
    def lab_show(self, req: dict) -> None:
        from .devices import parse_color

        group = int(req.get("group", 0))
        ring = req.get("ring", "inner")
        if ring not in ("inner", "outer"):
            raise cfgmod.ConfigError("ring must be inner or outer")
        colors = [parse_color(c) for c in req.get("colors") or ["#ff0000", "#ffd400", "#00ff6a", "#0088ff", "#b000ff", "#ff00c8"]]
        with self.lock:
            if self.hub is None:
                raise cfgmod.ConfigError("hub not connected")
            try:
                self.hub.raw_effect(group, ring, int(req.get("mode", 1)), colors,
                                    speed=int(req.get("speed", 2)), direction=str(req.get("direction", "cw")),
                                    brightness=int(req.get("brightness", 4)))
            except OSError as e:
                self._drop("hub", e)
                raise cfgmod.ConfigError(f"hub I/O error: {e}") from None
            self.lab_groups.add(group)

    def lab_end(self) -> None:
        with self.lock:
            groups, self.lab_groups = sorted(self.lab_groups), set()
            if self.hub is not None and groups:
                try:
                    self._push_hub_lighting(self.hub, groups)
                except OSError as e:
                    self._drop("hub", e)

    def state(self) -> dict:
        with self.lock:
            return {
                "hub": {
                    "connected": self.hub is not None,
                    "name": UniHubALV2.NAME,
                    "firmware": self.hub_firmware,
                    "groups": copy.deepcopy(self.groups_state),
                    "lab": sorted(self.lab_groups),
                },
                "strimer": {
                    "connected": self.strimer is not None,
                    "name": StrimerPlus.NAME,
                    "firmware": self.strimer_firmware,
                    "ports": self.strimer.port_count if self.strimer else 0,
                },
                "motherboard": {
                    "present": self.mb is not None,
                    "chip": self.mb.chip if self.mb else "",
                    "writable": self.mb.writable() if self.mb else False,
                    "headers": [{"index": i, **copy.deepcopy(s)} for i, s in sorted(self.mb_state.items())],
                    "error": self.mb_error,
                },
                "error": self.error,
            }

    def shutdown(self) -> None:
        with self.lock:
            if self.mb is not None:
                self.mb.release_all()  # BIOS curves take back over
            if self.strimer is not None:
                self.strimer.close()
                self.strimer = None
            hub = self.hub
            if hub is None:
                return
            action = self.cfg["on_exit"]
            try:
                if action == "mb_sync":
                    for i in range(4):
                        hub.set_mb_pwm_sync(i, True)
                elif action == "fixed":
                    for i in range(4):
                        if self.cfg["hub"]["groups"][i]["mode"] != "mb_sync":
                            hub.set_speed(i, int(self.cfg["on_exit_percent"]))
            except OSError as e:
                log.warning("on-exit action failed: %s", e)
            hub.close()
            self.hub = None

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # never let one bad tick kill fan control
                log.exception("tick failed")
            stop.wait(float(self.cfg["poll_interval"]))


_SOFTWARE_META = [
    {"name": "Custom", "software": True, "inner": True, "outer": True, "max_colors": 0},
    {"name": "Gradient", "software": True, "inner": True, "outer": True, "max_colors": 6},
    {"name": "Temperature", "software": True, "inner": True, "outer": True, "max_colors": 0},
]


def meta(ctrl: Controller) -> dict:
    return {
        "fan_modes": list(cfgmod.FAN_MODES),
        "mb_fan_modes": list(cfgmod.MB_FAN_MODES),
        "presets": PRESETS,
        "hub_lighting_modes": UniHubALV2.lighting_modes() + _SOFTWARE_META,
        "strimer_lighting_modes": StrimerPlus.lighting_modes() + _SOFTWARE_META,
        "themes": [{"name": n, "swatch": t["swatch"]} for n, t in THEMES.items()],
        "sensors": ctrl.sensors.list(),
    }


_STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
_CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css"}


def make_handler(ctrl: Controller, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "unihub"

        def log_message(self, fmt, *args):  # quiet by default
            log.debug("http: " + fmt, *args)

        def _host_ok(self) -> bool:
            # Blocks DNS-rebinding: only answer requests addressed to localhost.
            return self.headers.get("Host", "") in allowed_hosts

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                return self._json(200, ctrl.state())
            if path == "/api/config":
                return self._json(200, ctrl.cfg)
            if path == "/api/meta":
                return self._json(200, meta(ctrl))
            name = _STATIC.get(path)
            if name is None:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            f = WEB_DIR / name
            self._send(200, f.read_bytes(), _CTYPES[f.suffix])

        def do_PUT(self):
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
            # A custom header forces a CORS preflight, which we never grant,
            # so other websites can't drive the fans through the browser.
            if self.headers.get("X-UniHub") != "1":
                return self._json(HTTPStatus.FORBIDDEN, {"error": "missing X-UniHub header"})
            if self.path not in ("/api/config", "/api/theme", "/api/lab"):
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(n, 1 << 20)))
                if not isinstance(body, dict):
                    raise cfgmod.ConfigError("body must be an object")
                if self.path == "/api/lab":
                    if body.get("action") == "end":
                        ctrl.lab_end()
                    else:
                        ctrl.lab_show(body)
                    return self._json(200, ctrl.state())
                if self.path == "/api/theme":
                    name = body.get("name")
                    if name not in THEMES:
                        raise cfgmod.ConfigError(f"unknown theme {name!r}")
                    body = apply_theme(ctrl.cfg, name)
                new = ctrl.apply_config(body)
            except (ValueError, cfgmod.ConfigError) as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            return self._json(200, new)

    return Handler


def serve(ctrl: Controller, listen: str) -> ThreadingHTTPServer:
    host, port_s = listen.rsplit(":", 1)
    port = int(port_s)
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("refusing to listen on a non-loopback address")
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    httpd = ThreadingHTTPServer((host, port), make_handler(ctrl, allowed))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    return httpd
