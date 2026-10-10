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
        "/usr/lib/python3/dist-packages/louver/aura.py",
        "/usr/lib/python3/dist-packages/louver/web/index.html",
        "/usr/lib/python3/dist-packages/louver/web/fonts/unbounded.woff2",
        "/usr/lib/systemd/user/louver.service",
        "/usr/lib/systemd/system/louver-ram.socket",
        "/usr/lib/systemd/system/louver-ram.service",
        "/usr/lib/python3/dist-packages/louver/corsair_dram.py",
        "/usr/lib/python3/dist-packages/louver/ramhelper.py",
        "/usr/lib/python3/dist-packages/louver/ramrgb.py",
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
    copyright = (root / "usr/share/doc/louver/copyright").read_text()
    assert "Files: *\nCopyright: 2026 Ronaldo Campos\nLicense: GPL-2+\n" in copyright
    assert "License: GPL-2+\n This program is free software" in copyright
    assert "`/usr/share/common-licenses/GPL-2'" in copyright
    # lian-li-linux's MIT notice stays with the drivers that follow it (see NOTICE).
    assert "Files: louver/devices.py" in copyright and "2026 sgtaziz" in copyright
    assert "License: GPL-2+ and MIT" in copyright
    assert "License: MIT\n Permission is hereby granted" in copyright and "SIL OPEN FONT LICENSE" in copyright
    assert copyright.rstrip().endswith("SOFTWARE.")  # the MIT text ends where its notice in NOTICE does
    # The Aura driver is a port of OpenRGB's, at a pinned commit.
    aura = copyright.split("Files: louver/aura.py\n", 1)[1].split("\n\n", 1)[0]
    assert "Martin Hartl" in aura and "Adam Honse" in aura and "License: GPL-2+" in aura
    assert re.search(r"Comment: A port of OpenRGB's .*\n .*\n [0-9a-f]{40}; see NOTICE\.", aura)
    assert "MIT" not in copyright.split("Files: louver/devices.py", 1)[0]  # Files: * is no longer MIT
    # So is the Corsair DRAM driver.
    dram = copyright.split("Files: louver/corsair_dram.py\n", 1)[1].split("\n\n", 1)[0]
    assert "Adam Honse" in dram and "Erik Gilling" in dram and "License: GPL-2+" in dram
    assert "2026 Ronaldo Campos" in dram
    from louver.corsair_dram import OPENRGB_COMMIT

    assert re.search(
        r"Comment: A port of OpenRGB's Corsair DRAM driver\n .*\n " + OPENRGB_COMMIT + r"; see NOTICE\.", dram
    )
    rules = (root / "usr/lib/udev/rules.d/70-louver.rules").read_text()
    assert 'ATTRS{idVendor}=="0b05", ATTRS{idProduct}=="19af"' in rules
    sock = (root / "usr/lib/systemd/system/louver-ram.socket").read_text().splitlines()
    for line in ("ListenStream=/run/louver-ram.sock", "SocketUser=root", "SocketGroup=plugdev", "SocketMode=0660"):
        assert line in sock, line
    svc = (root / "usr/lib/systemd/system/louver-ram.service").read_text().splitlines()
    for line in (
        "ExecStart=/usr/bin/python3 -I -m louver.ramhelper",
        "Requires=louver-ram.socket",
        "DevicePolicy=closed",
        "DeviceAllow=char-i2c rw",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "SystemCallFilter=@system-service",
        "SystemCallFilter=~@privileged @resources",
        "ReadOnlyPaths=/dev",  # device nodes still open read-write on a read-only mount
    ):
        assert line in svc, line
    assert not [
        x for x in svc if x.startswith(("PrivateDevices", "PrivateUsers", "User="))
    ]  # both would hide /dev/i2c-*
    assert "[Install]" not in svc  # only the socket starts it
    # No udev rule ever hands /dev/i2c-* to users: the helper is the only way to the SMBus.
    assert "i2c" not in (root / "usr/lib/udev/rules.d/70-louver.rules").read_text()
    wrapper = root / "usr/bin/louver"
    assert wrapper.stat().st_mode & 0o777 == 0o755
    assert "/usr/bin/python3 -I -m louver" in wrapper.read_text()


STUBS = {
    # Two logged-in users, as `loginctl list-users --no-legend` prints them.
    "loginctl": 'printf "%s\\n" "1000 alice no active" "1001 bob no active"',
    # Reads stdin like a real command may: without </dev/null in the scripts it
    # would eat the rest of the user list.
    "runuser": 'cat >/dev/null; echo "runuser $*" >> "$LOG"',
    "udevadm": 'echo "udevadm $*" >> "$LOG"',
    "py3compile": 'echo "py3compile $*" >> "$LOG"',
    "py3clean": 'echo "py3clean $*" >> "$LOG"',
}
# Never the real ones: the scripts' system-unit calls land here. Like the
# real deb-systemd-helper, the stub refuses to run outside dpkg, and its
# was-enabled answers as WAS_ENABLED says.
SYSTEMD_STUBS = {
    "systemctl": 'echo "systemctl $*" >> "$LOG"',
    "deb-systemd-helper": """[ -n "$DPKG_MAINTSCRIPT_PACKAGE" ] || exit 1
echo "deb-systemd-helper $*" >> "$LOG"
case "$*" in *was-enabled*) [ "$WAS_ENABLED" = yes ] ;; esac""",
    "deb-systemd-invoke": 'echo "deb-systemd-invoke $*" >> "$LOG"',
}
BASICS = ("cat",)  # all a stub needs from the system: PATH reaches nothing else


def _stub_dir(path, stubs):
    path.mkdir()
    for name, body in stubs.items():
        (path / name).write_text(f"#!/bin/sh\n{body}\n")
        (path / name).chmod(0o755)
    return path


@pytest.fixture
def scripts(deb, tmp_path):
    """Runs a maintainer script against stub system commands; returns what
    they were asked to do. ``systemd`` picks the systemd-running branch (the
    script's /run/systemd/system check is pointed at a directory that exists
    or not), whatever this host runs; ``dpkg_root`` sets DPKG_ROOT, as for an
    install into another root; ``tools=False`` takes systemctl and the
    deb-systemd-* helpers away."""
    control = tmp_path / "DEBIAN"
    subprocess.run(["dpkg-deb", "-e", str(deb), str(control)], check=True)
    stubs = _stub_dir(tmp_path / "bin", STUBS)
    systemd_stubs = _stub_dir(tmp_path / "systemd-bin", SYSTEMD_STUBS)
    basics = tmp_path / "basics"
    basics.mkdir()
    for name in BASICS:
        (basics / name).symlink_to(shutil.which(name))
    log = tmp_path / "log"
    running = tmp_path / "run-systemd-system"
    running.mkdir()

    def run(script, *args, systemd=True, tools=True, was_enabled=True, dpkg_root=None):
        path = control / script
        assert path.stat().st_mode & 0o777 == 0o755
        text = path.read_text()
        assert text.count("/run/systemd/system") >= 1  # or the swap below would test nothing
        copy = tmp_path / f"{script}.run"
        copy.write_text(text.replace("/run/systemd/system", str(running if systemd else tmp_path / "absent")))
        log.write_text("")
        env = {
            "PATH": ":".join(str(d) for d in ([stubs, systemd_stubs] if tools else [stubs]) + [basics]),
            "LOG": str(log),
            "DPKG_MAINTSCRIPT_PACKAGE": "louver",  # dpkg sets it
            "WAS_ENABLED": "yes" if was_enabled else "no",
        }
        if dpkg_root is not None:
            env["DPKG_ROOT"] = dpkg_root
        r = subprocess.run(["/bin/sh", str(copy), *args], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout, log.read_text().splitlines()

    return run


def _systemctl(*args):
    return [
        f"runuser -u {user} -- env XDG_RUNTIME_DIR=/run/user/{uid} systemctl --user {' '.join(args)}"
        for uid, user in (("1000", "alice"), ("1001", "bob"))
    ]


# Where the scripts may (True) or must not (False) talk to the running
# systemd: it runs, it doesn't, or it runs but dpkg installs into another root.
HOSTS = [
    pytest.param({"systemd": True}, True, id="systemd"),
    pytest.param({"systemd": False}, False, id="no-systemd"),
    pytest.param({"systemd": True, "dpkg_root": "/srv/target"}, False, id="dpkg-root"),
    pytest.param({"systemd": True, "dpkg_root": ""}, True, id="empty-dpkg-root"),
]


def _system(calls):
    return [c for c in calls if c.startswith(("systemctl", "deb-systemd"))]


@pytest.mark.parametrize("old_version", ["", "0.1.0"])  # first install / upgrade or reinstall
def test_postinst_configure(scripts, old_version):
    out, calls = scripts("postinst", "configure", old_version)
    assert "py3compile -p louver" in calls
    assert [c for c in calls if c.startswith("runuser")] == [
        *_systemctl("daemon-reload"),  # a changed unit file applies too
        *_systemctl("try-restart", "louver.service"),
    ]
    assert _system(calls) == [
        "deb-systemd-helper unmask louver-ram.socket",
        "deb-systemd-helper --quiet was-enabled louver-ram.socket",
        "deb-systemd-helper enable louver-ram.socket",  # the socket only: the helper starts on a connection
        "systemctl --system daemon-reload",
        "deb-systemd-invoke start louver-ram.socket",
        *(["deb-systemd-invoke try-restart louver-ram.service"] if old_version else []),
    ]
    assert "systemctl --user enable --now louver" in out  # never enabled for every account
    udev = [c for c in calls if c.startswith("udevadm")]
    if Path("/run/udev/control").is_socket():
        assert "udevadm trigger --action=change --subsystem-match=hidraw" in udev
        assert "udevadm trigger --action=add --subsystem-match=hwmon --attr-match=name=nct67*" in udev
    else:  # no udevd of this system's own (a chroot, a container): leave the host's devices alone
        assert udev == []


def test_postinst_keeps_an_admins_disable(scripts):
    _, calls = scripts("postinst", "configure", "0.1.0", was_enabled=False)
    assert _system(calls) == [
        "deb-systemd-helper unmask louver-ram.socket",
        "deb-systemd-helper --quiet was-enabled louver-ram.socket",
        "deb-systemd-helper update-state louver-ram.socket",  # not enabled again
        "systemctl --system daemon-reload",
        "deb-systemd-invoke start louver-ram.socket",  # a no-op for a disabled unit
        "deb-systemd-invoke try-restart louver-ram.service",
    ]


@pytest.mark.parametrize("host, live", HOSTS)
def test_postinst_starts_the_socket_only_on_a_live_system(scripts, host, live):
    # A chroot, an image build, a container without systemd, an install into
    # another root: the enable link is made for the next boot, nothing is started.
    _, calls = scripts("postinst", "configure", "", **host)
    live_calls = ["systemctl --system daemon-reload", "deb-systemd-invoke start louver-ram.socket"]
    assert _system(calls) == [
        "deb-systemd-helper unmask louver-ram.socket",
        "deb-systemd-helper --quiet was-enabled louver-ram.socket",
        "deb-systemd-helper enable louver-ram.socket",
        *(live_calls if live else []),
    ]


@pytest.mark.parametrize("action", ["abort-upgrade", "abort-deconfigure", "abort-remove"])
def test_postinst_abort_puts_the_socket_back(scripts, action):
    _, calls = scripts("postinst", action, "0.1.0")
    assert "py3compile -p louver" not in calls and not [c for c in calls if c.startswith("runuser")]
    assert _system(calls)[:3] == [
        "deb-systemd-helper unmask louver-ram.socket",
        "deb-systemd-helper --quiet was-enabled louver-ram.socket",
        "deb-systemd-helper enable louver-ram.socket",
    ]
    assert "deb-systemd-invoke start louver-ram.socket" in calls


@pytest.mark.parametrize(
    "script, args", [("postinst", ("configure", "")), ("prerm", ("remove",)), ("postrm", ("purge",))]
)
def test_scripts_go_on_without_the_systemd_tools(scripts, script, args):
    _, calls = scripts(script, *args, tools=False)
    assert _system(calls) == []


@pytest.mark.parametrize("host, live", HOSTS)
def test_prerm_remove_stops_running_daemons(scripts, host, live):
    _, calls = scripts("prerm", "remove", **host)
    ram = ["deb-systemd-invoke stop louver-ram.socket louver-ram.service"] if live else []
    assert calls == [*_systemctl("stop", "louver.service"), *ram, "py3clean -p louver"]  # the link stays: postrm


def test_prerm_upgrade_leaves_daemons_running(scripts):
    _, calls = scripts("prerm", "upgrade", "0.2.0")
    assert calls == ["py3clean -p louver"]  # postinst try-restarts them on the new code


@pytest.mark.parametrize("host, live", HOSTS)
@pytest.mark.parametrize(
    "action, helper",
    [
        ("remove", ["deb-systemd-helper mask louver-ram.socket"]),  # inert until a reinstall unmasks it
        ("purge", ["deb-systemd-helper purge louver-ram.socket", "deb-systemd-helper unmask louver-ram.socket"]),
    ],
)
def test_postrm_forgets_the_ram_units(scripts, action, helper, host, live):
    _, calls = scripts("postrm", action, **host)
    assert _system(calls) == [*(["systemctl --system daemon-reload"] if live else []), *helper]


def test_postrm_upgrade_does_nothing(scripts):
    assert scripts("postrm", "upgrade", "0.2.0")[1] == []


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
