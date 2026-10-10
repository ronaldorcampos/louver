"""Preset RGB themes: one click sets every hub port and the Strimer.

A theme's ``hub`` is either one lighting entry (applied to all four ports)
or a list of four. Missing fields fall back to lighting defaults.
"""

from __future__ import annotations

import copy

THEMES: dict[str, dict] = {
    "Rainbow Wave": {
        "swatch": ["#ff0000", "#ffd400", "#00ff6a", "#0088ff", "#b000ff"],
        "hub": {"mode": "Rainbow", "ring": "both", "speed": 2},
        "strimer": {"mode": "Rainbow", "speed": 2},
    },
    "Ocean": {
        "swatch": ["#001aff", "#0077ff", "#00c8ff", "#00ffd5"],
        "hub": {
            "mode": "Gradient",
            "ring": "both",
            "span_all": True,
            "colors": ["#001aff", "#0077ff", "#00c8ff", "#00ffd5"],
        },
        "strimer": {"mode": "Gradient", "span_all": True, "colors": ["#001aff", "#00c8ff", "#00ffd5"]},
    },
    "Sunset": {
        "swatch": ["#ff1e00", "#ff6a00", "#ff2d7a", "#8a00ff"],
        "hub": {
            "mode": "Gradient",
            "ring": "both",
            "span_all": True,
            "colors": ["#ff1e00", "#ff6a00", "#ff2d7a", "#8a00ff"],
        },
        "strimer": {"mode": "Gradient", "span_all": True, "colors": ["#ff1e00", "#ff6a00", "#ff2d7a", "#8a00ff"]},
    },
    "Cyberpunk": {
        "swatch": ["#ff00c8", "#00f0ff"],
        # AL fans' two rings in contrasting colours.
        "hub": {"mode": "Custom", "ring": "both", "custom": {"inner": ["#ff00c8"] * 48, "outer": ["#00f0ff"] * 72}},
        "strimer": {"mode": "Mixing", "colors": ["#ff00c8", "#00f0ff"], "speed": 1},
    },
    "Arctic": {
        "swatch": ["#ffffff", "#bfe9ff", "#4fb8ff"],
        "hub": {"mode": "Custom", "ring": "both", "custom": {"inner": ["#ffffff"] * 48, "outer": ["#4fb8ff"] * 72}},
        "strimer": {"mode": "Breathing", "colors": ["#bfe9ff"], "speed": 0},
    },
    "Inferno": {
        "swatch": ["#ff0000", "#ff5a00", "#ffb300"],
        "hub": {"mode": "Meteor", "ring": "both", "speed": 3, "colors": ["#ff0000", "#ff5a00", "#ffb300"]},
        "strimer": {"mode": "Meteor", "speed": 3, "colors": ["#ff0000", "#ff5a00", "#ffb300"]},
    },
    "Forest": {
        "swatch": ["#003d12", "#00a34a", "#9dff3a"],
        "hub": {"mode": "Gradient", "ring": "both", "span_all": True, "colors": ["#00591a", "#00c853", "#9dff3a"]},
        "strimer": {"mode": "Breathing", "colors": ["#00c853"], "speed": 0},
    },
    "Vaporwave": {
        "swatch": ["#ff71ce", "#b967ff", "#01cdfe", "#05ffa1"],
        "hub": {
            "mode": "Color Cycle",
            "ring": "both",
            "speed": 1,
            "colors": ["#ff71ce", "#b967ff", "#01cdfe", "#05ffa1"],
        },
        "strimer": {"mode": "Wave", "speed": 1, "colors": ["#ff71ce", "#b967ff", "#01cdfe", "#05ffa1"]},
    },
    "Thermal": {
        "swatch": ["#005aff", "#00dc78", "#ffd200", "#ff2800"],
        "hub": {"mode": "Temperature", "ring": "both", "sensor": "cpu", "temp_range": [35, 85]},
        "strimer": {"mode": "Temperature", "sensor": "gpu", "temp_range": [35, 80]},
    },
    "Stealth": {
        "swatch": ["#111111", "#2a2a2a"],
        "hub": {"mode": "Off"},
        "strimer": {"mode": "Off"},
    },
}


def apply_theme(cfg: dict, name: str) -> dict:
    """Return a copy of ``cfg`` with the theme's lighting applied."""
    if name not in THEMES:
        raise KeyError(name)
    theme = THEMES[name]
    out = copy.deepcopy(cfg)
    hub = theme["hub"]
    entries = hub if isinstance(hub, list) else [hub] * 4
    out["hub"]["lighting"] = [copy.deepcopy(e) for e in entries]
    out["hub"]["mb_rgb_sync"] = False
    out["strimer"]["lighting"] = copy.deepcopy(theme["strimer"])
    out["strimer"]["per_port"] = False
    out["strimer"]["mb_rgb_sync"] = False
    out["theme"] = name
    return out
