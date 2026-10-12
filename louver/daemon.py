"""Control loop + the HTTP API/UI server on a Unix socket."""

from __future__ import annotations

import copy
import errno
import io
import json
import logging
import os
import socket
import socketserver
import stat
import struct
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from . import aura
from . import config as cfgmod
from .apiclient import MAX_BODY, check_length, socket_dir
from .aura import AuraMainboard, HidrawAccess
from .curves import PRESETS, Governor, interpolate
from .devices import DRIVERS, VENDOR_ID, LightingEffect, StrimerPlus, UniHubALV2, parse_color
from .hidraw import HidrawTransport, enumerate_hidraw
from .mbfans import MotherboardFans
from .rgbfx import SOFTWARE_MODES, render_hub_port, render_strimer_port
from .sensors import SensorHub
from .themes import THEMES, apply_theme

log = logging.getLogger("louver")

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


class _AuraUnusable(Exception):
    """The controller answered, but with nothing Louver can drive."""


class Controller:
    def __init__(
        self,
        cfg: dict,
        cfg_path: Path | None,
        sensors: SensorHub,
        opener: Callable[[frozenset[int]], dict[int, object]] = open_devices,
        clock: Callable[[], float] = time.monotonic,
        mbfans: MotherboardFans | None = None,
        aura_access=None,
    ):
        self.cfg = cfgmod.validate(cfg)
        # Bumped on every config change, so a window that's been open a while
        # (or hidden behind the tray) can tell its copy is out of date.
        self.cfg_rev = 0
        self.mb = mbfans if mbfans is not None and mbfans.present else None
        self._mb_fans = mbfans  # kept while the chip is gone, to look for it again
        self._mb_checked = -1e9  # last check that the chip is still where it was
        self._mb_governors: dict[int, Governor] = {}
        self._mb_sent: dict[int, tuple[int, float]] = {}
        self.mb_state: dict[int, dict] = {}
        self.mb_error = ""
        self.cfg_path = cfg_path
        self.sensors = sensors
        self._open = opener
        self._clock = clock
        self.lock = threading.RLock()
        # Set by shutdown(): from then on nothing may touch the hardware again,
        # not even a request the HTTP server was still answering.
        self._stopped = False
        self.hub: UniHubALV2 | None = None
        self.strimer: StrimerPlus | None = None
        self.hub_firmware = ""
        self.strimer_firmware = ""
        self.error = ""
        # The motherboard's Aura controller (vendor 0x0B05, the Aura mainboard
        # PIDs only): found, watched and opened through aura_access, and only
        # while motherboard RGB is turned on.
        self.aura_access = aura_access if aura_access is not None else HidrawAccess()
        self.aura: AuraMainboard | None = None
        self.aura_firmware = ""
        self.aura_error = ""
        self.aura_notes: dict[str, str] = {}  # channel key -> why Louver doesn't drive it
        self._aura_node = ""
        self._aura_ident = None  # aura_access.node_id of the open controller
        self._aura_dead = None  # node_id of a controller with nothing to drive: not retried
        self._aura_tried = -1e9  # last connect attempt
        self._aura_checked = -1e9  # last liveness check
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

    def _tick_aura(self, now: float) -> None:
        """Every RECONNECT_EVERY: notice an unplugged or re-enumerated
        controller (sysfs and a stat only, no device I/O), and try to connect
        while there's none."""
        if not self.cfg["motherboard_rgb"]["enabled"]:
            return
        if self.aura is not None and now - self._aura_checked >= RECONNECT_EVERY:
            self._aura_checked = now
            if self.aura_access.node_id(self._aura_node) != self._aura_ident:
                self._drop_aura(OSError(f"{self._aura_node} went away"))
        if self.aura is None and now - self._aura_tried >= RECONNECT_EVERY:
            self._connect_aura(now)

    def _connect_aura(self, now: float) -> None:
        self._aura_tried = now
        try:
            node = self.aura_access.find()
        except OSError as e:
            self._aura_failed(f"cannot look for it: {e}")
            return
        if node is None:
            # Nothing to report beyond what aura_error already says: a drop
            # reason stays visible until a connect, another error or a disable
            # replaces it.
            return
        ident = self.aura_access.node_id(node)
        if self._aura_dead is not None and ident == self._aura_dead:
            return  # the same controller as last time: waiting for it to change, or for a re-enable
        try:
            dev = self.aura_access.open(node)
        except OSError as e:
            self._aura_failed(f"cannot open: {e}")
            return
        try:
            self._init_aura(dev)
        except _AuraUnusable as e:
            self._aura_dead = ident
            self._aura_failed(f"{e}; not retried until it's turned off and on, or the controller reappears")
            dev.close()
            return
        except (OSError, ValueError) as e:
            self._aura_failed(f"init failed: {e}")
            dev.close()
            return
        log.info("opened %s at %s", AuraMainboard.NAME, node)
        self.aura, self._aura_node, self._aura_ident = dev, node, ident
        self._aura_checked = now
        self.aura_error = ""

    def _aura_failed(self, msg: str) -> None:
        if msg != self.aura_error:  # retried every few seconds: log each new reason once
            log.warning("motherboard RGB: %s", msg)
        self.aura_error = msg

    def _init_aura(self, dev: AuraMainboard) -> None:
        # OpenRGB carries on without a firmware string; without the config
        # table there are no channels, so that one is fatal.
        try:
            self.aura_firmware = dev.read_firmware()
        except TimeoutError:
            self.aura_firmware = ""
        channels = dev.read_config()
        # Check every channel before sending anything, so a table that
        # doesn't fit never gets half its channels set.
        notes = {ch.key: "its LEDs don't fit one effect report" for ch in channels if not dev.fits(ch.index)}
        if len(notes) == len(channels):
            argb = sum(ch.kind == "argb" for ch in channels)
            onboard = sum(ch.num_leds for ch in channels if ch.kind == "onboard")
            raise _AuraUnusable(
                f"its config table has nothing to drive ({onboard} onboard LEDs, {argb} ARGB headers"
                + (", none fitting one effect report)" if channels else ")")
            )
        self.aura_notes = notes
        dev.set_gen1()
        self._push_aura(dev)

    @staticmethod
    def _aura_look(m: dict, key: str) -> dict:
        """A channel's look in a motherboard_rgb config section."""
        return (m["channels"].get(key) if m["per_channel"] else None) or m["lighting"]

    def _push_aura(self, dev: AuraMainboard, keys=None) -> None:
        for ch in dev.channels:
            if (keys is not None and ch.key not in keys) or ch.key in self.aura_notes:
                continue
            entry = self._aura_look(self.cfg["motherboard_rgb"], ch.key)
            # OpenRGB sends black with the effects that take no colour.
            color = parse_color(entry["colors"][0]) if entry["mode"] in aura.COLOR_MODES else (0, 0, 0)
            dev.set_effect(ch.index, aura.MODES[entry["mode"]], color)

    def _drop_aura(self, err: Exception) -> None:
        log.warning("motherboard RGB I/O error, will reconnect: %s", err)
        self.aura_error = f"disconnected: {err}"
        dev, self.aura = self.aura, None
        if dev is not None:
            try:
                dev.close()
            except OSError:
                pass

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
                p
                for p, entry in enumerate(self._strimer_entries(s))
                if entry["mode"] == "Temperature"
                and self._temp_key(self._light_temp(entry)) != self._strimer_temp_key[p]
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
            if self._stopped:
                return
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
            self._tick_aura(now)

    def _check_motherboard(self, now: float) -> None:
        """Every RECONNECT_EVERY: find the Super I/O chip again if it wasn't
        there at start, went away, or its driver reloaded under a new hwmonN."""
        fans = self._mb_fans
        if fans is None or now - self._mb_checked < RECONNECT_EVERY:
            return
        self._mb_checked = now
        if not fans.stale():
            return
        was = self.mb is not None
        fans.rescan()
        self.mb = fans if fans.present else None
        self._mb_sent.clear()  # send every opted-in header its speed again, at the new path
        self.mb_state.clear()
        self.mb_error = ""
        if self.mb is not None:
            log.info("motherboard fans: %s at %s", fans.chip, fans.dir)
        elif was:
            log.warning("motherboard fans: %s went away, will look again", fans.chip)

    def _tick_motherboard(self, now: float, dt: float, floor: int) -> None:
        self._check_motherboard(now)
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
            low = max(float(floor), float(g["min_percent"]))  # the header's own floor, manual mode included
            pct = 100 if failsafe else int(round(gov.step(max(target, low), dt)))
            st.update(
                percent=pct,
                temp=temp,
                failsafe=failsafe,
                missing=self.sensors.missing(g["sensor"]) if not failsafe else [],
            )
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
    def update_config(self, change) -> dict:
        """Apply ``change(current cfg) -> new cfg`` atomically: no other
        change can land between reading the config and applying it."""
        with self.lock:
            self._refuse_if_stopped()
            return self.apply_config(change(self.cfg))

    def _refuse_if_stopped(self) -> None:
        if self._stopped:
            raise cfgmod.ConfigError("Louver is stopping")

    def apply_config(self, new: dict) -> dict:
        new = cfgmod.validate(new)
        with self.lock:
            self._refuse_if_stopped()
            old = self.cfg
            self.cfg = new
            self.cfg_rev += 1
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
                    if relight and any(
                        i in relight
                        for i in range(4)
                        if old["hub"]["groups"][i]["fans"] != new["hub"]["groups"][i]["fans"]
                    ):
                        # A cross-port gradient depends on every port's fan count.
                        relight += [
                            i
                            for i, e in enumerate(new["hub"]["lighting"])
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
            self._apply_aura_config(old["motherboard_rgb"], new["motherboard_rgb"])
        self.tick()
        return new

    def _apply_aura_config(self, old: dict, new: dict) -> None:
        if not new["enabled"]:
            if self.aura is not None:  # the lights stay as they are
                self.aura.close()
                self.aura = None
            self.aura_error = ""
            self._aura_dead = None
            return
        if not old["enabled"]:
            # Just turned on: try now, even on a controller that failed before.
            # Any other save leaves retries to _tick_aura's pace.
            self._aura_dead = None
            self._connect_aura(self._clock())
            return
        if self.aura is None or old == new:
            return
        keys = [ch.key for ch in self.aura.channels if self._aura_look(old, ch.key) != self._aura_look(new, ch.key)]
        try:
            self._push_aura(self.aura, keys)
        except OSError as e:
            self._drop_aura(e)

    # --- Lighting Lab -------------------------------------------------------
    def lab_show(self, req: dict) -> None:
        group = int(req.get("group", 0))
        ring = req.get("ring", "inner")
        if ring not in ("inner", "outer"):
            raise cfgmod.ConfigError("ring must be inner or outer")
        colors = [
            parse_color(c)
            for c in req.get("colors") or ["#ff0000", "#ffd400", "#00ff6a", "#0088ff", "#b000ff", "#ff00c8"]
        ]
        with self.lock:
            self._refuse_if_stopped()
            if self.hub is None:
                raise cfgmod.ConfigError("hub not connected")
            try:
                self.hub.raw_effect(
                    group,
                    ring,
                    int(req.get("mode", 1)),
                    colors,
                    speed=int(req.get("speed", 2)),
                    direction=str(req.get("direction", "cw")),
                    brightness=int(req.get("brightness", 4)),
                )
            except OSError as e:
                self._drop("hub", e)
                raise cfgmod.ConfigError(f"hub I/O error: {e}") from None
            self.lab_groups.add(group)

    def lab_end(self) -> None:
        with self.lock:
            self._refuse_if_stopped()
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
                "motherboard_rgb": {
                    "enabled": self.cfg["motherboard_rgb"]["enabled"],
                    "connected": self.aura is not None,
                    "name": AuraMainboard.NAME,
                    "firmware": self.aura_firmware,
                    "channels": [
                        {
                            "key": ch.key,
                            "name": ch.name,
                            "kind": ch.kind,
                            # ARGB headers: the config table doesn't say how many LEDs are on them.
                            "leds": ch.num_leds if ch.kind == "onboard" else None,
                            "rgb_headers": ch.rgb_headers,
                            "note": self.aura_notes.get(ch.key, ""),  # set: Louver doesn't drive it
                        }
                        for ch in (self.aura.channels if self.aura else [])
                    ],
                    "error": self.aura_error,
                },
                "error": self.error,
                "config_rev": self.cfg_rev,
            }

    def shutdown(self) -> None:
        with self.lock:
            self._stopped = True
            fans = self._mb_fans
            if fans is not None and fans.stale():  # moved since the last check: hand back at the new path
                fans.rescan()
            if fans is not None and fans.present:
                fans.release_all()  # BIOS curves take back over
            if self.aura is not None:  # its lights stay as they are
                self.aura.close()
                self.aura = None
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
        "pump_modes": list(cfgmod.PUMP_MODES),
        "presets": PRESETS,
        "hub_lighting_modes": UniHubALV2.lighting_modes() + _SOFTWARE_META,
        "strimer_lighting_modes": StrimerPlus.lighting_modes() + _SOFTWARE_META,
        "aura_lighting_modes": AuraMainboard.lighting_modes(),
        "themes": [{"name": n, "swatch": t["swatch"]} for n, t in THEMES.items()],
        "sensors": ctrl.sensors.list(),
    }


_STATIC = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/model.js": "model.js",
    "/style.css": "style.css",
    "/fonts/unbounded.woff2": "fonts/unbounded.woff2",
    "/fonts/martian-mono.woff2": "fonts/martian-mono.woff2",
}
_CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2"}


REQUEST_TIMEOUT = 10.0  # s a connection may stay silent, while sending its request or reading the reply
REQUEST_DEADLINE = 15.0  # s a connection has in all to send its request, however it trickles in
MAX_CONNECTIONS = 32
STOP_WAIT = 3.0  # s stop() waits for requests being answered
# The UI is these files and nothing else: no inline scripts, no other origins.
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


class _DeadlineReader(io.RawIOBase):
    """A connection's incoming bytes, by a deadline: REQUEST_TIMEOUT alone
    applies per recv, so a client sending a byte at a time could hold its
    slot indefinitely."""

    def __init__(self, sock: socket.socket, deadline: float, idle: float):
        self._sock, self._deadline, self._idle = sock, deadline, idle

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("request not received in time")
        self._sock.settimeout(min(self._idle, left))
        try:
            return self._sock.recv_into(b)
        finally:
            self._sock.settimeout(self._idle)  # the reply gets the idle timeout, not what's left


def make_handler(ctrl: Controller):
    idle, deadline = REQUEST_TIMEOUT, REQUEST_DEADLINE

    class Handler(BaseHTTPRequestHandler):
        server_version = "louver"
        timeout = idle

        def setup(self) -> None:
            super().setup()
            self.rfile.close()  # replaced: everything is read against the deadline
            self.rfile = io.BufferedReader(_DeadlineReader(self.connection, time.monotonic() + deadline, idle))

        def log_message(self, fmt, *args):  # quiet by default
            log.debug("http: " + fmt, *args)

        def address_string(self) -> str:
            return "unix"  # an AF_UNIX peer has no address

        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
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
            extra = {"Content-Security-Policy": _CSP} if f.suffix == ".html" else None
            self._send(200, f.read_bytes(), _CTYPES[f.suffix], extra)

        def do_PUT(self):
            if self.path not in ("/api/config", "/api/theme", "/api/zone-mode", "/api/lab"):
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length required"})
            if not 0 <= n <= MAX_BODY:
                # Negative too: rfile.read(-1) would read until the client stops.
                return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": f"body must be 0..{MAX_BODY} bytes"})
            try:
                raw = self.rfile.read(n)
            except OSError:  # timed out or reset mid-body
                self.close_connection = True
                return
            try:
                body = json.loads(raw)
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
                    new = ctrl.update_config(lambda cfg: apply_theme(cfg, name))
                elif self.path == "/api/zone-mode":
                    zone, mode = body.get("zone"), body.get("mode")
                    new = ctrl.update_config(lambda cfg: cfgmod.set_zone_mode(cfg, zone, mode))
                else:
                    new = ctrl.apply_config(body)
            except (ValueError, cfgmod.ConfigError) as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            return self._json(200, new)

    return Handler


def _peer_uid(sock: socket.socket) -> int:
    # struct ucred: pid_t pid (signed), uid_t uid, gid_t gid (unsigned)
    _pid, uid, _gid = struct.unpack(
        "iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII"))
    )
    return uid


def _private_dir(d: Path, tighten: bool) -> None:
    """``d`` exists, is a directory of ours and nobody else can write in it.
    Louver's own directory is made owner-only; a directory given with
    --socket is refused instead, rather than changing its mode. Only ``d``
    itself is checked, not the directories above it: --socket's help says
    to give it a directory only you can write to."""
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PermissionError(errno.EPERM, "not a directory of yours", str(d))
    if tighten and st.st_mode & 0o077:
        os.chmod(d, 0o700)
    elif st.st_mode & 0o022:
        raise PermissionError(errno.EPERM, "other accounts can write in this directory", str(d))


def _clear_stale(path: Path) -> None:
    """Remove a socket left by a copy that didn't shut down cleanly. Anything
    else at ``path`` is refused, and so is a socket someone still serves."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
        raise FileExistsError(errno.EEXIST, "something other than a Louver socket is there", str(path))
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except ConnectionRefusedError:
        pass  # nobody listening: stale
    else:  # the demo doesn't take the instance lock, so a live copy may be here
        raise OSError(errno.EADDRINUSE, "another Louver is serving this socket", str(path))
    finally:
        probe.close()
    os.unlink(path)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """A thread per connection, but at most MAX_CONNECTIONS of them: beyond
    that a new connection is closed at once instead of costing a thread.
    Only connections from our own account are served."""

    daemon_threads = True

    def __init__(self, path: Path, handler, uid: int | None = None):
        self._max = MAX_CONNECTIONS
        self._slots = threading.BoundedSemaphore(self._max)
        self.uid = os.getuid() if uid is None else uid
        self.path = path
        self._ident: tuple[int, int] | None = None
        super().__init__(str(path), handler)

    def server_bind(self) -> None:
        old = os.umask(0o177)  # the socket file is born 0600
        try:
            super().server_bind()
        finally:
            os.umask(old)
        st = os.lstat(self.path)
        self._ident = (st.st_dev, st.st_ino)

    def verify_request(self, request, client_address) -> bool:
        # The directory already keeps other accounts out; this is the backstop.
        try:
            return _peer_uid(request) == self.uid
        except OSError:
            return False

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def stop(self) -> None:
        """Stop serving, let requests being answered finish (for STOP_WAIT at
        most), and remove the socket file, if it's still ours. One still
        running after that is refused by the Controller once it has shut
        down, so it can't take the hardware back."""
        self.shutdown()
        self.server_close()
        end = time.monotonic() + STOP_WAIT
        for _ in range(self._max):  # every slot back: no handler is running
            if not self._slots.acquire(timeout=max(0.0, end - time.monotonic())):
                log.warning("stopping with a request still being answered")
                break
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return
        if (st.st_dev, st.st_ino) == self._ident:
            os.unlink(self.path)


def serve(ctrl: Controller, sock_path: Path) -> _Server:
    """Serve the API and the UI on ``sock_path``. Only one copy may hold the
    hardware lock, so a socket left there is stale unless the demo serves it."""
    sock_path = Path(sock_path)
    check_length(sock_path)
    _private_dir(sock_path.parent, tighten=sock_path.parent == socket_dir())
    _clear_stale(sock_path)
    httpd = _Server(sock_path, make_handler(ctrl))
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    return httpd
