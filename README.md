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

**RAM RGB** (Corsair DDR5 sticks on an Intel SMBus, opt-in, Debian package only)
- Corsair Vengeance RGB, Vengeance RGB RS, Vengeance Shugo, Dominator Platinum
  and Dominator Titanium DDR5 sticks, as a **RAM RGB** zone: Off, one colour,
  or a gradient along each stick's LEDs. The same look on every stick; themes
  set it too.
- Intel boards only: the sticks are reached over the chipset's SMBus
  (`i2c-i801`), found by its name, never by bus number. DDR4 sticks and AMD
  boards aren't supported: the helper scans only when the firmware's memory
  table (SMBIOS type 17, `/sys/firmware/dmi/entries`) lists DDR5 and nothing
  else in every occupied slot.
- Louver sends colours only (OpenRGB's "direct" mode). It never writes an
  effect or anything else the stick stores, so after a restart the sticks
  show their own effect until Louver sets them again. When Louver stops, or
  RAM RGB is turned off, they keep their last colours (or fall back to their
  own effect, depending on the stick).
- Off until you turn on **Settings → Control RAM RGB**; until then the Louver
  daemon never contacts the helper below. The setting is Louver's, not the
  helper's: `louver ram-probe`, or any other process in the `plugdev` group
  that connects to the helper's socket, starts it, and it scans the bus
  whatever the setting says.
- How: Louver runs as your user and gets no access to `/dev/i2c-*`. The
  package adds a small root helper, `louver-ram.service`, started on demand by
  `louver-ram.socket` (`/run/louver-ram.sock`, reachable by root and the
  `plugdev` group). The helper alone opens the SMBus, in a tight systemd
  sandbox (no capabilities, no network, `/dev/i2c-*` and nothing else). It
  refuses every register, address and length the driver doesn't use, and
  sends colours only to a stick that identified itself as a Corsair DDR5
  controller it knows (its LED count comes from that table: an unknown stick
  is listed, not driven), at most one frame a second per stick after a burst
  of four. A scan does write before that, as OpenRGB's does: two `0x00` bytes
  (registers `0x61` and `0x21`, selecting the device-info buffer) go to every
  address whose registers `0x43` and `0x44` read like a Corsair controller,
  before its device info has been read and checked. The helper drops a client
  that sends nothing for a minute (a connected Louver re-sends its frame
  every 10 s, on the first poll after that), and exits a minute after the
  last client disconnects.
  Unlike OpenRGB, there's no udev rule that opens the SMBus to users.
- `louver ram-probe` asks the helper what it found: the bus, each stick
  (address, model, LED count, firmware), and why any other address was skipped.
- The per-user `./install.sh` doesn't install the helper, so RAM RGB needs the
  Debian package. If `/dev/i2c-N` is missing, load the module:
  `sudo modprobe i2c-dev`, and to load it at every boot,
  `echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf`.

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
restarts the service on the new version for logged-in users. `sudo apt remove louver`
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
fan control: that's the `louver` service. The UI is
served on `http://127.0.0.1:47321/` and only accepts connections from this machine.

Only one copy runs at a time. Opening the app again brings the existing window
forward. A second `louver daemon` (or `probe` / `mb-test` while the service is up)
exits with a message naming the running copy, so two processes never fight over the hub.

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
louver ram-probe        Corsair DDR5 RAM RGB: what the louver-ram helper found on the SMBus
louver status           live state from the running daemon
louver set 2 60         port 2 to a fixed 60%
louver daemon --demo    simulated hub, Strimer and motherboard, for trying the UI without hardware
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

Corsair DDR5 RGB controllers sit at SMBus addresses `0x18`-`0x1F`. A
controller answers `0x1A`-`0x1C` at register `0x43` and `0x01`/`0x03`/`0x04`
at `0x44`; its 32-byte device-info buffer (vendor `0x1B1C`, product ID,
firmware, protocol) is selected with `0x61` and `0x21` and read from `0x40`,
with a CRC-8 at `0x42`. A direct frame (protocol 4 and later) is the LED
count, R, G, B per LED and a CRC-8, block-written to `0x31` (and `0x32` past
32 bytes). `louver/corsair_dram.py` is a port to Python of the Corsair DRAM
driver in OpenRGB (GPL-2.0-or-later), by Adam Honse and Erik Gilling; NOTICE
names the files and the commit. It leaves out the hardware effects and the
other colour path, which store their settings on the stick (register `0x82`).

Credits and licences are in [NOTICE](NOTICE).

Not a Lian Li product; not affiliated with or endorsed by Lian Li. Lian Li, UNI HUB,
Strimer and L-Connect are trademarks of Lian Li Industrial Co., Ltd. Not affiliated
with or endorsed by ASUS; ASUS, Aura and ROG are trademarks of ASUSTeK Computer Inc.

## Licence

GNU GPL, version 2 or (at your option) any later version; see [LICENSE](LICENSE).
Louver's code before the motherboard RGB port (commit d5b59b1 and earlier) was
MIT-licensed; from this release on, Louver is GPL-2.0-or-later.

## Development

```bash
python3 -m pytest
pre-commit install   # once per clone: ruff check + ruff format on every commit
packaging/build-deb.sh   # dist/louver_<version>_all.deb
```

To release: bump `version` in `pyproject.toml`, then push a matching tag
(`git tag v0.2.0 && git push origin v0.2.0`). CI attaches the .deb to a GitHub Release.
