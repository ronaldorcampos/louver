"""Motherboard fan headers via Linux hwmon (nct67xx / it87 Super I/O chips).

Each header is ``pwmN`` (0-255) with ``pwmN_enable`` (1 = manual; 2+ = the
chip's own automatic curve, which the BIOS programs). We switch a header to
manual only while UniHub drives it, and always restore the original enable
value afterwards, so the BIOS curve takes back over.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

HWMON = Path("/sys/class/hwmon")
SUPPORTED_CHIPS = ("nct6775", "nct6776", "nct6779", "nct6791", "nct6792", "nct6793",
                   "nct6795", "nct6796", "nct6797", "nct6798", "nct6799", "it87", "it8686", "it8688")
MANUAL = 1


@dataclass
class Header:
    index: int
    path: Path
    original_enable: int | None = None  # set while we hold manual control

    def _read(self, name: str) -> int | None:
        try:
            return int((self.path / name).read_text().strip())
        except (OSError, ValueError):
            return None

    @property
    def rpm(self) -> int | None:
        return self._read(f"fan{self.index}_input")

    @property
    def pwm(self) -> int | None:
        return self._read(f"pwm{self.index}")

    @property
    def enable(self) -> int | None:
        return self._read(f"pwm{self.index}_enable")


class MotherboardFans:
    def __init__(self, root: Path = HWMON):
        self.chip = ""
        self.dir: Path | None = None
        self.headers: dict[int, Header] = {}
        for d in sorted(root.iterdir()) if root.exists() else []:
            try:
                name = (d / "name").read_text().strip()
            except OSError:
                continue
            if name.startswith(SUPPORTED_CHIPS):
                self.chip, self.dir = name, d
                for p in sorted(d.glob("pwm[0-9]")):
                    i = int(p.name[3:])
                    if (d / f"fan{i}_input").exists():
                        self.headers[i] = Header(i, d)
                break

    @property
    def present(self) -> bool:
        return self.dir is not None

    def writable(self) -> bool:
        h = next(iter(self.headers.values()), None)
        if h is None:
            return False
        import os

        return os.access(h.path / f"pwm{h.index}", os.W_OK) and os.access(h.path / f"pwm{h.index}_enable", os.W_OK)

    def _write(self, h: Header, name: str, value: int) -> None:
        (h.path / name).write_text(f"{value}\n")

    def set_percent(self, index: int, percent: float) -> None:
        h = self.headers[index]
        if h.original_enable is None:
            cur = h.enable
            # Remember the BIOS's mode once; never "restore" to manual. If a
            # crash left it manual, fall back to the chip's automatic mode
            # (Nuvoton SmartFan IV = 5, ITE automatic = 2).
            h.original_enable = cur if cur not in (None, MANUAL) else (2 if self.chip.startswith("it") else 5)
            self._write(h, f"pwm{index}_enable", MANUAL)
        self._write(h, f"pwm{index}", max(0, min(255, round(percent * 2.55))))

    def release(self, index: int) -> None:
        """Hand a header back to the chip's automatic (BIOS) curve."""
        h = self.headers[index]
        if h.original_enable is not None:
            self._write(h, f"pwm{index}_enable", h.original_enable)
            h.original_enable = None

    def release_all(self) -> None:
        for i in list(self.headers):
            try:
                self.release(i)
            except OSError:
                pass

    def describe(self) -> list[dict]:
        return [
            {"index": h.index, "rpm": h.rpm, "pwm": h.pwm, "enable": h.enable, "held": h.original_enable is not None}
            for h in self.headers.values()
        ]
