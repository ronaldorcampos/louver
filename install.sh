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

# The app used to be called UniHub. Whether to take over from it is decided
# here, but it is only retired after Louver is fully in place, so a failure
# partway through never leaves you with neither service.
old_unit="$units/unihub.service"
was_enabled=0 was_active=0
if [ -e "$old_unit" ]; then
  if systemctl --user is-enabled --quiet unihub 2>/dev/null; then was_enabled=1; fi
  if systemctl --user is-active --quiet unihub 2>/dev/null; then was_active=1; fi
fi

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

# --- Retire UniHub ----------------------------------------------------------------
# Only files the old install.sh created are removed; ~/.config/unihub stays as
# a backup (Louver copies the config from it on first run).
if [ -e "$old_unit" ]; then
  # Stopping applies its on-exit setting (by default the fans go back to the
  # motherboard) for the moment until Louver starts below. Stop first and
  # disable after, so an abort here leaves it enabled and a re-run takes over.
  systemctl --user stop unihub 2>/dev/null || true
  if systemctl --user is-active --quiet unihub; then
    echo "Couldn't stop the old UniHub service. Stop it with 'systemctl --user stop unihub', then run ./install.sh again." >&2
    exit 1
  fi
  if ! systemctl --user disable unihub; then
    echo "Couldn't disable the old UniHub service; run ./install.sh again." >&2
    exit 1
  fi
  rm -f "$old_unit"
  systemctl --user daemon-reload
  echo "Removed the old UniHub service"
fi
rm -f "$bin/unihub" "$apps/unihub.desktop" "$apps/io.github.unihub.desktop" "$icons/scalable/apps/unihub.svg"
if [ -d "$HOME/.local/share/unihub" ]; then
  rm -rf "$HOME/.local/share/unihub/unihub"
  rm -f "$HOME/.local/share/unihub/README.md"
  rmdir "$HOME/.local/share/unihub" 2>/dev/null || true
fi

# --- Start the service and the tray app on the new code -------------------------
started=0
louver_service() {
  if systemctl --user "$@" louver; then
    started=1
  else
    echo "Couldn't $1 the Louver service; see: journalctl --user -u louver -e" >&2
  fi
}
if [ "$was_enabled" = 1 ]; then
  louver_service enable --now
elif [ "$was_active" = 1 ]; then
  louver_service start  # running but not enabled: same again
elif systemctl --user is-active --quiet louver 2>/dev/null; then
  louver_service restart  # re-run: pick up the new code
fi
if [ "$started" = 1 ]; then
  sleep 2
  if systemctl --user is-active --quiet louver; then
    echo "Louver service is running"
  else
    echo "Louver service failed to start; see: journalctl --user -u louver -e" >&2
  fi
fi
# A running window/tray app (old name or new) is restarted on the new code,
# the way it was started: tray-only if it came up at login, else with its
# window. Only from a desktop session, which the new copy needs.
app_pattern='^/usr/bin/python3 -m (unihub|louver) app'
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
  if [ -e /etc/udev/rules.d/99-unihub.rules ]; then
    cat <<MSG

The udev rule is still installed under its old name. It keeps working; to
rename it (needs root):
  sudo install -m 644 "$here/packaging/99-louver.rules" /etc/udev/rules.d/ && sudo rm /etc/udev/rules.d/99-unihub.rules
  sudo udevadm control --reload
MSG
  else
    cat <<MSG

One step needs root - the udev rule that lets you open the hub without sudo:
  sudo install -m 644 "$here/packaging/99-louver.rules" /etc/udev/rules.d/
  sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw
MSG
  fi
fi
if ! systemctl --user is-enabled --quiet louver 2>/dev/null; then
  cat <<MSG

Then start it (and on every login):
  systemctl --user enable --now louver
MSG
fi
echo "Open Louver from your app menu or run: louver app"
