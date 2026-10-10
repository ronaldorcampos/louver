"""louver command line.

louver daemon            run fan control + the web UI (127.0.0.1:47321)
louver app [--autostart] the desktop app: window + tray icon
louver ui                open the desktop app (falls back to a browser window)
louver status            show RPM / duty / temps from the running daemon
louver set PORT PERCENT  put a port in manual mode at PERCENT (via daemon)
louver probe             read-only hardware check, no daemon needed
louver aura-probe        ASUS Aura motherboard RGB: firmware and channels (read-only)
louver ram-probe         Corsair DDR5 RAM RGB: the sticks the louver-ram helper found
louver mb-list           motherboard fan headers: RPM, PWM, mode (read-only)
louver mb-test N PCT     run motherboard header N at PCT for a few seconds, then hand it back to BIOS
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from . import aura, instance, ramrgb
from . import config as cfgmod
from .devices import DRIVERS, VENDOR_ID, StrimerPlus, UniHubALV2
from .hidraw import HidrawTransport, enumerate_hidraw

ALREADY_RUNNING = 3  # the unit's RestartPreventExitStatus: don't retry against a running copy


def _hardware_lock(what: str, hint: str):
    """Take the one-process-at-a-time hardware lock, or explain who holds it."""
    got = instance.try_acquire()
    if isinstance(got, instance.Lock):
        return got
    print(f"{what}: Louver is already running{f' (pid {got.pid})' if got.pid else ''}. {hint}", file=sys.stderr)
    return None


def _url(cfg: dict, path: str) -> str:
    return f"http://{cfg['listen']}{path}"


def _api(cfg: dict, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(_url(cfg, path), data=data, method="GET" if body is None else "PUT")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Louver", "1")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def cmd_daemon(args, cfg: dict, path: Path) -> int:
    from .daemon import Controller, serve
    from .sensors import SensorHub

    lock = None
    if not args.demo:  # the demo touches no hardware, so it may run alongside
        lock = _hardware_lock("louver daemon", "Check it with: systemctl --user status louver")
        if lock is None:
            return ALREADY_RUNNING
    if args.demo:
        from .sim import SimAuraAccess, SimRamAccess, open_sim, sim_motherboard

        # Demo never touches real hardware, the RAM helper or the real config
        # file, so it can show the simulated motherboard and RAM RGB without asking.
        cfg = {
            **cfg,
            "motherboard_rgb": {**cfg["motherboard_rgb"], "enabled": True},
            "ram_rgb": {**cfg["ram_rgb"], "enabled": True},
        }
        ctrl = Controller(
            cfg,
            None,
            SensorHub(),
            opener=open_sim,
            mbfans=sim_motherboard(),
            aura_access=SimAuraAccess(),
            ram_access=SimRamAccess(),
        )
    else:
        from .mbfans import MotherboardFans

        ctrl = Controller(cfg, path, SensorHub(), mbfans=MotherboardFans())
    try:
        httpd = serve(ctrl, args.listen or cfg["listen"])
    except OSError as e:
        print(f"louver daemon: cannot listen on {args.listen or cfg['listen']}: {e.strerror or e}", file=sys.stderr)
        return ALREADY_RUNNING if e.errno == 98 else 1  # EADDRINUSE
    if not args.demo:  # only once we're really the running copy
        from . import autostart

        try:
            autostart.sync(cfg["app"]["start_on_login"])
        except OSError as e:
            logging.warning("autostart update failed: %s", e)
    stop = threading.Event()

    def _stop(*_):
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logging.info("UI at http://%s/", args.listen or cfg["listen"])
    try:
        ctrl.run_forever(stop)
    finally:
        httpd.shutdown()
        ctrl.shutdown()
    return 0


def cmd_app(args, cfg: dict, path: Path) -> int:
    try:
        from . import gui
    except (ImportError, ValueError) as e:
        print(f"desktop app needs GTK3, WebKit2GTK 4.1 and AyatanaAppIndicator (python3-gi): {e}", file=sys.stderr)
        return 1
    return gui.run(_url(cfg, "/"), ["--autostart"] if args.autostart else [])


def cmd_ui(args, cfg: dict, path: Path) -> int:
    try:
        import gi  # noqa: F401  (desktop app available?)

        # Keep -I (the packaged /usr/bin/louver): without it, run from a
        # checkout, -m would start the checkout's copy instead.
        isolated = ["-I"] if sys.flags.isolated else []
        subprocess.Popen(
            [sys.executable, *isolated, "-m", "louver", "app"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return 0
    except ImportError:
        pass
    url = _url(cfg, "/")
    for browser in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "brave-browser",
        "microsoft-edge",
    ):
        exe = shutil.which(browser)
        if exe:
            subprocess.Popen(
                [exe, f"--app={url}", "--class=louver"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return 0
    subprocess.Popen(["xdg-open", url], start_new_session=True)
    return 0


def cmd_status(args, cfg: dict, path: Path) -> int:
    try:
        st = _api(cfg, "/api/state")
    except (urllib.error.URLError, OSError) as e:
        print(f"daemon not reachable at {cfg['listen']}: {e}", file=sys.stderr)
        return 1
    hub = st["hub"]
    print(f"{hub['name']}: {'connected' if hub['connected'] else 'NOT connected'} {hub['firmware']}")
    for g, gc in zip(hub["groups"], cfg["hub"]["groups"]):
        pct = "MB" if g["percent"] is None else f"{g['percent']:>3}%"
        temp = "  -  " if g["temp"] is None else f"{g['temp']:4.1f}C"
        flag = (
            "  FAILSAFE (no temp)"
            if g["failsafe"]
            else (f"  (unreadable: {', '.join(g['missing'])})" if g.get("missing") else "")
        )
        print(f"  {gc['name']:<12} {gc['mode']:<9} {pct}  {g['rpm']:>5} rpm  {temp}{flag}")
    s = st["strimer"]
    print(f"{s['name']}: {'connected' if s['connected'] else 'not connected'} {s['firmware']}")
    if st["error"]:
        print(f"error: {st['error']}")
    return 0


def cmd_set(args, cfg: dict, path: Path) -> int:
    live = _api(cfg, "/api/config")
    g = live["hub"]["groups"][args.port - 1]
    g["mode"] = "manual"
    g["manual_percent"] = args.percent
    _api(cfg, "/api/config", live)
    print(f"port {args.port}: manual {args.percent}%")
    return 0


def cmd_probe(args, cfg: dict, path: Path) -> int:
    """Read-only: firmware + RPM. Sends only query commands."""
    lock = _hardware_lock("louver probe", "Its live readings are in: louver status")
    if lock is None:
        return ALREADY_RUNNING
    rc = 1
    for info in enumerate_hidraw():
        if info.vid != VENDOR_ID or info.pid not in DRIVERS:
            continue
        print(f"{info.devnode}  {info.vid:04x}:{info.pid:04x}  {info.name}  sn={info.serial}")
        try:
            t = HidrawTransport(info.devnode)
        except OSError as e:
            print(f"  cannot open: {e}")
            continue
        dev = DRIVERS[info.pid](t)
        try:
            if isinstance(dev, UniHubALV2):
                fw = dev.read_firmware()
                print(
                    f"  firmware {fw.version}  ids={fw.customer_id:#04x},{fw.project_id:#04x},"
                    f"{fw.major_id:#04x},{fw.minor_id:#04x}  {'OK' if fw.valid_alv2 else 'UNEXPECTED'}"
                )
                print(f"  rpm per port: {dev.read_rpms()}")
            elif isinstance(dev, StrimerPlus):
                print(f"  firmware {dev.read_firmware()}  ports={dev.PORTS_PER_CHANNEL}+{dev.read_channel2_ports()}")
            rc = 0
        except OSError as e:
            print(f"  query failed: {e}")
        finally:
            dev.close()
    if rc:
        print("no usable Lian Li controller found", file=sys.stderr)
    return rc


def cmd_aura_probe(args, cfg: dict, path: Path) -> int:
    """Read-only: the Aura controller's firmware and config table. Sends only
    the two query commands (firmware version, config table)."""
    lock = _hardware_lock(
        "louver aura-probe",
        "Stop the service first (systemctl --user stop louver), or see Settings → Motherboard RGB in the app.",
    )
    if lock is None:
        return ALREADY_RUNNING
    rc = 1
    for info in aura.find_controllers():
        print(f"{info.devnode}  {info.vid:04x}:{info.pid:04x}  {info.name}")
        try:
            dev = aura.AuraMainboard(HidrawTransport(info.devnode))
        except OSError as e:
            print(f"  cannot open: {e}")
            continue
        try:
            try:
                print(f"  firmware {dev.read_firmware()}")
            except TimeoutError:
                print("  firmware: no reply")
            channels = dev.read_config()
            print("  config table:")
            for i in range(0, len(dev.config_table), 6):
                print("    " + dev.config_table[i : i + 6].hex(" "))
            for ch in channels:
                if ch.kind == "argb":
                    what = "LED count not reported by the controller"
                elif ch.rgb_headers:
                    what = f"{ch.num_leds} LEDs, {ch.rgb_headers} of them 12 V RGB headers"
                else:
                    what = f"{ch.num_leds} LEDs"
                if not dev.fits(ch.index):
                    what += "; its effect colour doesn't fit one report, so Louver skips it"
                print(f"  {ch.key:<8} {ch.name:<14} effect channel {ch.effect_channel}  {what}")
            if not channels:
                print("  no channels in the config table")
            rc = 0
        except OSError as e:
            print(f"  query failed: {e}")
        finally:
            dev.close()
    if rc:
        print("no usable ASUS Aura motherboard RGB controller found", file=sys.stderr)
    return rc


def cmd_ram_probe(args, cfg: dict, path: Path, access=None) -> int:
    """What the louver-ram helper found on the SMBus. Only asks the helper
    (which scans on its first request); never touches the bus itself, so it
    needs no hardware lock: the helper serialises its bus access."""
    access = access if access is not None else ramrgb.RamHelperAccess(timeout=ramrgb.PROBE_TIMEOUT)
    try:
        client = access.connect()
    except OSError as e:
        print(f"louver ram-probe: {ramrgb.connect_error(access.path, e)}", file=sys.stderr)
        return 1
    try:
        listing = client.list(timeout=ramrgb.PROBE_TIMEOUT)  # the first one includes the scan
    except (OSError, ValueError, ramrgb.RamHelperError) as e:
        print(f"louver ram-probe: the RAM helper didn't answer: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    if listing.bus:
        print(f"{listing.bus}  {listing.device}")
    for st in listing.sticks:
        print(
            f"  {st.addr:#04x}  {st.name}  pid {st.pid:#06x}  {st.leds} LEDs"
            f"  firmware {st.firmware}  protocol {st.protocol}"
        )
    for addr, reason in listing.skipped:
        print(f"  {addr:#04x}  skipped: {reason}")
    if not listing.sticks:
        print(f"no Corsair DDR5 RGB stick to drive: {listing.error or 'none found'}", file=sys.stderr)
        return 1
    return 0


def cmd_mb_list(args, cfg: dict, path: Path) -> int:
    from .mbfans import MotherboardFans

    mb = MotherboardFans()
    if not mb.present:
        print("no supported motherboard fan chip found", file=sys.stderr)
        return 1
    names = {h["index"]: (h["name"], h["mode"]) for h in cfg["motherboard"]["headers"]}
    print(f"{mb.chip} at {mb.dir}  ({'writable' if mb.writable() else 'read-only: install the hwmon udev rule'})")
    for d in mb.describe():
        name, mode = names.get(d["index"], ("", "bios"))
        auto = "manual" if d["enable"] == 1 else f"auto({d['enable']})"
        pct = round((d["pwm"] or 0) / 2.55)
        print(
            f"  header {d['index']}: {d['rpm']:>5} rpm  pwm {d['pwm']:>3}/255 ({pct:>3}%)  {auto:<8} {mode:<8} {name}"
        )
    return 0


def cmd_mb_test(args, cfg: dict, path: Path) -> int:
    import time

    from .mbfans import MotherboardFans

    lock = _hardware_lock(
        "louver mb-test", "Stop it first so the two don't fight over the header: systemctl --user stop louver"
    )
    if lock is None:
        return ALREADY_RUNNING
    mb = MotherboardFans()
    if args.header not in mb.headers:
        print(f"no header {args.header}; have {sorted(mb.headers)}", file=sys.stderr)
        return 1
    h = mb.headers[args.header]
    print(f"header {args.header}: {h.rpm} rpm, pwm {h.pwm}, enable {h.enable} (before)")
    try:
        mb.set_percent(args.header, args.percent)
        for s in range(args.seconds):
            time.sleep(1)
            print(f"  {s + 1:>2}s  {h.rpm:>5} rpm  (pwm {h.pwm})", flush=True)
    except PermissionError:
        print("not writable: install the hwmon udev rule (see README)", file=sys.stderr)
        return 1
    finally:
        mb.release(args.header)
    print(f"header {args.header}: handed back to BIOS (enable {h.enable})")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="louver", description="Lian Li UNI HUB control for Linux")
    p.add_argument("--config", type=Path, default=None, help="config file (default ~/.config/louver/config.json)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daemon")
    d.add_argument("--listen", default=None)
    d.add_argument("--demo", action="store_true", help="simulate the hub, Strimer and motherboard (no hardware access)")
    sub.add_parser("ui")
    a = sub.add_parser("app")
    a.add_argument("--autostart", action="store_true", help="launched at login: honour 'start minimized'")
    sub.add_parser("status")
    s = sub.add_parser("set")
    s.add_argument("port", type=int, choices=range(1, 5))
    s.add_argument("percent", type=int, choices=range(0, 101), metavar="PERCENT")
    sub.add_parser("probe")
    sub.add_parser("aura-probe")
    sub.add_parser("ram-probe")
    sub.add_parser("mb-list")
    t = sub.add_parser("mb-test")
    t.add_argument("header", type=int)
    t.add_argument("percent", type=int, choices=range(0, 101), metavar="PERCENT")
    t.add_argument("--seconds", type=int, default=10)
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    path = args.config or cfgmod.default_path()
    try:
        cfg = cfgmod.load(path)
    except cfgmod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    return {
        "daemon": cmd_daemon,
        "ui": cmd_ui,
        "status": cmd_status,
        "set": cmd_set,
        "probe": cmd_probe,
        "aura-probe": cmd_aura_probe,
        "ram-probe": cmd_ram_probe,
        "app": cmd_app,
        "mb-list": cmd_mb_list,
        "mb-test": cmd_mb_test,
    }[args.cmd](args, cfg, path)


if __name__ == "__main__":
    sys.exit(main())
