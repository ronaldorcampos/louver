"""Login autostart for the Louver window/tray (XDG autostart entry).

Fan control itself starts via the systemd user service; this only decides
whether the desktop app (tray icon) comes up at login.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .gui_ids import APP_ID

MARKER = "X-Louver-Managed=true"


def entry_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "autostart" / "louver.desktop"


def _launcher() -> str:
    local = Path.home() / ".local" / "bin" / "louver"
    return str(local) if local.exists() else (shutil.which("louver") or "louver")


def sync(enabled: bool, path: Path | None = None, launcher: str | None = None) -> None:
    path = path or entry_path()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Louver\n"
            "Comment=Lian Li fan and RGB control (tray)\n"
            f"Exec={launcher or _launcher()} app --autostart\n"
            f"Icon={APP_ID}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
            f"{MARKER}\n"
        )
    elif path.exists() and MARKER in path.read_text(errors="replace"):
        path.unlink()  # only ever remove an entry we created
