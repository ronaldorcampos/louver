"""Software lighting: per-LED frames rendered here and pushed through the
controllers' static mode (which accepts an explicit colour per LED).

Software modes:
  Custom       explicit per-LED colours painted in the UI
  Gradient     colour stops spread across fans (optionally across all ports)
  Temperature  one colour that tracks a sensor: blue -> green -> yellow -> red
"""

from __future__ import annotations

from collections.abc import Sequence

from .devices import Color, parse_color

SOFTWARE_MODES = ("Custom", "Gradient", "Temperature")

HUB_INNER, HUB_OUTER, HUB_FANS = 8, 12, 6
STRIMER_LEDS = 27

_THERMAL_STOPS: tuple[Color, ...] = ((0, 90, 255), (0, 220, 120), (255, 210, 0), (255, 40, 0))
BLACK: Color = (0, 0, 0)


def lerp(a: Color, b: Color, t: float) -> Color:
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))  # type: ignore[return-value]


def gradient(stops: Sequence[Color], t: float) -> Color:
    if not stops:
        return BLACK
    if len(stops) == 1:
        return stops[0]
    t = max(0.0, min(1.0, t)) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    return lerp(stops[i], stops[i + 1], t - i)


def thermal(temp: float | None, lo: float, hi: float) -> Color:
    if temp is None:
        return (255, 0, 255)  # magenta = "no reading", impossible to mistake for a temperature
    return gradient(_THERMAL_STOPS, (temp - lo) / max(1e-6, hi - lo))


def _colors(entry: dict) -> list[Color]:
    return [parse_color(c) for c in entry.get("colors", [])] or [(255, 255, 255)]


def _ring_mask(entry: dict) -> tuple[bool, bool]:
    ring = entry.get("ring", "both")
    return ring in ("inner", "both"), ring in ("outer", "both")


def _custom_ring(entry: dict, ring: str, leds: int) -> list[Color]:
    raw = (entry.get("custom") or {}).get(ring) or []
    out = [parse_color(c) for c in raw[: HUB_FANS * leds]]
    return out + [BLACK] * (HUB_FANS * leds - len(out))


def render_hub_port(
    entry: dict, group: int, fan_counts: Sequence[int], temp: float | None = None
) -> tuple[list[Color], list[Color]]:
    """Return (inner 6x8, outer 6x12) LED colours for one hub port."""
    mode = entry["mode"]
    if mode == "Custom":
        return _custom_ring(entry, "inner", HUB_INNER), _custom_ring(entry, "outer", HUB_OUTER)

    if mode == "Gradient":
        stops = _colors(entry)
        if entry.get("span_all"):
            total = max(1, sum(fan_counts))
            offset = sum(fan_counts[:group])
        else:
            total, offset = max(1, fan_counts[group]), 0
        per_fan = [
            gradient(stops, (offset + f) / max(1, total - 1)) if f < fan_counts[group] else BLACK
            for f in range(HUB_FANS)
        ]
    elif mode == "Temperature":
        lo, hi = entry.get("temp_range", [35, 85])
        per_fan = [thermal(temp, lo, hi)] * HUB_FANS
    else:
        raise ValueError(f"{mode!r} is not a software mode")

    use_inner, use_outer = _ring_mask(entry)
    inner = [c if use_inner else BLACK for c in per_fan for _ in range(HUB_INNER)]
    outer = [c if use_outer else BLACK for c in per_fan for _ in range(HUB_OUTER)]
    return inner, outer


def render_strimer_port(entry: dict, port: int, port_count: int, temp: float | None = None) -> list[Color]:
    mode = entry["mode"]
    if mode == "Custom":
        raw = (entry.get("custom") or {}).get("leds") or []
        out = [parse_color(c) for c in raw[:STRIMER_LEDS]]
        return out + [BLACK] * (STRIMER_LEDS - len(out))
    if mode == "Gradient":
        stops = _colors(entry)
        if entry.get("span_all", True):
            return [gradient(stops, port / max(1, port_count - 1))] * STRIMER_LEDS
        return [gradient(stops, i / (STRIMER_LEDS - 1)) for i in range(STRIMER_LEDS)]
    if mode == "Temperature":
        lo, hi = entry.get("temp_range", [35, 85])
        return [thermal(temp, lo, hi)] * STRIMER_LEDS
    raise ValueError(f"{mode!r} is not a software mode")
