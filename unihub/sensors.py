"""Temperature sources: Linux hwmon plus nvidia-smi.

hwmonN indices are not stable across boots, so sensors are keyed by
``hwmon:<chip name>:<label>``. Two aliases resolve to the best match:
``cpu`` (coretemp package / k10temp Tctl) and ``gpu`` (NVIDIA / amdgpu).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

HWMON = Path("/sys/class/hwmon")

_CPU_PREFS = [
    ("coretemp", "Package id 0"),
    ("k10temp", "Tctl"),
    ("k10temp", "Tdie"),
    ("zenpower", "Tdie"),
    ("cpu_thermal", None),
]
_AMD_GPU_PREFS = [("amdgpu", "junction"), ("amdgpu", "edge")]

# Values outside this range are unconnected-probe noise (e.g. nct6798 AUXTIN = 127 C).
_PLAUSIBLE = (-20.0, 120.0)


@dataclass(frozen=True)
class Sensor:
    id: str
    label: str
    path: str | None = None  # hwmon temp*_input file


class SensorHub:
    def __init__(self, hwmon_root: Path = HWMON, nvidia_smi: str | None = "auto"):
        self.root = hwmon_root
        self._nvidia = shutil.which("nvidia-smi") if nvidia_smi == "auto" else nvidia_smi
        self._nv_cache: tuple[float, list[float]] = (0.0, [])
        self._nv_lock = threading.Lock()
        self._sensors: dict[str, Sensor] = {}
        self.rescan()

    def rescan(self) -> None:
        found: dict[str, Sensor] = {}
        if self.root.exists():
            for chip in sorted(self.root.iterdir()):
                try:
                    name = (chip / "name").read_text().strip()
                except OSError:
                    continue
                for inp in sorted(chip.glob("temp*_input")):
                    stem = inp.name[: -len("_input")]
                    try:
                        label = (chip / f"{stem}_label").read_text().strip()
                    except OSError:
                        label = stem
                    sid = f"hwmon:{name}:{label}"
                    # Duplicate chips (e.g. 3x nvme) get a suffix.
                    n, base = 2, sid
                    while sid in found:
                        sid = f"{base}#{n}"
                        n += 1
                    found[sid] = Sensor(sid, f"{name} {label}", str(inp))
        for i, _ in enumerate(self._nvidia_temps(force=True)):
            sid = f"nvidia:{i}"
            found[sid] = Sensor(sid, f"NVIDIA GPU {i}")
        self._sensors = found

    def _nvidia_temps(self, force: bool = False) -> list[float]:
        if not self._nvidia:
            return []
        with self._nv_lock:
            ts, vals = self._nv_cache
            if not force and time.monotonic() - ts < 2.0:
                return vals
            try:
                out = subprocess.run(
                    [self._nvidia, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=3, check=True,
                ).stdout
                vals = [float(x) for x in out.split()]
            except (OSError, subprocess.SubprocessError, ValueError):
                vals = []
            self._nv_cache = (time.monotonic(), vals)
            return vals

    def resolve(self, sid: str) -> str | None:
        if sid == "cpu":
            for chip, label in _CPU_PREFS:
                for s in self._sensors.values():
                    _, name, lab = s.id.split(":", 2)
                    if name == chip and (label is None or lab == label):
                        return s.id
            return None
        if sid == "gpu":
            if "nvidia:0" in self._sensors:
                return "nvidia:0"
            for chip, label in _AMD_GPU_PREFS:
                key = f"hwmon:{chip}:{label}"
                if key in self._sensors:
                    return key
            return None
        return sid if sid in self._sensors else None

    def read(self, sid: str) -> float | None:
        """Read one sensor (or alias, or ``max:a,b`` composite) in deg C."""
        if sid.startswith("max:"):
            vals = [v for v in (self.read(p) for p in sid[4:].split(",")) if v is not None]
            return max(vals) if vals else None
        real = self.resolve(sid)
        if real is None:
            return None
        if real.startswith("nvidia:"):
            vals = self._nvidia_temps()
            idx = int(real.split(":")[1])
            return vals[idx] if idx < len(vals) else None
        s = self._sensors[real]
        try:
            v = int(Path(s.path).read_text()) / 1000.0  # type: ignore[arg-type]
        except (OSError, ValueError):
            return None
        return v if _PLAUSIBLE[0] <= v <= _PLAUSIBLE[1] else None

    def list(self) -> list[dict]:
        out = []
        for alias, label in (("cpu", "CPU"), ("gpu", "GPU"), ("max:cpu,gpu", "Max of CPU / GPU")):
            if alias.startswith("max:") or self.resolve(alias):
                out.append({"id": alias, "label": label, "value": self.read(alias)})
        for s in self._sensors.values():
            v = self.read(s.id)
            if v is not None:
                out.append({"id": s.id, "label": s.label, "value": v})
        return out
