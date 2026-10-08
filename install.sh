#!/usr/bin/env bash
# Per-user install: no pip, no root except for the udev rule.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
share="$HOME/.local/share/unihub"
bin="$HOME/.local/bin"
units="$HOME/.config/systemd/user"

mkdir -p "$share" "$bin" "$units" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
rm -rf "$share/unihub"
cp -r "$here/unihub" "$share/unihub"
find "$share/unihub" -name __pycache__ -prune -exec rm -rf {} +
cp "$here/README.md" "$share/README.md"

cat > "$bin/unihub" <<WRAP
#!/bin/sh
PYTHONPATH="$share\${PYTHONPATH:+:\$PYTHONPATH}" exec /usr/bin/python3 -m unihub "\$@"
WRAP
chmod +x "$bin/unihub"

cp "$here/packaging/unihub.service" "$units/unihub.service"
cp "$here/packaging/unihub.desktop" "$HOME/.local/share/applications/unihub.desktop"
cp "$here/packaging/unihub.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/unihub.svg"

systemctl --user daemon-reload
echo "Installed to $share"

if [ ! -e /etc/udev/rules.d/99-unihub.rules ]; then
  cat <<MSG

One step needs root - the udev rule that lets you open the hub without sudo:
  sudo install -m 644 "$here/packaging/99-unihub.rules" /etc/udev/rules.d/
  sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw
MSG
fi
cat <<MSG

Then start it (and on every login):
  systemctl --user enable --now unihub
Open the UI from your app menu ("UniHub") or run: unihub ui
MSG
