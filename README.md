# Louver

A fan and RGB app for Linux, for the Lian Li **UNI HUB AL V2**
(`0cf2:a104`) and the **Strimer Plus** (`0cf2:a200`): an open-source
alternative to L-Connect 3, which only runs on Windows. It runs as a small
daemon with a local web UI. It needs nothing beyond the Python standard
library and talks to the controllers directly through `/dev/hidraw*`.

> [!WARNING]
> Louver sets your fan speeds directly. A curve that's too low, a fixed low
> speed, or a pump header taken off BIOS control can let your CPU, GPU or pump
> overheat. Watch your temperatures after any change. Louver comes with no
> warranty (see [LICENSE](LICENSE)); use it at your own risk.

## Compatible hardware

| Device | How Louver finds it | Tested on |
|---|---|---|
| Lian Li **UNI HUB AL V2** (fans and their RGB) | USB `0cf2:a104` | firmware v0.4 |
| Lian Li **Strimer Plus** (cable RGB) | USB `0cf2:a200` | firmware v1.5 |
| **ASUS Aura USB** motherboard RGB (ARGB headers, onboard LEDs) | USB `0b05:18f3`, `0b05:1939`, `0b05:19af`, `0b05:1aa6`, `0b05:1bed` | `0b05:19af`, ROG STRIX Z790-A GAMING WIFI (AULA3-AR32-0309) |
| **Motherboard fan headers** (Super I/O) | hwmon chips `nct6775`, `nct6776`, `nct6779`, `nct6791`-`nct6793`, `nct6795`-`nct6799`, `it87`, `it8686`, `it8688` | `nct6798`, ROG STRIX Z790-A GAMING WIFI |

The Aura product IDs are the ones OpenRGB's ASUS Aura USB driver lists; only
`19af` has run with Louver. To see whether a board has one:
`lsusb -d 0b05:`, then `louver aura-probe` (read-only).

Not supported:
- **Other Lian Li controllers** (other UNI HUB models, other Strimers).
- **Other boards' RGB:** MSI Mystic Light, Gigabyte RGB Fusion, ASRock
  Polychrome and the like use different controllers.
- **RAM RGB.** Corsair DDR5 support was tried and removed: Vengeance RGB DDR5
  sticks refuse the direct colour frames OpenRGB sends them.

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
- **Never below N%** sets a header's own minimum speed, which holds in every mode Louver drives it in, manual
  included (the failsafe's 100% still wins).
- Tick **Pump on this header** for a pump: Louver then offers only BIOS control or Full, and the
  tray and the config file can't set anything slower either.
- `louver mb-list` shows the headers; `louver mb-test N PCT` runs one at a set speed for 10 s and then hands it back.

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

**Motherboard RGB** (ASUS Aura USB boards, opt-in)
- The board's ARGB headers and onboard LEDs, as a **Motherboard RGB** zone: one
  look for every channel, or one per channel. Every Aura hardware effect, with
  a colour for the ones that take one. Themes set it too.
- Off until you turn on **Settings → Control motherboard RGB**; until then
  Louver never opens the board's Aura controller.
- Louver doesn't save effects to the board's flash: after a restart the board
  shows its own saved effect until Louver starts and sets it again. When Louver
  stops, the lights stay as they are.
- `louver aura-probe` shows the controller's firmware and channels (read-only).

## Install

### Ubuntu / Debian package

If you used `./install.sh` before, remove that per-user copy first, or it keeps
running instead of the package's:

```bash
systemctl --user disable --now louver
rm -rf ~/.local/share/louver ~/.local/bin/louver ~/.config/systemd/user/louver.service
rm -f ~/.local/share/applications/io.github.ronaldorcampos.Louver.desktop ~/.local/share/icons/hicolor/*/apps/io.github.ronaldorcampos.Louver*.svg
sudo rm -f /etc/udev/rules.d/99-louver.rules   # the package brings its own
systemctl --user daemon-reload
```

Then download `louver_<version>_all.deb` from the
[Releases](https://github.com/ronaldorcampos/louver/releases) page (or build it with
`packaging/build-deb.sh`, which writes it to `dist/`), and install it:

```bash
sudo apt install ./louver_*_all.deb
systemctl --user enable --now louver
```

The package brings the desktop app's dependencies and the udev rule. Upgrading
restarts the service on the new version for logged-in users; if the Louver app is open,
close its window, choose Quit from its tray icon and open it again, as the open window
still runs the old version. `sudo apt remove louver`
stops it for them and removes the code; your config in `~/.config/louver` stays. If
you won't reinstall, run `systemctl --user disable louver` before removing it: once
the package is gone, `systemctl --user disable` can no longer remove the
start-at-login link.

### Per-user install (any distro)

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
fan control: that's the `louver` service.

There's no network port. The service serves its API and the UI on a Unix socket,
`$XDG_RUNTIME_DIR/louver/api.sock`, in a directory only your account can open, so
other accounts, containers and web pages can't reach it. The app shows the UI from a
`louver://app/` address that it hands to that socket; `louver status` and `louver set`
use it too. There's no browser version: `louver ui` needs the desktop app's packages.

Only one copy runs at a time. Opening the app again brings the existing window
forward. A second `louver daemon` (or `probe` / `aura-probe` / `mb-test` while the
service is up) exits with a message naming the running copy, so two processes never
fight over the hub. With the Debian package that holds across accounts too, for
members of `plugdev`: the lock is in `/run/louver`, which only `plugdev` can open (the
group that can drive the hub and the fan headers anyway). The per-user install, or an
account outside `plugdev`, gets the per-account check only. That's also why the service
is enabled per user and never globally: a global enable would start it in the login
screen's session as well, whose account usually isn't in `plugdev`, so the two would
fight over the fans.

To keep fan control running when nobody is logged in: `loginctl enable-linger $USER`.

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
louver aura-probe       ASUS Aura motherboard RGB: firmware and channels (read-only)
louver status           live state from the running daemon
louver set 2 60         port 2 to a fixed 60%
louver daemon --demo --socket /tmp/demo/api.sock
                        simulated hub, Strimer and motherboard, for trying the UI without hardware;
                        its own --socket lets it run beside the service; louver app --socket (the
                        same path) shows it, when the app isn't already open. Give --socket a
                        directory only you can write to
```

## Config

`~/.config/louver/config.json`. The UI writes it, and it can also be edited by hand.
Restart the daemon after editing it by hand.

## Protocol notes

Both controllers expose report ID `0xE0`. Commands are HID feature reports,
colour buffers are output reports, and replies come back as input reports
via `HIDIOCGINPUT`. The drivers in `louver/devices.py` follow the Rust
implementation in [sgtaziz/lian-li-linux](https://github.com/sgtaziz/lian-li-linux) (MIT),
and its unverified effect numbers come from there; the protocol was also
cross-checked against [EightB1ts/uni-sync](https://github.com/EightB1ts/uni-sync) (MIT).

The ASUS Aura USB mainboard controller (`0b05:19af` and the other Aura
mainboard IDs) takes 65-byte output reports with report ID `0xEC` and answers
its two queries (firmware, config table) on the interrupt endpoint.
`louver/aura.py` is a port to Python of the Aura USB mainboard driver in
[OpenRGB](https://gitlab.com/CalcProgrammer1/OpenRGB) (GPL-2.0-or-later), by
Martin Hartl and Adam Honse; NOTICE names the files and the commit. It leaves
out OpenRGB's "save to device" command, which writes the controller's flash,
and its direct per-LED mode.
The config table says how many LEDs are on the board and how many ARGB
headers it has, but not how many LEDs are on each header, so Louver drives the
headers with hardware effects only.

Credits and licences are in [NOTICE](NOTICE).

Not a Lian Li product; not affiliated with or endorsed by Lian Li. Lian Li, UNI HUB,
Strimer and L-Connect are trademarks of Lian Li Industrial Co., Ltd. Not affiliated
with or endorsed by ASUS; ASUS, Aura and ROG are trademarks of ASUSTeK Computer Inc.

## Licence

GNU GPL, version 2 or (at your option) any later version; see [LICENSE](LICENSE).
Louver's code before the motherboard RGB port (commit d5b59b1 and earlier) was
MIT-licensed; from this release on, Louver is GPL-2.0-or-later.

## Development

How to report bugs and send changes: [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
python3 -m pytest
pre-commit install   # once per clone: ruff check + ruff format on every commit
packaging/build-deb.sh   # dist/louver_<version>_all.deb
```

To release: bump `version` in `pyproject.toml`, then push a matching tag
(`git tag v0.2.0 && git push origin v0.2.0`). CI attaches the .deb to a GitHub Release.
