#!/usr/bin/env bash
# Builds louver_<version>_all.deb into $1 (default: dist/). The package is a
# system-wide install of what install.sh copies per user. Needs a git
# checkout (only git-tracked files go in, as they are in the working tree)
# and dpkg-deb. The same commit always builds the same bytes.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
if ! git -C "$here" rev-parse --git-dir >/dev/null 2>&1; then
  echo "build-deb: $here is not a git checkout (only tracked files are packaged)" >&2
  exit 1
fi
for f in LICENSE NOTICE; do
  if ! git -C "$here" ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    echo "build-deb: $f must exist and be tracked (it goes into the package's copyright)" >&2
    exit 1
  fi
done
out="$(mkdir -p "${1:-$here/dist}" && cd "${1:-$here/dist}" && pwd)"
app_id="io.github.ronaldorcampos.Louver"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$here" log -1 --format=%ct)}"

# pyproject's PEP 440 version, as dpkg sorts it: 0.2.0rc1 -> 0.2.0~rc1, so
# the final 0.2.0 is an upgrade from it.
pep440="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$here/pyproject.toml")"
if ! printf '%s\n' "$pep440" | grep -Eq '^[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?$'; then
  echo "build-deb: can't package version '$pep440' (want 1.2.3, or 1.2.3a1/b1/rc1)" >&2
  exit 1
fi
version="$(printf '%s\n' "$pep440" | sed -E 's/(a|b|rc)([0-9]+)$/~\1\2/')"

root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
py="$root/usr/lib/python3/dist-packages"
mkdir -p "$py" "$root/usr/bin" "$root/usr/lib/systemd/user" "$root/usr/lib/udev/rules.d" \
  "$root/usr/lib/tmpfiles.d" \
  "$root/usr/share/applications" "$root/usr/share/icons" "$root/usr/share/doc/louver" "$root/DEBIAN"

tracked() {  # tracked DIR DEST PATH...: copy git-tracked DIR/PATH files to DEST, paths relative to DIR
  local dir="$1" dest="$2"
  shift 2
  (cd "$here/$dir" && git ls-files -z -- "$@" | xargs -0 --no-run-if-empty cp --parents -t "$dest")
}
tracked . "$py" louver
tracked packaging/icons "$root/usr/share/icons" hicolor
if [ ! -e "$py/louver/__main__.py" ] || [ ! -d "$root/usr/share/icons/hicolor/scalable" ]; then
  echo "build-deb: tracked files missing from the package" >&2
  exit 1
fi

# -I: never pick up a louver/ directory from the cwd, or PYTHON* variables.
cat > "$root/usr/bin/louver" <<'WRAP'
#!/bin/sh
exec /usr/bin/python3 -I -m louver "$@"
WRAP

unit="$root/usr/lib/systemd/user/louver.service"
sed -e '/^Environment="PYTHONPATH=/d' \
  -e 's|^Documentation=.*|Documentation=file:///usr/share/doc/louver/README.md|' \
  -e 's|^ExecStart=.*|ExecStart=/usr/bin/louver daemon|' \
  "$here/packaging/louver.service" > "$unit"
if grep -q -e PYTHONPATH -e '\.local' "$unit" || ! grep -q '^ExecStart=/usr/bin/louver daemon$' "$unit"; then
  echo "build-deb: packaging/louver.service changed shape; update the sed above" >&2
  exit 1
fi

# /run/louver, for the machine-wide hardware lock (louver/instance.py).
cp "$here/packaging/louver.tmpfiles" "$root/usr/lib/tmpfiles.d/louver.conf"

# 70, not 99: TAG+="uaccess" only takes effect before 73-seat-late.rules.
cp "$here/packaging/99-louver.rules" "$root/usr/lib/udev/rules.d/70-louver.rules"
cp "$here/packaging/$app_id.desktop" "$root/usr/share/applications/"
cp "$here/README.md" "$here/NOTICE" "$root/usr/share/doc/louver/"

indent() {  # a licence text as a DEP-5 field body: one leading space, "." for blank lines
  sed -e 's/^/ /' -e 's/^ *$/ ./' "$@"
}
{
  cat <<'COPY'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: louver
Source: https://github.com/ronaldorcampos/louver

Files: *
COPY
  # LICENSE is the bare GPL text (so GitHub detects it); NOTICE names the holder.
  ours="$(sed -n 's/^Copyright (c) \([^.]*\)\..*/\1/p' "$here/NOTICE" | head -1)"
  echo "Copyright: $ours"
  echo "License: GPL-2+"
  echo
  # The Lian Li drivers follow lian-li-linux's Rust implementation (see NOTICE).
  echo "Files: louver/devices.py"
  echo "Copyright: $ours"
  echo "           $(sed -n 's/^ *Copyright (c) \(.*sgtaziz\).*/\1/p' "$here/NOTICE" | head -1)"
  echo "License: GPL-2+ and MIT"
  echo "Comment: Follows lian-li-linux (https://github.com/sgtaziz/lian-li-linux), MIT."
  echo
  echo "Files: louver/aura.py"
  echo "Copyright: 2020 Martin Hartl (inlart)"
  echo "           2020 Adam Honse (CalcProgrammer1)"
  echo "           other OpenRGB contributors"
  echo "           $ours"
  echo "License: GPL-2+"
  echo "Comment: A port of OpenRGB's Aura USB mainboard driver"
  echo " (https://gitlab.com/CalcProgrammer1/OpenRGB) at commit"
  echo " $(sed -n 's/^OPENRGB_COMMIT = "\([0-9a-f]*\)"$/\1/p' "$here/louver/aura.py"); see NOTICE."
  echo
  echo "Files: louver/web/fonts/*"
  echo "Copyright: $(sed -n '1s/^Copyright //p' "$here/louver/web/fonts/LICENSE-unbounded.txt")"
  echo "           $(sed -n '1s/^Copyright //p' "$here/louver/web/fonts/LICENSE-martian-mono.txt")"
  echo "License: OFL-1.1"
  indent <(sed '1,2d' "$here/louver/web/fonts/LICENSE-unbounded.txt")
  echo
  cat <<'GPL'
License: GPL-2+
 This program is free software; you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation; either version 2 of the License, or
 (at your option) any later version.
 .
 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.
 .
 On Debian systems, the full text of the GNU General Public
 License version 2 can be found in the file
 `/usr/share/common-licenses/GPL-2'.

GPL
  echo "License: MIT"
  # lian-li-linux's permission text (NOTICE), without its copyright line
  indent <(sed -n '/^Permission is hereby granted/,/^SOFTWARE\.$/p' "$here/NOTICE")
} > "$root/usr/share/doc/louver/copyright"

find "$root" -type d -exec chmod 755 {} +
find "$root" -type f -exec chmod 644 {} +
chmod 755 "$root/usr/bin/louver"
for s in postinst prerm postrm; do
  install -m 755 "$here/packaging/deb/$s" "$root/DEBIAN/$s"
done

cat > "$root/DEBIAN/control" <<CONTROL
Package: louver
Version: $version
Architecture: all
Maintainer: Ronaldo Campos <ronaldo.romanello@gmail.com>
Installed-Size: $(du -sk --apparent-size "$root/usr" | cut -f1)
Depends: python3 (>= 3.10)
Recommends: python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, gir1.2-ayatanaappindicator3-0.1
Section: utils
Priority: optional
Homepage: https://github.com/ronaldorcampos/louver
Description: fan and RGB control for Lian Li UNI HUB AL V2 and Strimer Plus
 A fan-control daemon (a systemd user service) with a local web UI, plus a
 desktop app with a tray icon. It also drives motherboard fan headers on
 Nuvoton nct67xx and ITE it87 boards, each one opt-in.
CONTROL

deb="$out/louver_${version}_all.deb"
dpkg-deb --root-owner-group --build "$root" "$deb" >/dev/null
echo "$deb"
