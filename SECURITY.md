# Security

## Reporting a vulnerability

Report it privately, through GitHub:
[Security → Report a vulnerability](https://github.com/ronaldorcampos/louver/security/advisories/new).
Don't open a public issue for it.

Say what an attacker can do, from where (another local account, a container, a
web page, a crafted config file, a USB device), and how to reproduce it. You'll
get an answer within a week. Once a fix is released, the advisory is published,
with credit if you want it.

## Supported versions

Only the latest release gets security fixes.

## What Louver exposes

Useful for judging what counts as a vulnerability:

- **No network port.** The daemon serves its API and UI on a Unix socket,
  `$XDG_RUNTIME_DIR/louver/api.sock`, in a directory only your account can open.
  Another account, a container or a web page reaching that API is a
  vulnerability.
- **Device access through udev.** The udev rule gives the logged-in user
  (`uaccess`) and the `plugdev` group read/write on the supported Lian Li and
  ASUS Aura hidraw nodes, and lets `plugdev` write the `pwmN` and `pwmN_enable`
  files of supported Super I/O chips. Access to other devices, or to these
  beyond what the README describes, is a vulnerability.
- **The machine-wide lock.** With the Debian package, `/run/louver` is only
  open to `plugdev`. A way for another account to hold or remove it is a
  vulnerability.
- **The Debian package's maintainer scripts** run as root on install, upgrade
  and removal.

The service runs as your user, not as root.

## Not vulnerabilities

- Anything that needs your own account already: it can set your fan speeds
  anyway. Fan settings that let hardware overheat are covered by the warning in
  the README; report a Louver bug that runs fans slower than configured as a
  normal bug, marked as a safety issue.
- Bugs in the hardware's firmware.
