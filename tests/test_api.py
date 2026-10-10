"""The API on its Unix socket: who can reach it, what it accepts, the client
the CLI and the tray use, and proxy(), which serves the app's louver:// page."""

import argparse
import errno
import json
import os
import socket
import stat
import struct
import sys
import threading
import time
import types

import pytest

from louver import __main__ as cli
from louver import apiclient, daemon
from louver import config as cfgmod

from .test_control import Rig, _req, server  # noqa: F401  (fixture)
from .test_mbfans import fake_chip, mb_cfg, read


def _raw(sock_path, data: bytes, timeout=5.0) -> bytes:
    """Send ``data`` as is and return everything the daemon sends back."""
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(timeout)
    c.connect(str(sock_path))
    try:
        c.sendall(data)
        out = b""
        try:
            while chunk := c.recv(65536):
                out += chunk
        except ConnectionResetError:  # closed with our request unread
            pass
        return out
    finally:
        c.close()


def _status(reply: bytes) -> int:
    return int(reply.split(b" ", 2)[1])


def _serve(tmp_path_factory):
    r = Rig()
    r.ctrl.tick()
    sock = tmp_path_factory.mktemp("api") / "api.sock"
    return r, sock, daemon.serve(r.ctrl, sock)


# ---------------- the socket file ----------------
def test_default_socket_is_private_and_removed_on_stop():
    path = apiclient.socket_path()  # under conftest's runtime dir
    path.parent.mkdir(mode=0o755)  # loose, as an older copy might have left it
    os.chmod(path.parent, 0o755)
    httpd = daemon.serve(Rig().ctrl, path)
    try:
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
        st = os.lstat(path)
        assert stat.S_ISSOCK(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
        assert _req(path, "/api/state")[0] == 200
    finally:
        httpd.stop()
    assert not path.exists()


def test_stale_socket_is_replaced():
    path = apiclient.socket_path()
    path.parent.mkdir(mode=0o700)
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(path))  # what a crashed copy leaves: the file, and nobody listening
    dead.close()
    httpd = daemon.serve(Rig().ctrl, path)
    try:
        assert _req(path, "/api/state")[0] == 200
    finally:
        httpd.stop()


def test_live_socket_is_left_alone():
    path = apiclient.socket_path()
    path.parent.mkdir(mode=0o700)
    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(path))
    live.listen()
    try:
        with pytest.raises(OSError) as e:
            daemon.serve(Rig().ctrl, path)
        assert e.value.errno == errno.EADDRINUSE
        assert path.is_socket()
    finally:
        live.close()


def test_non_socket_at_the_path_is_refused_and_kept():
    path = apiclient.socket_path()
    path.parent.mkdir(mode=0o700)
    path.write_text("not mine to delete\n")
    with pytest.raises(FileExistsError):
        daemon.serve(Rig().ctrl, path)
    assert path.read_text() == "not mine to delete\n"


def test_foreign_or_symlinked_or_shared_dir_is_refused(tmp_path, monkeypatch):
    mine = tmp_path / "d"
    mine.mkdir(mode=0o700)
    real_uid = os.getuid()
    with monkeypatch.context() as m:  # not monkeypatch.undo(): that would drop conftest's patches too
        m.setattr(os, "getuid", lambda: real_uid + 1)
        with pytest.raises(PermissionError, match="not a directory of yours"):
            daemon.serve(Rig().ctrl, mine / "api.sock")
    link = tmp_path / "link"
    link.symlink_to(mine)
    with pytest.raises(PermissionError, match="not a directory of yours"):
        daemon.serve(Rig().ctrl, link / "api.sock")
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)  # a directory given with --socket isn't chmod-ed: refused
    with pytest.raises(PermissionError, match="other accounts can write"):
        daemon.serve(Rig().ctrl, shared / "api.sock")
    assert stat.S_IMODE(os.stat(shared).st_mode) == 0o777
    assert not (mine / "api.sock").exists() and not (shared / "api.sock").exists()


def test_another_accounts_socket_at_the_path_is_refused_and_kept(tmp_path, monkeypatch):
    path = tmp_path / "api.sock"
    left = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    left.bind(str(path))  # stale (nobody listening), but not ours to remove
    left.close()
    real_uid = os.getuid()
    with monkeypatch.context() as m:
        m.setattr(os, "getuid", lambda: real_uid + 1)
        with pytest.raises(FileExistsError, match="something other than a Louver socket"):
            daemon._clear_stale(path)
    assert path.is_socket()
    daemon._clear_stale(path)  # control: as its owner, it's stale and goes
    assert not path.exists()


def test_stop_leaves_a_newer_file_at_the_path(tmp_path_factory):
    _, sock, httpd = _serve(tmp_path_factory)
    os.unlink(sock)
    sock.write_text("someone else's now\n")
    httpd.stop()
    assert sock.read_text() == "someone else's now\n"


# ---------------- who may connect ----------------
def test_connection_from_another_uid_is_closed_unanswered(tmp_path_factory):
    r = Rig()
    sock = tmp_path_factory.mktemp("api") / "api.sock"
    httpd = daemon._Server(sock, daemon.make_handler(r.ctrl), uid=os.getuid() + 1)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert _raw(sock, b"GET /api/state HTTP/1.0\r\n\r\n") == b""
    finally:
        httpd.stop()


def test_peer_uid_is_unsigned():
    class Sock:
        def getsockopt(self, level, opt, size):
            assert (level, opt, size) == (socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            return struct.pack("iII", 4242, 2**31 + 5, 2**32 - 2)  # uids past 2^31 exist (systemd-homed, LDAP)

    assert daemon._peer_uid(Sock()) == 2**31 + 5


def test_peer_uid_reads_so_peercred():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert daemon._peer_uid(a) == os.getuid()
    finally:
        a.close()
        b.close()


# ---------------- what a request may be ----------------
@pytest.mark.parametrize(
    ("length", "status"),
    [(None, 411), ("abc", 411), ("", 411), ("-1", 413), (str(apiclient.MAX_BODY + 1), 413), ("1" + "0" * 30, 413)],
)
def test_put_content_length_rules(server, length, status):  # noqa: F811
    r, sock = server
    rev = r.ctrl.state()["config_rev"]
    head = b"PUT /api/config HTTP/1.0\r\n" + (b"" if length is None else f"Content-Length: {length}\r\n".encode())
    reply = _raw(sock, head + b"\r\n")
    assert _status(reply) == status
    assert r.ctrl.state()["config_rev"] == rev


def test_put_body_of_exactly_the_cap_is_read_whole(server):  # noqa: F811
    r, sock = server
    cfg = json.loads(json.dumps(r.ctrl.cfg))
    cfg["hub"]["groups"][0]["mode"] = "full"
    body = json.dumps(cfg).encode()
    body += b" " * (apiclient.MAX_BODY - len(body))  # trailing whitespace is still valid JSON
    assert len(body) == apiclient.MAX_BODY
    head = f"PUT /api/config HTTP/1.0\r\nContent-Length: {len(body)}\r\n\r\n".encode()
    reply = _raw(sock, head + body)
    assert _status(reply) == 200 and r.ctrl.cfg["hub"]["groups"][0]["mode"] == "full"


def test_idle_and_stalled_connections_are_closed(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(daemon, "REQUEST_TIMEOUT", 0.2)
    _, sock, httpd = _serve(tmp_path_factory)
    try:
        t0 = time.monotonic()
        assert _raw(sock, b"") == b""  # says nothing: dropped
        stalled = _raw(sock, b"PUT /api/config HTTP/1.0\r\nContent-Length: 10\r\n\r\n{}")  # body never finishes
        assert stalled == b"" and time.monotonic() - t0 < 3
    finally:
        httpd.stop()


def test_a_trickling_request_is_cut_off_at_the_deadline(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(daemon, "REQUEST_TIMEOUT", 0.5)  # never silent this long...
    monkeypatch.setattr(daemon, "REQUEST_DEADLINE", 1.0)  # ...but this is all it gets
    _, sock, httpd = _serve(tmp_path_factory)
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(str(sock))
    t0 = time.monotonic()
    try:
        for b in b"GET /api/state HTTP/1.0\r\nX-Pad: " + b"a" * 100:
            try:
                c.send(bytes([b]))
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(0.05)
        c.settimeout(2)
        try:
            got = c.recv(65536)
        except ConnectionResetError:
            got = b""
        assert got == b"" and time.monotonic() - t0 < 3  # closed, unanswered, once the deadline passed
        deadline = time.monotonic() + 2
        while httpd._slots._value < httpd._max and time.monotonic() < deadline:
            time.sleep(0.01)
        assert httpd._slots._value == httpd._max  # its slot is free again
    finally:
        c.close()
        httpd.stop()


def test_a_reply_isnt_held_to_the_deadline(tmp_path_factory, monkeypatch):
    # The deadline covers receiving the request; sending the reply gets the
    # idle timeout back. A reply bigger than the socket buffer, read only
    # after the deadline has passed, must still arrive whole.
    monkeypatch.setattr(daemon, "REQUEST_DEADLINE", 0.3)
    r, sock, httpd = _serve(tmp_path_factory)
    big = "x" * (4 << 20)
    r.ctrl.state = lambda: {"pad": big}
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(10)
    c.connect(str(sock))
    try:
        c.sendall(b"GET /api/state HTTP/1.0\r\n\r\n")
        time.sleep(1.0)  # the daemon is blocked writing, well past the deadline
        reply = b""
        while chunk := c.recv(1 << 16):
            reply += chunk
        head, _, body = reply.partition(b"\r\n\r\n")
        assert _status(head) == 200 and len(body) == len(json.dumps({"pad": big}))
        assert json.loads(body)["pad"] == big
    finally:
        c.close()
        httpd.stop()


def test_stop_waits_for_a_request_being_answered(tmp_path_factory):
    r, sock, httpd = _serve(tmp_path_factory)
    inside, done = threading.Event(), []
    real = r.ctrl.apply_config

    def slow_apply(cfg):
        inside.set()
        time.sleep(0.5)
        out = real(cfg)
        done.append(time.monotonic())
        return out

    r.ctrl.apply_config = slow_apply
    replies = []
    t = threading.Thread(target=lambda: replies.append(_req(sock, "/api/config", body=r.ctrl.cfg)[0]))
    t.start()
    assert inside.wait(5)
    httpd.stop()
    stopped = time.monotonic()
    assert done and done[0] <= stopped  # it finished before stop() returned (and before ctrl.shutdown())
    t.join(5)
    assert replies == [200]


def test_a_request_that_outlasts_stop_is_refused_after_shutdown(tmp_path, tmp_path_factory, monkeypatch):
    # stop() gives up waiting; the daemon hands the hardware back; then the
    # rest of a PUT arrives. It must not take the header (or the hub) back.
    monkeypatch.setattr(daemon, "STOP_WAIT", 0.2)
    d = fake_chip(tmp_path)
    r = Rig(mb_cfg({"index": 2, "mode": "manual", "manual_percent": 60}))
    r.ctrl.mb = daemon.MotherboardFans(tmp_path)
    r.ctrl.tick()
    assert read(d, "pwm2_enable") == 1 and r.opens == 1
    sock = tmp_path_factory.mktemp("api") / "api.sock"
    httpd = daemon.serve(r.ctrl, sock)
    cfg = json.loads(json.dumps(r.ctrl.cfg))
    cfg["motherboard"]["headers"][0]["manual_percent"] = 80
    body = json.dumps(cfg).encode()
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(5)
    c.connect(str(sock))
    try:
        c.sendall(f"PUT /api/config HTTP/1.0\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body[:10])
        time.sleep(0.2)  # the handler is reading the body
        httpd.stop()  # times out: the request is still coming in
        r.ctrl.shutdown()
        assert read(d, "pwm2_enable") == 5 and r.ctrl.hub is None  # handed back
        c.sendall(body[10:])
        reply = b""
        while chunk := c.recv(65536):
            reply += chunk
    finally:
        c.close()
    assert _status(reply) == 400 and b"Louver is stopping" in reply
    r.now += 10
    r.ctrl.tick()  # neither does the control loop, should it tick once more
    assert read(d, "pwm2_enable") == 5 and r.opens == 1 and r.ctrl.hub is None
    assert r.ctrl.cfg["motherboard"]["headers"][0]["manual_percent"] == 60
    for refused in (
        lambda: r.ctrl.apply_config(cfg),
        lambda: r.ctrl.update_config(lambda c: c),
        lambda: r.ctrl.lab_show({"group": 0}),
        r.ctrl.lab_end,
    ):
        with pytest.raises(cfgmod.ConfigError, match="Louver is stopping"):
            refused()


def test_daemon_stops_the_server_before_handing_the_hardware_back(tmp_path, monkeypatch):
    order = []

    class Ctrl:
        def __init__(self, *a, **kw):
            pass

        def run_forever(self, stop):
            order.append("ran")

        def shutdown(self):
            order.append("ctrl.shutdown")

    class Httpd:
        def stop(self):
            order.append("httpd.stop")

    monkeypatch.setattr(daemon, "Controller", Ctrl)
    monkeypatch.setattr(daemon, "serve", lambda ctrl, sock: Httpd())
    args = argparse.Namespace(demo=True, socket=tmp_path / "a.sock")
    assert cli.cmd_daemon(args, cfgmod.validate({}), tmp_path / "config.json") == 0
    assert order == ["ran", "httpd.stop", "ctrl.shutdown"]


def test_connection_cap_turns_the_extra_one_away_and_frees_slots(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(daemon, "MAX_CONNECTIONS", 2)
    _, sock, httpd = _serve(tmp_path_factory)
    held = []
    try:
        for _ in range(2):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock))
            held.append(c)
        deadline = time.monotonic() + 5
        while httpd._slots._value and time.monotonic() < deadline:  # both accepted and being served
            time.sleep(0.01)
        assert httpd._slots._value == 0
        assert _raw(sock, b"GET /api/state HTTP/1.0\r\n\r\n") == b""  # over the cap: closed at once
        for c in held:
            c.close()
        while httpd._slots._value < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _req(sock, "/api/state")[0] == 200
    finally:
        for c in held:
            c.close()
        httpd.stop()


def test_csp_only_on_the_html(server):  # noqa: F811
    _, sock = server
    for path, csp in (
        ("/", True),
        ("/index.html", True),
        ("/app.js", False),
        ("/style.css", False),
        ("/api/state", False),
    ):
        status, _, headers, _ = apiclient.request("GET", path, sock_path=sock)
        assert status == 200 and dict(headers).get("Content-Security-Policy") == (daemon._CSP if csp else None), path


def test_meta_lists_the_pump_modes(server):  # noqa: F811
    _, sock = server
    assert _req(sock, "/api/meta")[1]["pump_modes"] == ["bios", "full"]


# ---------------- config ----------------
def test_old_config_with_listen_loads_without_it(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"listen": "127.0.0.1:47321", "poll_interval": 2}\n')
    cfg = cfgmod.load(p)
    assert "listen" not in cfg and cfg["poll_interval"] == 2
    assert "listen" not in cfgmod.DEFAULTS


def test_old_config_with_ram_rgb_loads_without_it(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"ram_rgb": {"enabled": true, "lighting": {"mode": "Static", "colors": ["#ff0000"]}}}\n')
    cfg = cfgmod.load(p)
    assert "ram_rgb" not in cfg and "ram_rgb" not in cfgmod.DEFAULTS


# ---------------- the client ----------------
def test_call_gets_and_puts_json(server):  # noqa: F811
    r, sock = server
    assert apiclient.call("/api/state", sock_path=sock)["hub"]["connected"]
    cfg = apiclient.call("/api/config", sock_path=sock)
    cfg["hub"]["groups"][2]["mode"] = "high"
    assert apiclient.call("/api/config", cfg, sock_path=sock)["hub"]["groups"][2]["mode"] == "high"
    assert r.ctrl.cfg["hub"]["groups"][2]["mode"] == "high"


def test_call_raises_the_daemons_error(server):  # noqa: F811
    _, sock = server
    with pytest.raises(apiclient.ApiError, match="unknown theme") as e:
        apiclient.call("/api/theme", {"name": "Nope"}, sock_path=sock)
    assert e.value.status == 400
    with pytest.raises(apiclient.ApiError, match="not found") as e:
        apiclient.call("/api/nothing", sock_path=sock)
    assert e.value.status == 404


def test_not_running_is_said_plainly(tmp_path):
    with pytest.raises(apiclient.NotRunning, match="Louver isn't running: systemctl --user start louver"):
        apiclient.call("/api/state", sock_path=tmp_path / "none.sock")  # no file
    stale = tmp_path / "stale.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(stale))  # a file, but nobody listening
    s.close()
    with pytest.raises(apiclient.NotRunning):
        apiclient.call("/api/state", sock_path=stale)


def _fake_daemon(path, answer: bytes | None):
    """A listener that sends ``answer`` (or nothing, ever) to its one client."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()

    def run():
        c, _ = listener.accept()
        c.recv(65536)
        if answer is not None:
            c.sendall(answer)
            c.close()
        else:
            time.sleep(2)
            c.close()

    threading.Thread(target=run, daemon=True).start()
    return listener


def test_a_reply_that_isnt_http_is_a_connection_error(tmp_path):
    listener = _fake_daemon(tmp_path / "s", b"hello\r\n")
    try:
        with pytest.raises(ConnectionError, match="bad reply from Louver") as e:
            apiclient.request("GET", "/api/state", sock_path=tmp_path / "s")
        assert not isinstance(e.value, apiclient.NotRunning)
    finally:
        listener.close()


# ---------------- proxy(): the app's louver://app/ ----------------
def test_proxy_serves_the_page_with_its_headers(server):  # noqa: F811
    _, sock = server
    rep = apiclient.proxy("GET", "louver://app/#hub-2", None, None, sock_path=sock)  # the hash isn't sent
    assert rep.status == 200 and rep.content_type == "text/html; charset=utf-8" and b"Louver" in rep.body
    h = dict(rep.headers)
    assert h["Content-Security-Policy"] == daemon._CSP and h["Cache-Control"] == "no-store"
    assert h["X-Content-Type-Options"] == "nosniff" and "Content-Length" not in h and "Server" not in h
    js = apiclient.proxy("GET", "louver://app/app.js", None, None, sock_path=sock)
    assert js.content_type == "text/javascript" and "Content-Security-Policy" not in dict(js.headers)


@pytest.mark.parametrize(
    ("path", "ctype"),
    [
        ("/", "text/html; charset=utf-8"),
        ("/app.js", "text/javascript"),
        ("/model.js", "text/javascript"),
        ("/style.css", "text/css"),
        ("/fonts/unbounded.woff2", "font/woff2"),
        ("/api/state", "application/json"),
    ],
)
def test_proxy_forwards_the_content_type_among_the_headers(server, path, ctype):  # noqa: F811
    # The window's set_http_headers() replaces the type set_content_type() gave;
    # with nosniff, a script or stylesheet that arrives without one is refused.
    _, sock = server
    rep = apiclient.proxy("GET", "louver://app" + path, None, None, sock_path=sock)
    assert rep.content_type == ctype and [v for k, v in rep.headers if k.lower() == "content-type"] == [ctype]


def test_proxy_passes_method_body_and_query(server):  # noqa: F811
    r, sock = server
    assert apiclient.proxy("GET", "louver://app/api/state?x=1", None, None, sock_path=sock).status == 200
    body = json.dumps({"zone": "hub-3", "mode": "quiet"}).encode()
    rep = apiclient.proxy("PUT", "louver://app/api/zone-mode", body, "application/json", sock_path=sock)
    assert rep.status == 200 and rep.content_type == "application/json"
    assert json.loads(rep.body)["hub"]["groups"][3]["mode"] == "quiet" == r.ctrl.cfg["hub"]["groups"][3]["mode"]
    bad = apiclient.proxy("PUT", "louver://app/api/zone-mode", b'{"zone": "hub-9"}', None, sock_path=sock)
    assert bad.status == 400 and "unknown zone" in json.loads(bad.body)["error"]


def test_proxy_refuses_a_body_over_the_cap_without_asking(tmp_path):
    big = b" " * (apiclient.MAX_BODY + 1)
    rep = apiclient.proxy("PUT", "louver://app/api/config", big, None, sock_path=tmp_path / "none.sock")
    assert rep.status == 413 and "body must be" in json.loads(rep.body)["error"]  # 413, not 503: never sent
    with pytest.raises(apiclient.ProxyError):
        apiclient.proxy("PUT", "louver://app/", big, None, sock_path=tmp_path / "none.sock")


def test_proxy_when_the_daemon_is_down(tmp_path):
    rep = apiclient.proxy("GET", "louver://app/api/state", None, None, sock_path=tmp_path / "none.sock")
    assert rep.status == 503 and rep.content_type == "application/json"
    assert ("Content-Type", "application/json") in rep.headers
    assert json.loads(rep.body) == {"error": apiclient.NOT_RUNNING}
    with pytest.raises(apiclient.ProxyError, match="isn't running"):  # the window shows its retry page
        apiclient.proxy("GET", "louver://app/", None, None, sock_path=tmp_path / "none.sock")


def test_proxy_when_the_daemon_doesnt_answer(tmp_path):
    listener = _fake_daemon(tmp_path / "s", None)
    try:
        rep = apiclient.proxy("GET", "louver://app/api/state", None, None, sock_path=tmp_path / "s", timeout=0.2)
        assert rep.status == 503 and "didn't answer" in json.loads(rep.body)["error"]
    finally:
        listener.close()


@pytest.mark.parametrize(
    "uri", ["louver://evil/", "louver://app@evil/", "louver://app:1/", "http://app/", "louver:///"]
)
def test_proxy_serves_only_louver_app(uri, tmp_path):
    with pytest.raises(apiclient.ProxyError, match="not a Louver page"):
        apiclient.proxy("GET", uri, None, None, sock_path=tmp_path / "none.sock")


@pytest.mark.parametrize(
    ("uri", "same"),
    [
        ("louver://app/", True),
        ("louver://app/#aura", True),
        ("louver://APP/api/state?x=1", True),
        ("louver://app:8080/", False),
        ("louver://app@evil/", False),
        ("louver://user:pw@app/", False),
        ("louver://application/", False),
        ("louver://app.evil/", False),
        ("http://app/", False),
        ("about:blank", False),
        ("louver://app:x/", False),
    ],
)
def test_same_origin(uri, same):
    assert apiclient.same_origin("louver://app", uri) is same


@pytest.mark.parametrize("uri", ["http://[x/", "louver://[::1/", "louver://app:99999999/"])
def test_same_origin_is_false_for_a_malformed_uri(uri):
    assert apiclient.same_origin("louver://app", uri) is False  # never raises: the window would fail open
    assert apiclient.window_may_open(uri) is False


# ---------------- the CLI ----------------
def test_status_and_set_over_the_socket(server, tmp_path, monkeypatch, capsys):  # noqa: F811
    r, sock = server
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cli.main(["status", "--socket", str(sock)]) == 0
    assert "connected" in capsys.readouterr().out
    assert cli.main(["set", "--socket", str(sock), "2", "60"]) == 0
    g = r.ctrl.cfg["hub"]["groups"][1]
    assert g["mode"] == "manual" and g["manual_percent"] == 60


def test_status_and_set_say_when_louver_isnt_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cli.main(["status"]) == 1  # the default socket, under conftest's runtime dir
    assert cli.main(["set", "--socket", str(tmp_path / "none.sock"), "2", "60"]) == 1
    err = capsys.readouterr().err
    assert "louver status: Louver isn't running" in err and "louver set: Louver isn't running" in err


def test_daemon_serves_the_socket_it_was_given(tmp_path, monkeypatch, capsys):
    got = []

    def refuse(ctrl, sock):
        got.append(sock)
        raise PermissionError(errno.EPERM, "other accounts can write in this directory", str(tmp_path))

    monkeypatch.setattr(daemon, "serve", refuse)
    args = argparse.Namespace(demo=True, socket=tmp_path / "demo.sock")
    assert cli.cmd_daemon(args, cfgmod.validate({}), tmp_path / "config.json") == 1
    assert got == [tmp_path / "demo.sock"]
    assert f"cannot serve on {tmp_path / 'demo.sock'}: other accounts can write in this directory ({tmp_path})" in (
        capsys.readouterr().err
    )

    def in_use(ctrl, sock):
        got.append(sock)
        raise OSError(errno.EADDRINUSE, "another Louver is serving this socket", str(sock))

    monkeypatch.setattr(daemon, "serve", in_use)
    args = argparse.Namespace(demo=True, socket=None)
    assert cli.cmd_daemon(args, cfgmod.validate({}), tmp_path / "config.json") == cli.ALREADY_RUNNING
    assert got[-1] == apiclient.socket_path()


def test_ui_without_the_desktop_app_says_so_and_starts_nothing(monkeypatch, capsys):
    launched = []
    monkeypatch.setitem(sys.modules, "gi", None)  # import gi -> ImportError
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **kw: launched.append(a))
    assert cli.cmd_ui(None, {}, None) == 1
    assert launched == [] and "python3-gi" in capsys.readouterr().err


# ---------------- the socket path's length ----------------
def _path_of(base, n: int):
    """A path under ``base`` exactly ``n`` bytes long."""
    pad = n - len(os.fsencode(base)) - len("/") - len(".sock")
    assert pad > 0
    return base / ("s" * pad + ".sock")


def test_socket_path_at_the_limit_works_and_one_over_is_explained(tmp_path_factory):
    base = tmp_path_factory.mktemp("len")
    ok = _path_of(base / "d", apiclient.SUN_PATH_MAX)
    httpd = daemon.serve(Rig().ctrl, ok)
    try:
        assert _req(ok, "/api/state")[0] == 200
    finally:
        httpd.stop()
    long = _path_of(base / "e", apiclient.SUN_PATH_MAX + 1)
    with pytest.raises(OSError, match="at most 107 bytes; give a shorter one with --socket") as e:
        daemon.serve(Rig().ctrl, long)
    assert e.value.errno == errno.ENAMETOOLONG and not long.parent.exists()  # refused before making anything
    with pytest.raises(OSError, match="at most 107 bytes") as e:
        apiclient.call("/api/state", sock_path=long)
    assert not isinstance(e.value, apiclient.NotRunning)


def test_default_socket_under_a_long_home_says_what_to_do(tmp_path, monkeypatch, capsys):
    home = tmp_path / ("h" * 100)
    monkeypatch.delenv("XDG_RUNTIME_DIR")  # the ~/.cache/louver fallback
    monkeypatch.setenv("HOME", str(home))
    assert len(os.fsencode(apiclient.socket_path())) > apiclient.SUN_PATH_MAX
    args = argparse.Namespace(demo=True, socket=None)
    assert cli.cmd_daemon(args, cfgmod.validate({}), tmp_path / "config.json") == 1
    err = capsys.readouterr().err
    assert f"cannot serve on {apiclient.socket_path()}: a Unix socket's path can be at most 107 bytes" in err
    assert "--socket" in err and not (home / ".cache" / "louver").exists()


# ---------------- where the app window may go ----------------
@pytest.mark.parametrize(
    ("uri", "ok"),
    [
        ("louver://app/", True),
        ("louver://app/#hub-2", True),
        ("about:blank", True),  # the "isn't running" page (load_html with no base)
        ("about:blank#x", False),
        ("about:srcdoc", False),
        ("louver://app@evil/", False),
        ("https://example.com/", False),
        ("file:///etc/passwd", False),
        ("data:text/html,hi", False),
    ],
)
def test_window_may_open(uri, ok):
    assert apiclient.window_may_open(uri) is ok


# ---------------- the window's request bodies ----------------
@pytest.fixture
def gui():
    pytest.importorskip("gi")
    try:
        from louver import gui
    except (ImportError, ValueError) as e:  # gi, but not GTK3 / WebKit2GTK 4.1
        pytest.skip(f"no desktop app libraries: {e}")
    return gui


@pytest.mark.parametrize(
    ("size", "want"),
    [
        (apiclient.MAX_BODY + 10, apiclient.MAX_BODY + 1),
        (apiclient.MAX_BODY, apiclient.MAX_BODY),
        (0, 0),
        (70000, 70000),
    ],
)
def test_read_body_stops_just_past_the_cap(gui, size, want):
    from gi.repository import Gio, GLib

    data = bytes(i & 0xFF for i in range(size))
    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
    got = gui._read_body(stream)
    assert got == data[:want] and stream.is_closed()


def test_read_body_of_none_is_none(gui):
    assert gui._read_body(None) is None


class _Request:
    """What _serve_scheme and _finish use of a WebKit2.URISchemeRequest."""

    def __init__(self, method="GET", uri="louver://app/", body=None, ctype=None):
        from gi.repository import Gio, GLib, Soup

        self.method, self.uri = method, uri
        self.headers = Soup.MessageHeaders.new(Soup.MessageHeadersType.REQUEST)
        if ctype:
            self.headers.append("Content-Type", ctype)
        self.body = None if body is None else Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(body))
        self.response = self.error = None

    def get_http_method(self):
        return self.method

    def get_uri(self):
        return self.uri

    def get_http_headers(self):
        return self.headers

    def get_http_body(self):
        return self.body

    def finish_with_response(self, resp):
        self.response = resp

    def finish_error(self, err):
        self.error = err


class _Response:
    """A real URISchemeResponse, with what was set on it kept for the test."""

    def __init__(self, real, data: bytes, length: int):
        self.real, self.data, self.length = real, data, length
        self.status = self.ctype = self.headers = None

    def set_status(self, code, reason):
        self.status = (code, reason)
        self.real.set_status(code, reason)

    def set_content_type(self, ctype):
        self.ctype = ctype
        self.real.set_content_type(ctype)

    def set_http_headers(self, headers):
        self.headers = headers
        self.real.set_http_headers(headers)


@pytest.fixture
def app(gui, monkeypatch, tmp_path):
    """A LouverApp with no Gtk.Application set up: no window, nothing registered."""
    from gi.repository import Gio

    real_new = gui.WebKit2.URISchemeResponse.new

    def new(stream, length):
        data = stream.read_bytes(length + 1, None).get_data()  # what the window would be sent
        return _Response(real_new(Gio.MemoryInputStream.new_from_bytes(gui.GLib.Bytes.new(data)), length), data, length)

    monkeypatch.setattr(gui.WebKit2.URISchemeResponse, "new", new)
    a = gui.LouverApp.__new__(gui.LouverApp)
    a._requests, a.sock = set(), tmp_path / "api.sock"
    return a


def test_finish_answers_with_status_type_headers_and_body(app):
    req = _Request()
    app._requests.add(req)
    reply = apiclient.Reply(
        201,
        "Created",
        "text/css",
        [("Content-Type", "text/css"), ("Content-Security-Policy", "default-src 'self'")],
        b"a{}",
    )
    assert app._finish(req, reply) is False  # an idle callback: once
    resp = req.response
    assert resp.status == (201, "Created") and resp.ctype == "text/css" and (resp.data, resp.length) == (b"a{}", 3)
    assert resp.headers.get_one("Content-Type") == "text/css"  # kept: set_http_headers replaces set_content_type's
    assert resp.headers.get_one("Content-Security-Policy") == "default-src 'self'"
    assert req.error is None and app._requests == set()


def test_finish_fails_the_load_for_an_error(app):
    req = _Request()
    app._requests.add(req)
    app._finish(req, apiclient.ProxyError("Louver isn't running: systemctl --user start louver"))
    assert req.response is None and req.error.message == "Louver isn't running: systemctl --user start louver"
    assert app._requests == set()


@pytest.fixture
def inline(gui, monkeypatch):
    """_serve_scheme's worker thread and idle callback, run on the spot."""

    class Thread:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(gui, "threading", types.SimpleNamespace(Thread=Thread))
    monkeypatch.setattr(gui.GLib, "idle_add", lambda fn, *a: fn(*a))


def test_serve_scheme_passes_the_request_to_proxy(app, inline, monkeypatch):
    seen = []

    def proxy(method, uri, body, ctype, *, sock_path):
        seen.append((method, uri, body, ctype, sock_path))
        return apiclient.Reply(200, "OK", "application/json", [("Content-Type", "application/json")], b"{}")

    monkeypatch.setattr(apiclient, "proxy", proxy)
    req = _Request("PUT", "louver://app/api/config", b'{"a": 1}', "application/json")
    app._serve_scheme(req)
    assert seen == [("PUT", "louver://app/api/config", b'{"a": 1}', "application/json", app.sock)]
    assert req.response.status == (200, "OK") and req.response.data == b"{}" and app._requests == set()
    get = _Request()  # a page load: no body, no type
    app._serve_scheme(get)
    assert seen[-1] == ("GET", "louver://app/", None, None, app.sock) and get.response is not None


def test_serve_scheme_finishes_with_an_error_whatever_proxy_raises(app, inline, monkeypatch):
    def proxy(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(apiclient, "proxy", proxy)
    req = _Request()
    app._serve_scheme(req)
    assert req.response is None and req.error.message == "boom" and app._requests == set()


def test_serve_scheme_fails_a_request_whose_body_cant_be_read(app, inline, monkeypatch):
    called = []
    monkeypatch.setattr(apiclient, "proxy", lambda *a, **kw: called.append(a))
    req = _Request("PUT", "louver://app/api/config", b"x", "application/json")
    req.body.close(None)  # reading it now raises GLib.Error
    app._serve_scheme(req)
    assert req.error.message == "can't read the request: Stream is already closed"
    assert req.response is None and called == [] and app._requests == set()


def test_register_scheme_binds_louver_secure_and_cors_enabled(app):
    class Security:
        def __init__(self):
            self.calls = []

        def register_uri_scheme_as_secure(self, scheme):
            self.calls.append(("secure", scheme))

        def register_uri_scheme_as_cors_enabled(self, scheme):
            self.calls.append(("cors_enabled", scheme))  # the page's fetch() of louver://app/api/... needs it

    class Context:
        def __init__(self):
            self.schemes, self.security = [], Security()

        def register_uri_scheme(self, scheme, callback):
            self.schemes.append((scheme, callback))

        def get_security_manager(self):
            return self.security

    ctx = Context()
    app._register_scheme(ctx)
    assert ctx.schemes == [("louver", app._serve_scheme)]
    assert sorted(ctx.security.calls) == [("cors_enabled", "louver"), ("secure", "louver")]
