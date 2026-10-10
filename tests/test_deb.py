"""Builds the .deb (when dpkg-deb is installed) and checks what it holds."""

import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "io.github.ronaldorcampos.Louver"


@pytest.fixture(scope="module")
def deb(tmp_path_factory):
    if shutil.which("dpkg-deb") is None:
        pytest.skip("dpkg-deb not installed")
    out = tmp_path_factory.mktemp("dist")
    r = subprocess.run([str(ROOT / "packaging" / "build-deb.sh"), str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return Path(r.stdout.strip())


def _run(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def _extract(deb, tmp_path):
    subprocess.run(["dpkg-deb", "-x", str(deb), str(tmp_path)], check=True)
    return tmp_path


def test_named_after_the_pyproject_version(deb):
    version = re.search(r'^version = "(.+)"$', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    assert deb.name == f"louver_{version}_all.deb"
    assert _run("dpkg-deb", "-f", str(deb), "Version").strip() == version


def test_contents(deb):
    listing = _run("dpkg-deb", "-c", str(deb))
    paths = {line.split()[-1].removeprefix(".") for line in listing.splitlines()}
    for p in (
        "/usr/bin/louver",
        "/usr/lib/python3/dist-packages/louver/__main__.py",
        "/usr/lib/python3/dist-packages/louver/web/index.html",
        "/usr/lib/python3/dist-packages/louver/web/fonts/unbounded.woff2",
        "/usr/lib/systemd/user/louver.service",
        "/usr/lib/udev/rules.d/70-louver.rules",
        f"/usr/share/applications/{APP_ID}.desktop",
        f"/usr/share/icons/hicolor/scalable/apps/{APP_ID}.svg",
        f"/usr/share/icons/hicolor/symbolic/apps/{APP_ID}-symbolic.svg",
        "/usr/share/doc/louver/copyright",
        "/usr/share/doc/louver/NOTICE",
    ):
        assert p in paths, p
    assert not [p for p in paths if "__pycache__" in p or p.endswith(".pyc")]
    assert all(line.split()[1] == "root/root" for line in listing.splitlines())


def test_installed_files_point_at_the_system_copy(deb, tmp_path):
    root = _extract(deb, tmp_path)
    unit = (root / "usr/lib/systemd/user/louver.service").read_text()
    assert "ExecStart=/usr/bin/louver daemon\n" in unit
    assert "PYTHONPATH" not in unit and ".local" not in unit
    wrapper = root / "usr/bin/louver"
    assert wrapper.stat().st_mode & 0o777 == 0o755
    assert "/usr/bin/python3 -I -m louver" in wrapper.read_text()


STUBS = {
    # Two logged-in users, as `loginctl list-users --no-legend` prints them.
    "loginctl": 'printf "%s\\n" "1000 alice no active" "1001 bob no active"',
    "runuser": 'echo "runuser $*" >> "$LOG"',
    "udevadm": 'echo "udevadm $*" >> "$LOG"',
    "py3compile": 'echo "py3compile $*" >> "$LOG"',
    "py3clean": 'echo "py3clean $*" >> "$LOG"',
}


@pytest.fixture
def scripts(deb, tmp_path):
    """Runs a maintainer script against stub system commands; returns what they were asked to do."""
    control = tmp_path / "DEBIAN"
    subprocess.run(["dpkg-deb", "-e", str(deb), str(control)], check=True)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in STUBS.items():
        (stubs / name).write_text(f"#!/bin/sh\n{body}\n")
        (stubs / name).chmod(0o755)
    log = tmp_path / "log"

    def run(script, *args):
        assert (control / script).stat().st_mode & 0o777 == 0o755
        log.write_text("")
        env = {"PATH": f"{stubs}:/usr/bin:/bin", "LOG": str(log)}
        r = subprocess.run(["sh", str(control / script), *args], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout, log.read_text().splitlines()

    return run


def _systemctl(*args):
    return [
        f"runuser -u {user} -- env XDG_RUNTIME_DIR=/run/user/{uid} systemctl --user {' '.join(args)}"
        for uid, user in (("1000", "alice"), ("1001", "bob"))
    ]


@pytest.mark.parametrize("old_version", ["", "0.1.0"])  # first install / upgrade or reinstall
def test_postinst_configure(scripts, old_version):
    out, calls = scripts("postinst", "configure", old_version)
    assert "py3compile -p louver" in calls
    assert [c for c in calls if c.startswith("runuser")] == [
        *_systemctl("daemon-reload"),  # a changed unit file applies too
        *_systemctl("try-restart", "louver.service"),
    ]
    assert "systemctl --user enable --now louver" in out  # never enabled for every account
    udev = [c for c in calls if c.startswith("udevadm")]
    if Path("/run/udev/control").is_socket():
        assert "udevadm trigger --action=change --subsystem-match=hidraw" in udev
        assert "udevadm trigger --action=add --subsystem-match=hwmon --attr-match=name=nct67*" in udev
    else:  # no udevd of this system's own (a chroot, a container): leave the host's devices alone
        assert udev == []


def test_prerm_remove_stops_running_daemons(scripts):
    _, calls = scripts("prerm", "remove")
    assert calls == [*_systemctl("stop", "louver.service"), "py3clean -p louver"]


def test_prerm_upgrade_leaves_daemons_running(scripts):
    _, calls = scripts("prerm", "upgrade", "0.2.0")
    assert calls == ["py3clean -p louver"]  # postinst try-restarts them on the new code


def test_postrm_parses(scripts):
    scripts("postrm", "purge")


def test_ui_relaunch_keeps_isolated_mode(monkeypatch):
    from louver import __main__ as cli

    launched = []
    monkeypatch.setitem(sys.modules, "gi", types.ModuleType("gi"))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    for isolated, expect in (
        (1, ["/usr/bin/python3", "-I", "-m", "louver", "app"]),
        (0, ["/usr/bin/python3", "-m", "louver", "app"]),
    ):
        fake = types.SimpleNamespace(flags=types.SimpleNamespace(isolated=isolated), executable="/usr/bin/python3")
        monkeypatch.setattr(cli, "sys", fake)
        assert cli.cmd_ui(None, {}, Path("x")) == 0
        assert launched.pop() == expect
