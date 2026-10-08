"""Protocol drivers for Lian Li ENE-based wired controllers.

Byte layouts follow the community reverse-engineering in
sgtaziz/lian-li-linux (MIT) and EightB1ts/uni-sync, cross-checked against
this hub's HID report descriptor (report 0xE0: 64-byte input, 352-byte
output, 6-byte feature).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from .hidraw import Transport

VENDOR_ID = 0x0CF2
REPORT_ID = 0xE0

Color = tuple[int, int, int]


def parse_color(value: str | Sequence[int]) -> Color:
    if isinstance(value, str):
        s = value.lstrip("#")
        if len(s) != 6:
            raise ValueError(f"bad colour {value!r}")
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    r, g, b = value
    return (int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF)


def _speed_byte(speed: int) -> int:
    """0 (slowest) .. 4 (fastest) -> ENE byte."""
    return {0: 2, 1: 1, 2: 0, 3: 0xFF, 4: 0xFE}.get(speed, 0)


def _brightness_byte(brightness: int, off_value: int) -> int:
    """0 (dimmest) .. 4 (brightest), -1 = off -> ENE byte."""
    if brightness < 0:
        return off_value
    return {0: 4, 1: 3, 2: 2, 3: 1, 4: 0}.get(brightness, 2)


def _direction_byte(direction: str) -> int:
    return 1 if direction in ("ccw", "left", "counterclockwise") else 0


def _encode_colors(colors: Sequence[Color]) -> bytes:
    # Firmware quirk: channel order is R, B, G.
    return bytes(b for (r, g, bl) in colors for b in (r, bl, g))


@dataclass(frozen=True)
class Firmware:
    customer_id: int
    project_id: int
    major_id: int
    minor_id: int
    fine_tune: int

    @property
    def version(self) -> str:
        hi, lo = self.fine_tune >> 4, self.fine_tune & 0x0F
        if lo > 9:
            return "v0.0"
        n = hi * 10 + lo
        return f"v{n // 10}.{n % 10}"

    @property
    def valid_alv2(self) -> bool:
        return (self.customer_id, self.project_id, self.major_id, self.minor_id) == (
            0xE0,
            0x50,
            0x80,
            0xC6,
        )


# --- UNI HUB AL V2 lighting tables -----------------------------------------
# Inner ring (8 LEDs/fan) and outer ring (12 LEDs/fan) take different mode
# bytes on the AL V2. A mode missing from one ring is skipped on that ring.

ALV2_INNER_MODES: dict[str, int] = {
    "Off": 0, "Static": 1, "Breathing": 2, "Rainbow Morph": 4, "Rainbow": 5,
    "Breathing Rainbow": 6, "Meteor Rainbow": 8, "Color Cycle": 24, "Meteor": 25,
    "Runway": 26, "Mop Up": 27, "Lottery": 29, "Wave": 30, "Spring": 31,
    "Tail Chasing": 32, "Warning": 33, "Voice": 34, "Mixing": 35, "Stack": 36,
    "Tide": 37, "Scan": 38, "Pac-Man": 39, "Colorful City": 40, "Render": 41,
    "Twinkle": 42,
}  # fmt: skip

ALV2_OUTER_MODES: dict[str, int] = {
    "Off": 0, "Static": 1, "Breathing": 2, "Static Colorful": 1,
    "Breathing Colorful": 2, "Rainbow Morph": 4, "Breathing Rainbow": 6,
    "Meteor": 25, "Runway": 26, "Rainbow": 43, "Color Cycle": 46, "Tai Chi": 47,
    "Warning": 48, "Voice": 49, "Mixing": 50, "Tide": 51, "Scan": 52,
    "Contest": 53, "Colorful City": 56, "Render": 57, "Twinkle": 58, "Wave": 59,
    "Spring": 60, "Tail Chasing": 61, "Mop Up": 62, "Tornado": 63,
    "Staggered": 64, "Spanning Teacups": 65, "Electric Current": 66, "Stack": 67,
}  # fmt: skip

PER_FAN_MODES = {"Static", "Breathing"}
CORNER_MODES = {"Static Colorful", "Breathing Colorful"}


@dataclass
class LightingEffect:
    mode: str = "Rainbow"
    colors: tuple[Color, ...] = ((255, 0, 0),)
    speed: int = 2  # 0..4
    brightness: int = 4  # 0..4, -1 = off
    direction: str = "cw"
    ring: str = "both"  # inner | outer | both

    @classmethod
    def from_dict(cls, d: dict) -> "LightingEffect":
        return cls(
            mode=str(d.get("mode", "Rainbow")),
            colors=tuple(parse_color(c) for c in d.get("colors", ["#ff0000"])) or ((255, 0, 0),),
            speed=int(d.get("speed", 2)),
            brightness=int(d.get("brightness", 4)),
            direction=str(d.get("direction", "cw")),
            ring=str(d.get("ring", "both")),
        )


class _Device:
    cmd_delay = 0.02

    def __init__(self, transport: Transport, sleep: Callable[[float], None] = time.sleep):
        self.t = transport
        self._sleep = sleep
        self.lock = threading.RLock()

    def _feature(self, *payload: int) -> None:
        with self.lock:
            self.t.send_feature(bytes([REPORT_ID, *payload]))
            self._sleep(self.cmd_delay)

    def _output(self, payload: bytes) -> None:
        with self.lock:
            self.t.write(bytes([REPORT_ID]) + payload)
            self._sleep(self.cmd_delay)

    def _input(self, length: int) -> bytes:
        with self.lock:
            data = self.t.get_input(REPORT_ID, 64)
        if len(data) < length:
            raise IOError(f"short input report: {len(data)} < {length}")
        return data[:length]

    def close(self) -> None:
        self.t.close()


class UniHubALV2(_Device):
    PID = 0xA104
    NAME = "UNI HUB AL V2"
    GROUPS = 4
    MAX_FANS = 6
    INNER_LEDS = 8
    OUTER_LEDS = 12
    PALETTE = 6

    def __init__(self, transport: Transport, sleep: Callable[[float], None] = time.sleep):
        super().__init__(transport, sleep)
        self.fan_counts = [3] * self.GROUPS

    # --- info ---------------------------------------------------------------
    def read_firmware(self) -> Firmware:
        with self.lock:
            self._feature(0x50, 0x01)
            d = self._input(5)
        return Firmware(*d[:5])

    def read_rpms(self) -> list[int]:
        with self.lock:
            self._feature(0x50, 0x00)
            d = self._input(9)
        # V2 hubs: 1 leading byte, then four big-endian u16 RPMs.
        return [int.from_bytes(d[1 + 2 * i : 3 + 2 * i], "big") for i in range(4)]

    # --- fans ---------------------------------------------------------------
    def _check_group(self, group: int) -> None:
        if not 0 <= group < self.GROUPS:
            raise ValueError(f"group {group} out of range 0..{self.GROUPS - 1}")

    def set_fan_count(self, group: int, count: int) -> None:
        """Tell the hub how many fans are daisy-chained on a port (0..6)."""
        self._check_group(group)
        if not 0 <= count <= self.MAX_FANS:
            raise ValueError(f"fan count {count} out of range 0..{self.MAX_FANS}")
        self._feature(0x10, 0x60, group + 1, count, 0x00)
        self.fan_counts[group] = count

    def set_mb_pwm_sync(self, group: int, enabled: bool) -> None:
        """Hand a port's speed to the motherboard PWM header (or take it back)."""
        self._check_group(group)
        self._feature(0x10, 0x62, (1 << (group + 4)) | (int(enabled) << group), 0x00, 0x00)

    def set_speed(self, group: int, percent: int) -> None:
        self._check_group(group)
        pct = max(0, min(100, int(round(percent))))
        self._feature(0x20 | group, 0x00, pct)

    # --- lighting -----------------------------------------------------------
    def set_mb_rgb_sync(self, enabled: bool) -> None:
        self._feature(0x10, 0x61, int(enabled), 0x00, 0x00)

    def _ring_colors(self, mode: str, colors: Sequence[Color], leds: int) -> list[Color]:
        fans = self.MAX_FANS
        if mode in CORNER_MODES:
            # 12 outer LEDs = 4 corners x 3 LEDs, one colour per corner.
            corner = [colors[i % len(colors)] for i in range(4)]
            return [corner[c] for _ in range(fans) for c in range(4) for _ in range(3)]
        if mode in PER_FAN_MODES:
            # One colour per fan; fewer colours than fans repeat cyclically.
            return [colors[f % len(colors)] for f in range(fans) for _ in range(leds)]
        if mode == "Meteor":
            pal = [colors[i % len(colors)] for i in range(self.PALETTE)]
        else:
            pal = [colors[i] if i < len(colors) else (0, 0, 0) for i in range(self.PALETTE)]
        return pal * fans

    def set_lighting(self, group: int, effect: LightingEffect) -> None:
        self._check_group(group)
        if effect.mode not in ALV2_INNER_MODES and effect.mode not in ALV2_OUTER_MODES:
            raise ValueError(f"unknown lighting mode {effect.mode!r}")
        speed = _speed_byte(effect.speed)
        bright = _brightness_byte(effect.brightness, off_value=8)
        direction = _direction_byte(effect.direction)
        rings = []
        if effect.ring in ("inner", "both") and effect.mode not in CORNER_MODES:
            rings.append((group * 2, ALV2_INNER_MODES.get(effect.mode), self.INNER_LEDS))
        if effect.ring in ("outer", "both"):
            rings.append((group * 2 + 1, ALV2_OUTER_MODES.get(effect.mode), self.OUTER_LEDS))
        with self.lock:
            for port, mode_byte, leds in rings:
                if mode_byte is None:
                    continue
                colors = self._ring_colors(effect.mode, effect.colors, leds)
                self._output(bytes([0x30 | port]) + _encode_colors(colors))
                self._feature(0x10 | port, mode_byte, speed, direction, bright)
            # The hub re-latches LED counts, then commits the frame.
            self._feature(0x10, 0x60, group + 1, self.fan_counts[group], 0x00)
            self._feature(0x60, 0x00, 0x01)

    def set_leds(
        self, group: int, inner: Sequence[Color] | None, outer: Sequence[Color] | None, brightness: int = 4
    ) -> None:
        """Paint every LED of a port explicitly (static mode + per-LED buffer).

        ``inner`` is 6 fans x 8 LEDs, ``outer`` 6 fans x 12 LEDs, fan-major.
        A ring passed as None is left untouched."""
        self._check_group(group)
        bright = _brightness_byte(brightness, off_value=8)
        with self.lock:
            for port, leds, n in ((group * 2, inner, self.INNER_LEDS), (group * 2 + 1, outer, self.OUTER_LEDS)):
                if leds is None:
                    continue
                if len(leds) != self.MAX_FANS * n:
                    raise ValueError(f"expected {self.MAX_FANS * n} colours, got {len(leds)}")
                self._output(bytes([0x30 | port]) + _encode_colors(leds))
                self._feature(0x10 | port, ALV2_INNER_MODES["Static"], 0, 0, bright)
            self._feature(0x10, 0x60, group + 1, self.fan_counts[group], 0x00)
            self._feature(0x60, 0x00, 0x01)

    @staticmethod
    def lighting_modes() -> list[dict]:
        names = list(dict.fromkeys([*ALV2_INNER_MODES, *ALV2_OUTER_MODES]))
        return [
            {
                "name": n,
                "inner": n in ALV2_INNER_MODES,
                "outer": n in ALV2_OUTER_MODES,
                "max_colors": 4 if n in CORNER_MODES else (UniHubALV2.MAX_FANS if n in PER_FAN_MODES else (0 if "Rainbow" in n or n == "Off" else UniHubALV2.PALETTE)),
            }
            for n in names
        ]


STRIMER_MODES: dict[str, int] = {
    "Off": 0, "Static": 1, "Breathing": 2, "Rainbow Morph": 4, "Rainbow": 5,
    "Wave": 24, "Snooker": 25, "Mixing": 26, "Ping Pong": 27, "Runway": 28,
    "Paint": 29, "Tide": 30, "Blow Up": 31, "Meteor": 32, "Shock Wave": 33,
    "Ripple": 34, "Voice": 35, "Bullet Stack": 36, "Drizzling": 37,
    "Fade Out": 38, "Color Transfer": 39, "Cross Over": 40, "Twinkle": 41,
    "Contest": 42, "Parallel": 43,
}  # fmt: skip

# Modes that drive a whole channel as one strip from its base port.
_STRIMER_CHANNEL_MODES = set(range(33, 44))
_RAINBOW_PALETTE: tuple[Color, ...] = (
    (255, 0, 0), (255, 105, 0), (255, 215, 0), (0, 255, 0), (0, 0, 255), (170, 0, 255),
)  # fmt: skip


class StrimerPlus(_Device):
    PID = 0xA200
    NAME = "Strimer Plus"
    cmd_delay = 0.04
    PORTS_PER_CHANNEL = 6
    LEDS_PER_PORT = 27

    def __init__(self, transport: Transport, sleep: Callable[[float], None] = time.sleep):
        super().__init__(transport, sleep)
        self.channel2_ports = 6
        self.port_modes = [1] * 12

    def read_firmware(self) -> str:
        with self.lock:
            self._feature(0x50, 0x00)
            d = self._input(5)
        if tuple(d[:4]) != (0xE0, 0x52, 0xFF, 0x40):
            return "unknown"
        hi, lo = d[4] >> 4, d[4] & 0x0F
        if lo > 9:
            return "v0.0"
        n = 10 * (hi + 1) + (lo - 3)
        return f"v{n // 10}.{n % 10}"

    def read_channel2_ports(self) -> int:
        with self.lock:
            self._feature(0x50, 0x01)
            count = self._input(1)[0]
        if count in (4, 6):
            self.channel2_ports = count
        return self.channel2_ports

    @property
    def port_count(self) -> int:
        return self.PORTS_PER_CHANNEL + self.channel2_ports

    def _enable_bitmap(self) -> int:
        bitmap = 0
        for ch, count in ((0, self.PORTS_PER_CHANNEL), (1, self.channel2_ports)):
            base = ch * self.PORTS_PER_CHANNEL
            if self.port_modes[base] in _STRIMER_CHANNEL_MODES:
                bitmap |= 1 << base
            else:
                for p in range(count):
                    bitmap |= 1 << (base + p)
        return bitmap

    def set_mb_rgb_sync(self, enabled: bool) -> None:
        with self.lock:
            self._feature(0x10, 0x40, int(enabled), 0x00, 0x00)
            self._feature(0x2C, 0x00, 0x00)

    def set_lighting(self, effect: LightingEffect, ports: Sequence[int] | None = None) -> None:
        if effect.mode not in STRIMER_MODES:
            raise ValueError(f"unknown Strimer mode {effect.mode!r}")
        mode = STRIMER_MODES[effect.mode]
        if effect.mode in ("Rainbow", "Rainbow Morph", "Bullet Stack", "Twinkle"):
            colors: Sequence[Color] = _RAINBOW_PALETTE
        elif effect.mode in PER_FAN_MODES:
            colors = [effect.colors[0]] * self.LEDS_PER_PORT
        else:
            colors = effect.colors
        speed = _speed_byte(effect.speed)
        bright = _brightness_byte(effect.brightness, off_value=0xFF)
        direction = _direction_byte(effect.direction)
        targets = list(range(self.port_count)) if ports is None else list(ports)
        with self.lock:
            for port in targets:
                self.port_modes[port] = mode
                self._feature(0x10 | (port & 0x0F), mode, speed, direction, bright)
                if colors:
                    self._output(bytes([0x30 | (port & 0x0F)]) + _encode_colors(colors))
            bitmap = self._enable_bitmap()
            self._feature(0x2C, bitmap >> 8, bitmap & 0xFF)

    def set_leds(self, frames: dict[int, Sequence[Color]], brightness: int = 4) -> None:
        """Paint ports LED-by-LED: ``frames`` maps port -> 27 colours."""
        bright = _brightness_byte(brightness, off_value=0xFF)
        with self.lock:
            for port, leds in frames.items():
                if not 0 <= port < self.port_count:
                    raise ValueError(f"port {port} out of range")
                if len(leds) != self.LEDS_PER_PORT:
                    raise ValueError(f"expected {self.LEDS_PER_PORT} colours, got {len(leds)}")
                self.port_modes[port] = STRIMER_MODES["Static"]
                self._feature(0x10 | port, STRIMER_MODES["Static"], 0, 0, bright)
                self._output(bytes([0x30 | port]) + _encode_colors(leds))
            bitmap = self._enable_bitmap()
            self._feature(0x2C, bitmap >> 8, bitmap & 0xFF)

    @staticmethod
    def lighting_modes() -> list[dict]:
        return [
            {"name": n, "max_colors": 1 if n in PER_FAN_MODES else (0 if n in ("Off", "Rainbow", "Rainbow Morph", "Bullet Stack", "Twinkle") else 6)}
            for n in STRIMER_MODES
        ]


DRIVERS = {UniHubALV2.PID: UniHubALV2, StrimerPlus.PID: StrimerPlus}
