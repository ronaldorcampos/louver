"""unihub command line.

  unihub daemon            run fan control + the web UI (127.0.0.1:47321)
  unihub ui                open the UI in an app-style browser window
  unihub status            show RPM / duty / temps from the running daemon
  unihub set PORT PERCENT  put a port in manual mode at PERCENT (via daemon)
  unihub probe             read-only hardware check, no daemon needed
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

from . import config as cfgmod
from .devices import DRIVERS, VENDOR_ID, StrimerPlus, UniHubALV2
from .hidraw import HidrawTransport, enumerate_hidraw


def _url(cfg: dict, path: str) -> str:
    return f"http://{cfg['listen']}{path}"


def _api(cfg: dict, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(_url(cfg, path), data=data, method="GET" if body is None else "PUT")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-UniHub", "1")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def cmd_daemon(args, cfg: dict, path: Path) -> int:
    from .daemon import Controller, serve
    from .sensors import SensorHub

    if args.demo:
        from .sim import open_sim

        # Demo never touches real hardware or the real config file.
        ctrl = Controller(cfg, None, SensorHub(), opener=open_sim)
    else:
        ctrl = Controller(cfg, path, SensorHub())
    stop = threading.Event()

    def _stop(*_):
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    httpd = serve(ctrl, args.listen or cfg["listen"])
    logging.info("UI at http://%s/", args.listen or cfg["listen"])
    try:
        ctrl.run_forever(stop)
    finally:
        httpd.shutdown()
        ctrl.shutdown()
    return 0


def cmd_ui(args, cfg: dict, path: Path) -> int:
    url = _url(cfg, "/")
    for browser in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser", "microsoft-edge"):
        exe = shutil.which(browser)
        if exe:
            subprocess.Popen([exe, f"--app={url}", "--class=unihub"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    for i, (g, gc) in enumerate(zip(hub["groups"], cfg["hub"]["groups"])):
        pct = "MB" if g["percent"] is None else f"{g['percent']:>3}%"
        temp = "  -  " if g["temp"] is None else f"{g['temp']:4.1f}C"
        flag = "  FAILSAFE (no temp)" if g["failsafe"] else ""
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
                print(f"  firmware {fw.version}  ids={fw.customer_id:#04x},{fw.project_id:#04x},"
                      f"{fw.major_id:#04x},{fw.minor_id:#04x}  {'OK' if fw.valid_alv2 else 'UNEXPECTED'}")
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="unihub", description="Lian Li UNI HUB control for Linux")
    p.add_argument("--config", type=Path, default=None, help="config file (default ~/.config/unihub/config.json)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daemon")
    d.add_argument("--listen", default=None)
    d.add_argument("--demo", action="store_true", help="simulate the hub and Strimer (no hardware access)")
    sub.add_parser("ui")
    sub.add_parser("status")
    s = sub.add_parser("set")
    s.add_argument("port", type=int, choices=range(1, 5))
    s.add_argument("percent", type=int, choices=range(0, 101), metavar="PERCENT")
    sub.add_parser("probe")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    path = args.config or cfgmod.default_path()
    try:
        cfg = cfgmod.load(path)
    except cfgmod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    return {"daemon": cmd_daemon, "ui": cmd_ui, "status": cmd_status, "set": cmd_set, "probe": cmd_probe}[args.cmd](
        args, cfg, path
    )


if __name__ == "__main__":
    sys.exit(main())
