"""Taking over from the app's previous name, UniHub."""

import errno
import json
import logging
import os
import re
import socket
from pathlib import Path

import pytest

from louver import __main__ as cli
from louver import autostart
from louver import config as cfgmod


def old_config(root, text='{"poll_interval": 3}\n'):
    p = root / "unihub" / "config.json"
    p.parent.mkdir(parents=True)
    p.write_text(text)
    return p


def test_old_config_is_copied_and_kept_as_backup(tmp_path):
    old = old_config(tmp_path)
    new = tmp_path / "louver" / "config.json"
    assert cfgmod.migrate_legacy(new) is True
    assert new.read_text() == old.read_text() and old.exists()
    assert sorted(p.name for p in new.parent.iterdir()) == ["config.json"]  # no temp file left behind


def test_existing_louver_config_is_never_overwritten(tmp_path):
    old_config(tmp_path)
    new = tmp_path / "louver" / "config.json"
    new.parent.mkdir()
    new.write_text('{"poll_interval": 9}\n')
    assert cfgmod.migrate_legacy(new) is False
    assert json.loads(new.read_text())["poll_interval"] == 9


def test_nothing_to_migrate(tmp_path):
    new = tmp_path / "louver" / "config.json"
    assert cfgmod.migrate_legacy(new) is False and not new.exists()


def test_losing_the_race_to_another_process_keeps_its_config(tmp_path, monkeypatch):
    old_config(tmp_path)
    new = tmp_path / "louver" / "config.json"
    real_link = os.link

    def other_process_wins(src, dst):
        new.write_text('{"poll_interval": 7}\n')  # created between our exists() check and the link
        real_link(src, dst)

    monkeypatch.setattr(os, "link", other_process_wins)
    assert cfgmod.migrate_legacy(new) is False
    assert json.loads(new.read_text())["poll_interval"] == 7
    assert sorted(p.name for p in new.parent.iterdir()) == ["config.json"]


def test_cli_migrates_the_default_config_but_not_an_explicit_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    old_config(tmp_path, '{"poll_interval": 3, "listen": "127.0.0.1:1"}\n')
    other = tmp_path / "elsewhere.json"
    other.write_text('{"listen": "127.0.0.1:1"}\n')  # never reach a real daemon
    cli.main(["--config", str(other), "status"])
    assert not (tmp_path / "louver" / "config.json").exists()
    cli.main(["status"])  # daemon unreachable on port 1; the migration still happens first
    assert json.loads((tmp_path / "louver" / "config.json").read_text())["poll_interval"] == 3
    capsys.readouterr()


def test_old_autostart_entry_is_replaced_only_if_it_was_ours(tmp_path):
    d = tmp_path / "autostart"
    d.mkdir()
    old = d / "unihub.desktop"
    old.write_text(f"[Desktop Entry]\nExec=/home/x/.local/bin/unihub app --autostart\n{autostart.LEGACY_MARKER}\n")
    autostart.sync(True, d / "louver.desktop", launcher="/x/louver")
    assert not old.exists() and (d / "louver.desktop").exists()
    old.write_text("[Desktop Entry]\nExec=something-the-user-made\n")
    autostart.sync(False, d / "louver.desktop")
    assert old.exists()  # same file name, but not written by us: left alone


def test_a_copy_that_fails_partway_leaves_no_temp_file(tmp_path, monkeypatch):
    old_config(tmp_path)
    new = tmp_path / "louver" / "config.json"

    def disk_full(src, dst, **_):
        Path(dst).write_text('{"poll')  # half written, then the disk fills up
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(cfgmod.shutil, "copyfile", disk_full)
    with pytest.raises(OSError):
        cfgmod.migrate_legacy(new)
    assert list(new.parent.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 file")
def test_unreadable_old_config_warns_and_carries_on(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setitem(cfgmod.DEFAULTS, "listen", "127.0.0.1:1")  # it falls back to defaults; never reach a real daemon
    old = old_config(tmp_path, '{"listen": "127.0.0.1:1"}\n')
    old.chmod(0)
    try:
        with caplog.at_level(logging.WARNING):
            assert cli.main(["status"]) == 1  # ran on defaults; the daemon just isn't there
    finally:
        old.chmod(0o644)
    assert "could not copy the old UniHub config" in caplog.text


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
    old = tmp_path / "autostart" / "unihub.desktop"
    old.parent.mkdir()
    old.write_text(f"[Desktop Entry]\n{autostart.LEGACY_MARKER}\n")
    try:
        assert cli.main(["daemon", "--listen", f"127.0.0.1:{port}"]) == cli.ALREADY_RUNNING
    finally:
        busy.close()
    assert old.exists() and not (tmp_path / "autostart" / "louver.desktop").exists()


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
