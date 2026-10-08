# UniHub

An L-Connect 3–style fan and RGB app for Linux, for the Lian Li **UNI HUB AL V2**
(`0cf2:a104`) and the **Strimer Plus** (`0cf2:a200`). It runs as a small
daemon with a local web UI. It needs nothing beyond the Python standard
library and talks to the controllers directly through `/dev/hidraw*`.

## Features

**Fans (per port, 4 ports)**
- Live RPM, duty and temperature for each port.
- Modes: Quiet / Standard / High Speed / Full Speed presets, Fixed %, a custom
  curve (draggable points), or **MB PWM Sync** (the motherboard header drives the port).
- Temperature sources: CPU (coretemp/k10temp), GPU (`nvidia-smi` or amdgpu),
  any hwmon sensor, or "max of CPU / GPU".
- Smoothing: speed rises quickly and falls slowly, so short load spikes don't
  make the fans surge.
- **Failsafe:** if a port's sensor can't be read, that port runs at 100% until it recovers.
- On exit: hand the fans back to the motherboard (default), set a fixed
  speed, or keep the last speed.

**Motherboard fan headers** (Nuvoton nct67xx / ITE it87 boards)
- Each header is opt-in and starts on **BIOS control**; pick a mode to let UniHub drive it with the same presets and curves.
- Whenever UniHub stops, every header it drove goes back to the BIOS's own curve.
- `unihub mb-list` shows the headers; `unihub mb-test N PCT` runs one at a set speed for 10 s and then hands it back.
- Keep pump headers on BIOS control.

**Lighting**
- **Themes:** Rainbow Wave, Ocean, Sunset, Cyberpunk, Arctic, Inferno,
  Forest, Vaporwave, Thermal, Stealth. Each one sets the hub and the Strimer
  in one click.
- **Hardware effects:** every AL V2 effect (inner and outer rings use separate
  effect tables) and every Strimer Plus effect, with speed, direction and brightness.
- **Per-fan colours** in Static and Breathing; **per-corner colours** for
  Static/Breathing Colorful on the outer ring.
- **Software effects**, drawn LED by LED:
  - **Custom:** paint each LED (8 inner + 12 outer per fan, 27 per Strimer port).
  - **Gradient:** colour stops spread across the fans of one port, or across all ports.
  - **Temperature:** colour follows a sensor, from blue through green and yellow to red.
- Lighting per port or for all ports at once; per-port Strimer control; motherboard ARGB sync toggles.

## Install

```bash
./install.sh
sudo install -m 644 packaging/99-unihub.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw
systemctl --user enable --now unihub
```

Then open **UniHub** from the app menu, or run `unihub ui`. The UI is
served on `http://127.0.0.1:47321/` and only accepts connections from this machine.

To keep fan control running when nobody is logged in: `loginctl enable-linger $USER`.

### If you used uni-sync before

`uni-sync.service` runs once at boot and puts the hub ports into motherboard
PWM mode. UniHub takes the ports back when it starts, so the two don't fight.
It is still cleaner to disable the old service:

```bash
sudo systemctl disable uni-sync.service
```

## CLI

```
unihub probe            read-only hardware check: firmware and RPM (no daemon needed)
unihub status           live state from the running daemon
unihub set 2 60         port 2 to a fixed 60%
unihub daemon --demo    simulated hub and Strimer, for trying the UI without hardware
```

## Config

`~/.config/unihub/config.json`. The UI writes it, and it can also be edited by hand.
Restart the daemon after editing it by hand.

## Protocol notes

Both controllers expose report ID `0xE0`. Commands are HID feature reports,
colour buffers are output reports, and replies come back as input reports
via `HIDIOCGINPUT`. The byte layouts follow the community reverse-engineering
in [sgtaziz/lian-li-linux](https://github.com/sgtaziz/lian-li-linux) (MIT) and
[EightB1ts/uni-sync](https://github.com/EightB1ts/uni-sync). See `unihub/devices.py`.

Not a Lian Li product; not affiliated with Lian Li.

## Development

```bash
python3 -m pytest
```
