# Louver

An L-Connect 3–style fan and RGB app for Linux, for the Lian Li **UNI HUB AL V2**
(`0cf2:a104`) and the **Strimer Plus** (`0cf2:a200`). It runs as a small
daemon with a local web UI. It needs nothing beyond the Python standard
library and talks to the controllers directly through `/dev/hidraw*`.

## Features

**Fans (per port, 4 ports)**
- Live RPM, duty and temperature for each port.
- Modes: Quiet / Standard / High / Full presets, Manual (a fixed %), a custom
  curve (draggable points), or **MB sync** (the motherboard header's PWM drives the port).
- Temperature sources: CPU (coretemp/k10temp), GPU (`nvidia-smi` or amdgpu),
  any hwmon sensor, or "max of CPU / GPU".
- Smoothing: speed rises quickly and falls slowly, so short load spikes don't
  make the fans surge.
- **Failsafe:** if a port's sensor can't be read, that port runs at 100% until it recovers.
- On exit: hand the fans back to the motherboard (default), set a fixed
  speed, or keep the last speed.

**Motherboard fan headers** (Nuvoton nct67xx / ITE it87 boards)
- Each header is opt-in and starts on **BIOS control**; pick a mode to let Louver drive it with the same presets and curves.
- Whenever Louver stops, every header it drove goes back to the BIOS's own curve.
- `louver mb-list` shows the headers; `louver mb-test N PCT` runs one at a set speed for 10 s and then hands it back.
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
sudo install -m 644 packaging/99-louver.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw
systemctl --user enable --now louver
```

Then open **Louver** from the app menu, or run `louver app`. The desktop app is
a window plus a tray icon (GTK3, WebKit2GTK 4.1 and AyatanaAppIndicator via
`python3-gi`; on GNOME the AppIndicator extension must be enabled, as it is on
Ubuntu). **Settings → Application** has start on login, start minimized,
and minimize to tray on close. The tray icon only appears while the window
is closed. Quitting the app never stops
fan control: that's the `louver` service. The UI is
served on `http://127.0.0.1:47321/` and only accepts connections from this machine.

Only one copy runs at a time. Opening the app again brings the existing window
forward. A second `louver daemon` (or `probe` / `mb-test` while the service is up)
exits with a message naming the running copy, so two processes never fight over the hub.

To keep fan control running when nobody is logged in: `loginctl enable-linger $USER`.

### Upgrading from UniHub

Louver used to be called UniHub. Running `./install.sh` swaps the old service,
launcher, menu entry and tray app for the new ones, and starts Louver if UniHub
was enabled. On first run Louver copies `~/.config/unihub/config.json` to
`~/.config/louver/`, and replaces the old start-on-login entry. The old config
is left in place as a backup and can be deleted once you're happy. The udev rule
keeps working under its old name; install.sh prints the commands to rename it.

### If you used uni-sync before

`uni-sync.service` runs once at boot and puts the hub ports into motherboard
PWM mode. Louver takes the ports back when it starts, so the two don't fight.
It is still cleaner to disable the old service:

```bash
sudo systemctl disable uni-sync.service
```

## CLI

```
louver probe            read-only hardware check: firmware and RPM (no daemon needed)
louver status           live state from the running daemon
louver set 2 60         port 2 to a fixed 60%
louver daemon --demo    simulated hub and Strimer, for trying the UI without hardware
```

## Config

`~/.config/louver/config.json`. The UI writes it, and it can also be edited by hand.
Restart the daemon after editing it by hand.

## Protocol notes

Both controllers expose report ID `0xE0`. Commands are HID feature reports,
colour buffers are output reports, and replies come back as input reports
via `HIDIOCGINPUT`. The byte layouts follow the community reverse-engineering
in [sgtaziz/lian-li-linux](https://github.com/sgtaziz/lian-li-linux) (MIT) and
[EightB1ts/uni-sync](https://github.com/EightB1ts/uni-sync). See `louver/devices.py`.

Not a Lian Li product; not affiliated with Lian Li.

## Development

```bash
python3 -m pytest
```
