# Contributing to Louver

Bug reports, hardware reports and pull requests are welcome. Louver drives fans
directly, so changes are held to one rule above the rest: a mistake must fail
safe, with fans running faster, never slower.

## Reporting a bug

Open an issue with:

- What you did, what you expected, and what happened.
- The Louver version (`dpkg -s louver | grep Version`, or the git commit) and
  your distro and kernel (`uname -r`).
- Your hardware: `lsusb -d 0cf2:` for Lian Li, `lsusb -d 0b05:` for ASUS Aura,
  and `louver mb-list` for motherboard fan headers.
- `louver status`, and the log: `journalctl --user -u louver -b`.

If a fan ran slower than it should, or a header wasn't handed back to the BIOS,
say so in the title. Those come first.

## Reporting hardware

Louver has only run on the devices in the README's table. If you have a
controller it lists but hasn't been tested on (another Aura product ID, another
Super I/O chip), a report that it works is useful too: the board or device,
`louver probe` or `louver aura-probe` output (with the service stopped:
`systemctl --user stop louver`), and what you tried.

Support for a new device needs its protocol and someone who can test on it.
Open an issue before writing a driver.

## Development

```bash
python3 -m pytest
pre-commit install   # once per clone: ruff check + ruff format on every commit
packaging/build-deb.sh   # dist/louver_<version>_all.deb
```

Louver needs Python 3.10 or later and nothing outside the standard library;
keep it that way. The tests need only `pytest`; the few that use `node` (the
UI model tests), `dpkg-deb` (the package) or `python3-gi` skip when it's
missing.

To try the UI without hardware, run a simulated hub, Strimer and motherboard on
a socket of its own, beside the service:

```bash
louver daemon --demo --socket /tmp/louver-demo/api.sock
louver app --socket /tmp/louver-demo/api.sock
```

Give `--socket` a directory only you can write to.

## Pull requests

1. Fork, branch from `main`, and keep a PR to one change.
2. Add a test for what you change: a specific input and the output it must
   give. The fakes in `tests/fakes.py` and the fake sysfs trees in the tests
   stand in for the hardware.
3. Hardware changes: say in the PR what you ran it on, and what you couldn't
   test.
4. Keep the safety behaviour: an unreadable sensor runs its fans at 100%,
   motherboard headers start on BIOS control and are handed back when Louver
   stops, and nothing writes a device's flash.

Commit messages start with the area, then say what changed and why:

```
Sensors: find a sensor that appears or moves after start

The daemon scanned hwmon once, at start. A sensor whose driver loaded
later stayed unreadable until a restart, and every fan on it ran at 100%.
```

CI runs lint, the tests on Python 3.10 and 3.14, and a package build; all four
must pass, and the branch must be up to date with `main`. History on `main` is
linear, so PRs merge by squash or rebase. Every PR needs the maintainer's
approval.

## Licence

Louver is GPL-2.0-or-later. By sending a pull request you agree that your
contribution is licensed under the same terms. Code taken from another project
must have a compatible licence and be credited in NOTICE.
