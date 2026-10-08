"""Persistent JSON configuration (~/.config/unihub/config.json)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .curves import PRESETS, normalize
from .devices import STRIMER_MODES, ALV2_INNER_MODES, ALV2_OUTER_MODES, parse_color
from .rgbfx import SOFTWARE_MODES

FAN_MODES = ("quiet", "standard", "high", "full", "manual", "curve", "mb_sync")
# Motherboard headers: "bios" leaves the header on the board's own curve.
MB_FAN_MODES = ("bios", "quiet", "standard", "high", "full", "manual", "curve")
EXIT_ACTIONS = ("mb_sync", "hold", "fixed")


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "unihub" / "config.json"


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
        "mode": "Rainbow", "ring": "both", "colors": ["#ff0000"], "speed": 2, "brightness": 4,
        "direction": "cw", "sensor": "cpu", "temp_range": [35, 85], "span_all": False, "custom": {},
    }

STRIMER_PORTS = 12


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


def validate(cfg: dict) -> dict:
    """Fill defaults and reject invalid values. Returns a new dict."""
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
    return cfg


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
