"""Fan curves and speed smoothing."""

from __future__ import annotations

from collections.abc import Sequence

Point = tuple[float, float]  # (deg C, percent)

# Shaped after L-Connect 3's Quiet / Standard / High Speed / Full Speed presets.
PRESETS: dict[str, list[Point]] = {
    "quiet": [(30, 20), (50, 30), (65, 45), (75, 65), (85, 100)],
    "standard": [(30, 30), (50, 45), (65, 60), (75, 80), (85, 100)],
    "high": [(30, 50), (50, 65), (65, 80), (75, 90), (80, 100)],
    "full": [(0, 100), (100, 100)],
}


def normalize(points: Sequence[Sequence[float]]) -> list[Point]:
    pts = sorted((float(t), max(0.0, min(100.0, float(p)))) for t, p in points)
    if not pts:
        raise ValueError("curve needs at least one point")
    return pts


def interpolate(points: Sequence[Point], temp: float) -> float:
    """Piecewise-linear lookup, clamped flat beyond the first/last point."""
    if temp <= points[0][0]:
        return points[0][1]
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if temp <= t1:
            if t1 == t0:
                return p1
            return p0 + (p1 - p0) * (temp - t0) / (t1 - t0)
    return points[-1][1]


class Governor:
    """Rate-limits speed changes: rise fast (cooling is urgent), fall slowly
    (so a brief load spike doesn't make the fans surge and drop audibly)."""

    def __init__(self, up_per_s: float = 25.0, down_per_s: float = 4.0):
        self.up = up_per_s
        self.down = down_per_s
        self.current: float | None = None

    def step(self, target: float, dt: float) -> float:
        if self.current is None:
            self.current = target
        elif target > self.current:
            self.current = min(target, self.current + self.up * dt)
        else:
            self.current = max(target, self.current - self.down * dt)
        return self.current

    def reset(self) -> None:
        self.current = None
