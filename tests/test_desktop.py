"""The desktop side: the login autostart entry and the icons the app names."""

import re
import socket
from pathlib import Path

import pytest

from louver import __main__ as cli
from louver import apiclient, autostart
from louver import config as cfgmod


def test_daemon_that_cannot_serve_leaves_autostart_alone(tmp_path, monkeypatch):
    from louver import daemon

    # Never let this reach the hardware, whatever serve() does.
    monkeypatch.setattr(daemon.Controller, "run_forever", lambda self, stop: pytest.fail("daemon ran"))
    monkeypatch.setattr(daemon.Controller, "shutdown", lambda self: None)
    sock = apiclient.socket_path()
    sock.parent.mkdir(mode=0o700)
    busy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    busy.bind(str(sock))  # a copy that's still serving (the demo, on the default socket)
    busy.listen()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = cfgmod.validate({})
    cfg["app"]["start_on_login"] = True
    cfgmod.save(cfg, tmp_path / "louver" / "config.json")
    try:
        assert cli.main(["daemon"]) == cli.ALREADY_RUNNING
    finally:
        busy.close()
    assert sock.is_socket()  # left to its owner
    assert not (tmp_path / "autostart" / "louver.desktop").exists()  # only the running copy writes it


def test_explicit_config_is_used_and_status_reports_unreachable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    other = tmp_path / "elsewhere.json"
    other.write_text('{"poll_interval": 2}\n')
    assert cli.main(["--config", str(other), "status"]) == 1  # nothing serves the (test's) socket
    assert "louver status: Louver isn't running: systemctl --user start louver" in capsys.readouterr().err
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
