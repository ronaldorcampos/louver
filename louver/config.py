"""Persistent JSON configuration (~/.config/louver/config.json)."""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

from .aura import MODES as AURA_MODES
from .curves import PRESETS, normalize
from .devices import ALV2_INNER_MODES, ALV2_OUTER_MODES, STRIMER_MODES, parse_color
from .ramrgb import MAX_COLORS as RAM_MAX_COLORS
from .ramrgb import MODES as RAM_MODES
from .rgbfx import SOFTWARE_MODES
from .themes import THEMES

FAN_MODES = ("quiet", "standard", "high", "full", "manual", "curve", "mb_sync")
# Motherboard headers: "bios" leaves the header on the board's own curve.
MB_FAN_MODES = ("bios", "quiet", "standard", "high", "full", "manual", "curve")
EXIT_ACTIONS = ("mb_sync", "hold", "fixed")


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "louver" / "config.json"


def _default_group(i: int) -> dict:
    return {
        "name": f"Port {i + 1}",
        "fans": 3,
        "mode": "standard",
        "manual_percent": 50,
        "sensor": "cpu",
        "curve": [list(p) for p in PRESETS["standard"]],
    }


def _default_light() -> dict:
    return {
        "mode": "Rainbow",
        "ring": "both",
        "colors": ["#ff0000"],
        "speed": 2,
        "brightness": 4,
        "direction": "cw",
        "sensor": "cpu",
        "temp_range": [35, 85],
        "span_all": False,
        "custom": {},
    }


STRIMER_PORTS = 12


def _default_aura_light() -> dict:
    return {"mode": "Rainbow", "colors": ["#ff0000"]}


def _default_ram_light() -> dict:
    return {"mode": "Static", "colors": ["#ffffff"]}


# Channel keys as aura.decode_config names them.
_AURA_KEY = re.compile(r"onboard|argb-[1-9][0-9]?")


DEFAULTS: dict = {
    "poll_interval": 1.0,
    "min_percent": 0,
    "on_exit": "mb_sync",
    "on_exit_percent": 60,
    "listen": "127.0.0.1:47321",
    "hub": {
        "mb_rgb_sync": False,
        "groups": [_default_group(i) for i in range(4)],
        "lighting": [_default_light() for _ in range(4)],
        # Effect numbers verified on this hub in the Lighting Lab: {"inner": {name: n}, "outer": {...}}
        "mode_map": {"inner": {}, "outer": {}},
    },
    "strimer": {
        "mb_rgb_sync": False,
        "per_port": False,
        "lighting": _default_light(),
        "ports": [_default_light() for _ in range(STRIMER_PORTS)],
    },
    "theme": None,
    "ui": {"show_empty_ports": False},
    # Desktop app (window + tray). Fan control runs regardless, as a service.
    "app": {"start_on_login": False, "start_minimized": True, "close_to_tray": True},
    # Opt-in per header: [{"index": 2, "name": ..., "mode": "bios"|..., ...}]
    "motherboard": {"headers": []},
    # ASUS Aura USB motherboard RGB. Off until turned on in Settings: while
    # it's off, Louver never opens the board's Aura controller.
    "motherboard_rgb": {
        "enabled": False,
        "per_channel": False,
        "lighting": _default_aura_light(),  # every channel, unless per_channel
        "channels": {},  # per_channel looks: {"onboard" | "argb-<n>": {"mode", "colors"}}
    },
    # Corsair DDR5 RAM RGB, through the louver-ram root helper. Off until
    # turned on in Settings: while it's off, Louver never contacts the helper.
    "ram_rgb": {
        "enabled": False,
        "lighting": _default_ram_light(),  # every stick
    },
}


class ConfigError(ValueError):
    pass


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _validate_light(light: dict, modes, where: str) -> None:
    if light.get("mode") not in modes:
        raise ConfigError(f"{where}: unknown lighting mode {light.get('mode')!r}")
    if light.get("ring", "both") not in ("inner", "outer", "both"):
        raise ConfigError(f"{where}: ring must be inner/outer/both")
    for c in light.get("colors", []):
        try:
            parse_color(c)
        except (ValueError, TypeError) as e:
            raise ConfigError(f"{where}: {e}") from None
    if not 0 <= int(light.get("speed", 2)) <= 4:
        raise ConfigError(f"{where}: speed must be 0..4")
    if not -1 <= int(light.get("brightness", 4)) <= 4:
        raise ConfigError(f"{where}: brightness must be -1..4")
    tr = light.get("temp_range", [35, 85])
    if not (isinstance(tr, list) and len(tr) == 2 and float(tr[0]) < float(tr[1])):
        raise ConfigError(f"{where}: temp_range must be [low, high] with low < high")
    custom = light.get("custom") or {}
    if not isinstance(custom, dict):
        raise ConfigError(f"{where}: custom must be an object")
    for ring, limit in (("inner", 48), ("outer", 72), ("leds", 27)):
        leds = custom.get(ring, [])
        if not isinstance(leds, list) or len(leds) > limit:
            raise ConfigError(f"{where}: custom.{ring} must be a list of at most {limit} colours")
        for c in leds:
            try:
                parse_color(c)
            except (ValueError, TypeError) as e:
                raise ConfigError(f"{where}: {e}") from None


def _validate_aura_light(light, where: str) -> dict:
    if not isinstance(light, dict):
        raise ConfigError(f"{where}: must be an object")
    full = _merge(_default_aura_light(), light)
    if set(full) != {"mode", "colors"}:
        raise ConfigError(f"{where}: only mode and colors can be set")
    if full["mode"] not in AURA_MODES:
        raise ConfigError(f"{where}: unknown effect {full['mode']!r}")
    colors = full["colors"]
    if not isinstance(colors, list) or len(colors) != 1:
        raise ConfigError(f"{where}: colors must be a list of one colour")
    try:
        parse_color(colors[0])
    except (ValueError, TypeError) as e:
        raise ConfigError(f"{where}: {e}") from None
    return full


def _validate_ram_light(light, where: str) -> dict:
    if not isinstance(light, dict):
        raise ConfigError(f"{where}: must be an object")
    full = _merge(_default_ram_light(), light)
    if set(full) != {"mode", "colors"}:
        raise ConfigError(f"{where}: only mode and colors can be set")
    if full["mode"] not in RAM_MODES:
        raise ConfigError(f"{where}: mode must be one of {RAM_MODES}")
    colors = full["colors"]
    if not isinstance(colors, list) or not 1 <= len(colors) <= RAM_MAX_COLORS:
        raise ConfigError(f"{where}: colors must be a list of 1..{RAM_MAX_COLORS} colours")
    for c in colors:
        try:
            parse_color(c)
        except (ValueError, TypeError) as e:
            raise ConfigError(f"{where}: {e}") from None
    return full


def validate(cfg: dict) -> dict:
    """Fill defaults and reject invalid values. Returns a new dict."""
    theme = cfg.get("theme")
    if isinstance(theme, str) and theme in THEMES:
        # A config from before motherboard or RAM RGB: start it on the theme in use.
        if "motherboard_rgb" not in cfg:
            cfg = {**cfg, "motherboard_rgb": {"lighting": copy.deepcopy(THEMES[theme]["aura"])}}
        if "ram_rgb" not in cfg:
            cfg = {**cfg, "ram_rgb": {"lighting": copy.deepcopy(THEMES[theme]["ram"])}}
    cfg = _merge(DEFAULTS, cfg)
    if not 0.25 <= float(cfg["poll_interval"]) <= 30:
        raise ConfigError("poll_interval must be 0.25..30 s")
    if not 0 <= int(cfg["min_percent"]) <= 100:
        raise ConfigError("min_percent must be 0..100")
    if cfg["on_exit"] not in EXIT_ACTIONS:
        raise ConfigError(f"on_exit must be one of {EXIT_ACTIONS}")
    groups = cfg["hub"]["groups"]
    if len(groups) != 4:
        raise ConfigError("hub.groups must have 4 entries")
    for i, g in enumerate(groups):
        full = _merge(_default_group(i), g)
        if full["mode"] not in FAN_MODES:
            raise ConfigError(f"group {i}: mode must be one of {FAN_MODES}")
        if not 0 <= int(full["fans"]) <= 6:
            raise ConfigError(f"group {i}: fans must be 0..6")
        if not 0 <= float(full["manual_percent"]) <= 100:
            raise ConfigError(f"group {i}: manual_percent must be 0..100")
        try:
            full["curve"] = [list(p) for p in normalize(full["curve"])]
        except (ValueError, TypeError) as e:
            raise ConfigError(f"group {i}: bad curve: {e}") from None
        groups[i] = full
    cfg["app"].pop("show_tray", None)  # retired: the tray icon now shows whenever the window is closed
    for key, val in cfg["app"].items():
        if key not in DEFAULTS["app"] or not isinstance(val, bool):
            raise ConfigError(f"app.{key}: must be one of {sorted(DEFAULTS['app'])} with a true/false value")
    seen = set()
    headers = cfg["motherboard"]["headers"]
    for k, h in enumerate(headers):
        if not isinstance(h, dict) or not isinstance(h.get("index"), int) or not 1 <= h["index"] <= 16:
            raise ConfigError(f"motherboard header {k}: index must be an integer 1..16")
        if h["index"] in seen:
            raise ConfigError(f"motherboard header {h['index']} listed twice")
        seen.add(h["index"])
        full = _merge({**_default_group(0), "name": f"Header {h['index']}", "mode": "bios"}, h)
        full.pop("fans", None)
        if full["mode"] not in MB_FAN_MODES:
            raise ConfigError(f"motherboard header {h['index']}: mode must be one of {MB_FAN_MODES}")
        if not 0 <= float(full["manual_percent"]) <= 100:
            raise ConfigError(f"motherboard header {h['index']}: manual_percent must be 0..100")
        try:
            full["curve"] = [list(p) for p in normalize(full["curve"])]
        except (ValueError, TypeError) as e:
            raise ConfigError(f"motherboard header {h['index']}: bad curve: {e}") from None
        headers[k] = full
    mm = cfg["hub"]["mode_map"]
    known = set(ALV2_INNER_MODES) | set(ALV2_OUTER_MODES)
    for ring in ("inner", "outer"):
        table = mm.get(ring, {})
        if not isinstance(table, dict):
            raise ConfigError(f"mode_map.{ring} must be an object")
        for name, num in table.items():
            if name not in known:
                raise ConfigError(f"mode_map.{ring}: unknown effect {name!r}")
            if not isinstance(num, int) or not 0 <= num <= 255:
                raise ConfigError(f"mode_map.{ring}.{name}: must be an integer 0..255")
    lights = cfg["hub"]["lighting"]
    if len(lights) != 4:
        raise ConfigError("hub.lighting must have 4 entries")
    hub_modes = set(ALV2_INNER_MODES) | set(ALV2_OUTER_MODES) | set(SOFTWARE_MODES)
    for i, light in enumerate(lights):
        lights[i] = _merge(_default_light(), light)
        _validate_light(lights[i], hub_modes, f"hub lighting {i}")
    strimer_modes = set(STRIMER_MODES) | set(SOFTWARE_MODES)
    cfg["strimer"]["lighting"] = _merge(_default_light(), cfg["strimer"]["lighting"])
    _validate_light(cfg["strimer"]["lighting"], strimer_modes, "strimer lighting")
    ports = cfg["strimer"]["ports"]
    if len(ports) != STRIMER_PORTS:
        raise ConfigError(f"strimer.ports must have {STRIMER_PORTS} entries")
    for i, light in enumerate(ports):
        ports[i] = _merge(_default_light(), light)
        _validate_light(ports[i], strimer_modes, f"strimer port {i + 1}")
    mrgb = cfg["motherboard_rgb"]
    if set(mrgb) != set(DEFAULTS["motherboard_rgb"]):
        raise ConfigError(f"motherboard_rgb: keys must be {sorted(DEFAULTS['motherboard_rgb'])}")
    for key in ("enabled", "per_channel"):
        if not isinstance(mrgb[key], bool):
            raise ConfigError(f"motherboard_rgb.{key} must be true or false")
    mrgb["lighting"] = _validate_aura_light(mrgb["lighting"], "motherboard_rgb lighting")
    if not isinstance(mrgb["channels"], dict):
        raise ConfigError("motherboard_rgb.channels must be an object")
    for key, light in mrgb["channels"].items():
        if not _AURA_KEY.fullmatch(key):
            raise ConfigError(f"motherboard_rgb channel {key!r}: must be onboard or argb-<n>")
        mrgb["channels"][key] = _validate_aura_light(light, f"motherboard_rgb channel {key}")
    ram = cfg["ram_rgb"]
    if not isinstance(ram, dict) or set(ram) != set(DEFAULTS["ram_rgb"]):
        raise ConfigError(f"ram_rgb: keys must be {sorted(DEFAULTS['ram_rgb'])}")
    if not isinstance(ram["enabled"], bool):
        raise ConfigError("ram_rgb.enabled must be true or false")
    ram["lighting"] = _validate_ram_light(ram["lighting"], "ram_rgb lighting")
    return cfg


def set_zone_mode(cfg: dict, zone: str, mode: str) -> dict:
    """Return a copy of ``cfg`` with one zone's speed mode changed (the tray's
    quick control). ``zone`` is "hub-<0..3>" or "mb-<1..16>"; a header with no
    entry yet gets one. Raises ConfigError on anything else."""
    kind, _, num = str(zone).partition("-")
    if not num.isdigit() or num != str(int(num)):
        raise ConfigError(f"unknown zone {zone!r}")
    n = int(num)
    out = copy.deepcopy(cfg)
    if kind == "hub" and 0 <= n <= 3:
        if mode not in FAN_MODES:
            raise ConfigError(f"mode must be one of {FAN_MODES}")
        out["hub"]["groups"][n]["mode"] = mode
    elif kind == "mb" and 1 <= n <= 16:
        if mode not in MB_FAN_MODES:
            raise ConfigError(f"mode must be one of {MB_FAN_MODES}")
        headers = out["motherboard"]["headers"]
        entry = next((h for h in headers if h.get("index") == n), None)
        if entry is None:
            headers.append({"index": n, "mode": mode})  # validate() fills the rest
        else:
            entry["mode"] = mode
    else:
        raise ConfigError(f"unknown zone {zone!r}")
    return out


def load(path: Path | None = None) -> dict:
    path = path or default_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return validate({})
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return validate(raw)


def save(cfg: dict, path: Path | None = None) -> None:
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, path)
