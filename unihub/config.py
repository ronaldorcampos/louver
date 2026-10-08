"""Persistent JSON configuration (~/.config/unihub/config.json)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .curves import PRESETS, normalize
from .devices import STRIMER_MODES, ALV2_INNER_MODES, ALV2_OUTER_MODES, parse_color

FAN_MODES = ("quiet", "standard", "high", "full", "manual", "curve", "mb_sync")
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
    return {"mode": "Rainbow", "ring": "both", "colors": ["#ff0000"], "speed": 2, "brightness": 4, "direction": "cw"}


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
    },
    "strimer": {"mb_rgb_sync": False, "lighting": _default_light()},
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
    lights = cfg["hub"]["lighting"]
    if len(lights) != 4:
        raise ConfigError("hub.lighting must have 4 entries")
    hub_modes = set(ALV2_INNER_MODES) | set(ALV2_OUTER_MODES)
    for i, light in enumerate(lights):
        lights[i] = _merge(_default_light(), light)
        _validate_light(lights[i], hub_modes, f"hub lighting {i}")
    cfg["strimer"]["lighting"] = _merge(_default_light(), cfg["strimer"]["lighting"])
    _validate_light(cfg["strimer"]["lighting"], STRIMER_MODES, "strimer lighting")
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
