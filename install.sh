#!/usr/bin/env bash
# Per-user install: no pip, no root except for the udev rule.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
app_id="io.github.ronaldorcampos.Louver"
share="$HOME/.local/share/louver"
bin="$HOME/.local/bin"
units="$HOME/.config/systemd/user"
apps="$HOME/.local/share/applications"
icons="$HOME/.local/share/icons/hicolor"

# --- Install Louver -------------------------------------------------------------
mkdir -p "$share" "$bin" "$units" "$apps" "$icons"
rm -rf "$share/louver"
cp -r "$here/louver" "$share/louver"
find "$share/louver" -name __pycache__ -prune -exec rm -rf {} +
cp "$here/README.md" "$share/README.md"

cat > "$bin/louver" <<WRAP
#!/bin/sh
PYTHONPATH="$share\${PYTHONPATH:+:\$PYTHONPATH}" exec /usr/bin/python3 -m louver "\$@"
WRAP
chmod +x "$bin/louver"

cp "$here/packaging/louver.service" "$units/louver.service"
# Named after the app ID so GNOME matches the window (Wayland) to this entry.
cp "$here/packaging/$app_id.desktop" "$apps/$app_id.desktop"
cp -r "$here/packaging/icons/hicolor/." "$icons/"
gtk-update-icon-cache -q -t "$icons" 2>/dev/null || true
systemctl --user daemon-reload
echo "Installed to $share"

# --- Start the service and the tray app on the new code -------------------------
# A re-run picks up the new code.
if systemctl --user is-active --quiet louver 2>/dev/null; then
  if ! systemctl --user restart louver; then
    echo "Couldn't restart the Louver service; see: journalctl --user -u louver -e" >&2
  else
    sleep 2
    if systemctl --user is-active --quiet louver; then
      echo "Louver service is running"
    else
      echo "Louver service failed to start; see: journalctl --user -u louver -e" >&2
    fi
  fi
fi
# A running window/tray app is restarted on the new code, the way it was
# started: tray-only if it came up at login, else with its window. Only from
# a desktop session, which the new copy needs.
app_pattern='^/usr/bin/python3 -m louver app'
if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && pgrep -f "$app_pattern" >/dev/null 2>&1; then
  app_args=(app)
  case "$(pgrep -af "$app_pattern")" in *--autostart*) app_args+=(--autostart) ;; esac
  pkill -f "$app_pattern" 2>/dev/null || true
  sleep 1
  if setsid -f "$bin/louver" "${app_args[@]}" >/dev/null 2>&1 < /dev/null; then
    echo "Restarted the tray app"
  fi
fi

if [ ! -e /etc/udev/rules.d/99-louver.rules ]; then
  cat <<MSG

One step needs root - the udev rule that lets you open the hub without sudo:
  sudo install -m 644 "$here/packaging/99-louver.rules" /etc/udev/rules.d/
  sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw
MSG
fi
if ! systemctl --user is-enabled --quiet louver 2>/dev/null; then
  cat <<MSG

Then start it (and on every login):
  systemctl --user enable --now louver
MSG
fi
echo "Open Louver from your app menu or run: louver app"
