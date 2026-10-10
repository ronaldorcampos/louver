"""Control loop + local HTTP API/UI server."""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import aura, ramrgb
from . import config as cfgmod
from .aura import AuraMainboard, HidrawAccess
from .curves import PRESETS, Governor, interpolate
from .devices import DRIVERS, VENDOR_ID, LightingEffect, StrimerPlus, UniHubALV2, parse_color
from .hidraw import HidrawTransport, enumerate_hidraw
from .mbfans import MotherboardFans
from .ramrgb import RamClient, RamHelperAccess, RamHelperError, RamStick
from .rgbfx import SOFTWARE_MODES, render_hub_port, render_strimer_port
from .sensors import SensorHub
from .themes import THEMES, apply_theme

log = logging.getLogger("louver")

WEB_DIR = Path(__file__).with_name("web")
RECONNECT_EVERY = 5.0
KEEPALIVE_EVERY = 10.0
TEMP_LIGHT_STEP = 2.0  # deg C a Temperature-mode colour must move before it's re-sent
# RAM RGB: a scan the helper says might find more (I/O errors, a busy bus) is
# asked for again after RECONNECT_EVERY, then twice as long each time, up to this.
RAM_RESCAN_MAX = 300.0
RAM_LOG_REPEAT = 600.0  # the same RAM RGB problem is logged again only after this long


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
        ram_access=None,
    ):
        self.cfg = cfgmod.validate(cfg)
        # Bumped on every config change, so a window that's been open a while
        # (or hidden behind the tray) can tell its copy is out of date.
        self.cfg_rev = 0
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
        # RAM RGB: the louver-ram root helper does the SMBus I/O; Louver only
        # talks to its socket, and only while RAM RGB is turned on.
        self.ram_access = ram_access if ram_access is not None else RamHelperAccess()
        self.ram: RamClient | None = None
        self.ram_sticks: list[RamStick] = []
        self.ram_error = ""
        self._ram_dead = False  # the helper found nothing to drive: not asked again until a re-enable
        self._ram_next_try = -1e9  # next connect attempt
        self._ram_rescan_next = False  # the next connect asks the helper to scan again
        self._ram_rescan_at: float | None = None  # while connected: when to ask for a scan again
        self._ram_backoff = RECONNECT_EVERY
        self._ram_frames: dict[int, list] | None = None  # what each stick was last sent
        self._ram_sent_at = -1e9
        self._ram_logged = ("", -1e9)  # the last problem logged, and when
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

    def _tick_ram(self, now: float) -> None:
        """While there's no helper connection: try to connect, every
        RECONNECT_EVERY (or on the rescan backoff). While there is one: send
        the frame when it changed and every KEEPALIVE_EVERY, and ask for a
        rescan when the last scan hit I/O errors."""
        if not self.cfg["ram_rgb"]["enabled"]:
            return
        if self.ram is None:
            if not self._ram_dead and now >= self._ram_next_try:
                self._connect_ram(now, rescan=self._ram_rescan_next)
            return
        if self._ram_rescan_at is not None and now >= self._ram_rescan_at:
            client = self.ram
            try:
                listing = client.list(rescan=True)
            except (OSError, ValueError, RamHelperError) as e:
                self._drop_ram(e)
                return
            self._take_ram_listing(client, listing, now)
        if self.ram is not None:
            self._push_ram(now)

    def _connect_ram(self, now: float, rescan: bool = False) -> None:
        self._ram_next_try = now + RECONNECT_EVERY
        path = getattr(self.ram_access, "path", ramrgb.SOCKET_PATH)
        try:
            client = self.ram_access.connect()
        except OSError as e:
            self._ram_failed(ramrgb.connect_error(path, e))
            return
        try:
            listing = client.list(rescan=rescan)
        except (OSError, ValueError, RamHelperError) as e:
            client.close()
            self._ram_failed(f"the RAM helper didn't answer: {e}")
            return
        self._take_ram_listing(client, listing, now)
        if self.ram is not None:
            self._push_ram(now)

    def _next_ram_backoff(self) -> float:
        wait = self._ram_backoff
        self._ram_backoff = min(RAM_RESCAN_MAX, wait * 2)
        return wait

    def _take_ram_listing(self, client: RamClient, listing: ramrgb.Listing, now: float) -> None:
        """Use what the helper found: drive its sticks, and plan another scan
        when it says one might find more."""
        skipped = "; ".join(f"{addr:#04x}: {reason}" for addr, reason in listing.skipped)
        if not listing.sticks:
            # Closed, so the helper can exit when there's nothing to ask it again.
            self.ram = client
            self._close_ram()
            why = (listing.error or "no stick found") + (f" ({skipped})" if skipped else "")
            if listing.retry:
                wait = self._next_ram_backoff()
                self._ram_next_try, self._ram_rescan_next = now + wait, True
                self._ram_failed(f"{why}; scanning again in {wait:.0f} s")
            else:
                self._ram_dead = True
                self._ram_failed(f"{why}; not asked again until it's turned off and on")
            return
        if self.ram is not client:
            log.info("RAM RGB: %d stick(s) on %s", len(listing.sticks), listing.bus)
        if listing.sticks != self.ram_sticks:
            self._ram_frames = None
        self.ram, self.ram_sticks = client, listing.sticks
        self.ram_error = ""
        self._ram_rescan_next = False
        if listing.retry:
            wait = self._next_ram_backoff()
            self._ram_rescan_at = now + wait
            self._ram_log(f"some addresses didn't answer properly ({skipped}); scanning again in {wait:.0f} s")
        else:
            self._ram_rescan_at, self._ram_backoff = None, RECONNECT_EVERY

    def _ram_log(self, msg: str) -> None:
        """Retried every few seconds: log each new problem once, and the same
        one again only after RAM_LOG_REPEAT."""
        now = self._clock()
        last, at = self._ram_logged
        if msg != last or now - at >= RAM_LOG_REPEAT:
            log.warning("RAM RGB: %s", msg)
            self._ram_logged = (msg, now)

    def _ram_failed(self, msg: str) -> None:
        self._ram_log(msg)
        self.ram_error = msg

    def _push_ram(self, now: float) -> None:
        client = self.ram
        if client is None:
            return
        look = self.cfg["ram_rgb"]["lighting"]
        frames = {st.addr: ramrgb.render(look, st.leds) for st in self.ram_sticks}
        if frames == self._ram_frames and now - self._ram_sent_at < KEEPALIVE_EVERY:
            return
        try:
            for addr, colors in frames.items():
                client.set(addr, colors)
        except RamHelperError as e:
            if e.code == "too_soon":  # faster than the helper allows: the next tick sends it
                self._ram_frames = None
                return
            self._drop_ram(e)
            return
        except (OSError, ValueError) as e:
            self._drop_ram(e)
            return
        self._ram_frames, self._ram_sent_at = frames, now

    def _drop_ram(self, err: Exception) -> None:
        self._ram_log(f"disconnected, will reconnect: {err}")
        self.ram_error = f"disconnected: {err}"
        self._close_ram()

    def _close_ram(self) -> None:
        client, self.ram = self.ram, None
        self.ram_sticks = []
        self._ram_frames, self._ram_rescan_at = None, None
        if client is not None:
            try:
                client.close()
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
            self._tick_ram(now)

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
            return self.apply_config(change(self.cfg))

    def apply_config(self, new: dict) -> dict:
        new = cfgmod.validate(new)
        with self.lock:
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
            self._apply_ram_config(old["ram_rgb"], new["ram_rgb"])
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

    def _apply_ram_config(self, old: dict, new: dict) -> None:
        if not new["enabled"]:
            self._close_ram()  # nothing more is sent: the sticks keep their last frame
            self.ram_error = ""
            self._ram_dead, self._ram_rescan_next, self._ram_rescan_at = False, False, None
            self._ram_backoff, self._ram_logged = RECONNECT_EVERY, ("", -1e9)
            return
        if not old["enabled"]:
            # Just turned on: ask now, for a fresh scan. Any other save leaves
            # retries to _tick_ram's pace.
            self._ram_dead = False
            self._connect_ram(self._clock(), rescan=True)
            return
        self._push_ram(self._clock())  # sends only if the frame changed

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
                "ram_rgb": {
                    "enabled": self.cfg["ram_rgb"]["enabled"],
                    "connected": self.ram is not None,
                    "sticks": [st.describe() for st in self.ram_sticks],
                    "error": self.ram_error,
                },
                "error": self.error,
                "config_rev": self.cfg_rev,
            }

    def shutdown(self) -> None:
        with self.lock:
            if self.mb is not None:
                self.mb.release_all()  # BIOS curves take back over
            if self.aura is not None:  # its lights stay as they are
                self.aura.close()
                self.aura = None
            self._close_ram()  # so do the sticks'
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
        "aura_lighting_modes": AuraMainboard.lighting_modes(),
        "ram_lighting_modes": ramrgb.lighting_modes(),
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


def make_handler(ctrl: Controller, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "louver"

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
            if self.headers.get("X-Louver") != "1":
                return self._json(HTTPStatus.FORBIDDEN, {"error": "missing X-Louver header"})
            if self.path not in ("/api/config", "/api/theme", "/api/zone-mode", "/api/lab"):
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
