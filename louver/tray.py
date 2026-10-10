"""What the tray menu shows: pure functions over the daemon's config, state
and sensor list, so they can be tested without GTK. gui.py turns them into
menu items."""

from __future__ import annotations

from typing import NamedTuple

# The quick speeds the tray offers; anything else is set in the window.
# Labels match the window's mode chips (web/app.js MODE_LABELS).
SPEEDS = (("quiet", "Quiet"), ("standard", "Standard"), ("high", "High"), ("full", "Full"))
MODE_LABELS = {
    **dict(SPEEDS), "manual": "Manual", "curve": "Custom curve", "mb_sync": "MB sync", "bios": "BIOS",
}


class Zone(NamedTuple):
    key: str  # "hub-2" / "mb-7": the window's #hash for it, and /api/zone-mode's "zone"
    name: str
    mode: str
    rpm: int
    failsafe: bool
    manual_percent: float


def zones(cfg: dict, state: dict) -> list[Zone]:
    """Zones with a speed the tray can set: hub ports with fans, and
    motherboard headers Louver controls. Headers on BIOS control stay off the
    tray, so a stray click can't take a pump off the board's curve."""
    out = []
    groups = state.get("hub", {}).get("groups") or []
    for i, g in enumerate(cfg["hub"]["groups"]):
        if g["fans"] > 0:
            s = groups[i] if i < len(groups) else {}
            out.append(Zone(f"hub-{i}", g["name"], g["mode"], int(s.get("rpm") or 0), bool(s.get("failsafe")),
                            g["manual_percent"]))
    mb = state.get("motherboard") or {}
    if mb.get("present") and mb.get("writable"):
        live = {h["index"]: h for h in mb.get("headers", [])}
        for h in sorted(cfg["motherboard"]["headers"], key=lambda h: h["index"]):
            s = live.get(h["index"])
            if s is not None and h["mode"] != "bios":
                out.append(Zone(f"mb-{h['index']}", h["name"], h["mode"], int(s.get("rpm") or 0),
                                bool(s.get("failsafe")), h["manual_percent"]))
    return out


def mode_label(z: Zone) -> str:
    if z.mode == "manual":
        return f"Manual {z.manual_percent:.0f}%"
    return MODE_LABELS.get(z.mode, z.mode)


def zone_label(z: Zone) -> str:
    warn = "⚠ " if z.failsafe else ""
    return f"{warn}{z.name} · {mode_label(z)} · {z.rpm} rpm"


def status_line(cfg: dict | None, state: dict | None, labels: dict[str, str]) -> str:
    """The tray's first line: the temperatures the zones follow (up to three),
    or what's wrong. labels maps sensor ids to names (from /api/meta)."""
    if cfg is None or state is None:
        return "Louver service not running"
    if not state.get("hub", {}).get("connected"):
        return "UNI HUB not connected"
    temps: dict[str, float] = {}
    hub = state["hub"].get("groups") or []
    for g, s in zip(cfg["hub"]["groups"], hub):
        if g["fans"] > 0 and g["mode"] != "mb_sync" and s.get("temp") is not None:
            temps.setdefault(g["sensor"], s["temp"])
    live = {h["index"]: h for h in (state.get("motherboard") or {}).get("headers", [])}
    for h in cfg["motherboard"]["headers"]:
        s = live.get(h["index"], {})
        if h["mode"] != "bios" and s.get("temp") is not None:
            temps.setdefault(h["sensor"], s["temp"])
    parts = [f"{labels.get(sid, sid)} {t:.0f}°C" for sid, t in list(temps.items())[:3]]
    if any(s.get("failsafe") for s in [*hub, *live.values()]):
        parts.append("no temperature: fans at 100%")
    return " · ".join(parts) or "Louver"


def signature(cfg: dict | None, state: dict | None, themes: list[str]) -> tuple:
    """Everything that changes the menu's shape or its radio marks (but not
    the live rpm/temperature text), so the menu is rebuilt only when needed."""
    if cfg is None or state is None:
        return ("down", tuple(themes))
    return (tuple((z.key, z.name, z.mode, z.manual_percent) for z in zones(cfg, state)), cfg.get("theme"), tuple(themes))
