"""The desktop side: the login autostart entry and the icons the app names."""

import re
import socket
from pathlib import Path

from louver import __main__ as cli
from louver import autostart
from louver import config as cfgmod


def test_daemon_that_cannot_listen_leaves_autostart_alone(tmp_path, monkeypatch):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    cfg = cfgmod.validate({})
    cfg["app"]["start_on_login"] = True
    cfgmod.save(cfg, tmp_path / "louver" / "config.json")
    try:
        assert cli.main(["daemon", "--listen", f"127.0.0.1:{port}"]) == cli.ALREADY_RUNNING
    finally:
        busy.close()
    assert not (tmp_path / "autostart" / "louver.desktop").exists()  # only the running copy writes it


def test_explicit_config_is_used_and_status_reports_unreachable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    other = tmp_path / "elsewhere.json"
    other.write_text('{"listen": "127.0.0.1:1"}\n')  # never reach a real daemon
    assert cli.main(["--config", str(other), "status"]) == 1
    assert "daemon not reachable at 127.0.0.1:1" in capsys.readouterr().err
    assert not (tmp_path / "cfg" / "louver" / "config.json").exists()  # the default config is left alone


ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "packaging" / "icons" / "hicolor"


def test_every_icon_name_the_app_uses_is_shipped():
    from louver import gui_ids

    shipped = {p.stem for p in ICONS.glob("*/apps/*.svg")}
    desktop = (ROOT / "packaging" / f"{gui_ids.APP_ID}.desktop").read_text()
    used = {gui_ids.APP_ID, gui_ids.TRAY_ICON, re.search(r"^Icon=(.+)$", desktop, re.M).group(1)}
    assert used <= shipped, used - shipped
    assert re.search(r"^StartupWMClass=(.+)$", desktop, re.M).group(1) == gui_ids.APP_ID
    assert f"Icon={gui_ids.APP_ID}" in autostart_entry_text()
    for size in ("16x16", "22x22", "24x24", "32x32", "scalable"):
        assert (ICONS / size / "apps" / f"{gui_ids.APP_ID}.svg").is_file(), size


def autostart_entry_text():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "autostart" / "louver.desktop"
        autostart.sync(True, p, launcher="/x/louver")
        return p.read_text()
