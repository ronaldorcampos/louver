"""Talking to the daemon's API, which is HTTP/1.0 on a Unix socket.

There's no TCP port: the socket sits in a directory only your account can
open, so nothing else on the machine (other accounts, containers, a web
page in a browser) can reach the API. (root, and plugdev through the udev
rule, can still drive the fans directly, as they always could.) The CLI
and the tray ask it directly; the desktop app's window loads
``louver://app/...``, and gui.py hands each of those requests to
:func:`proxy`, which forwards it here.
"""

from __future__ import annotations

import errno
import http.client
import json
import os
import socket
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

MAX_BODY = 1 << 20  # the largest request body the daemon reads
SUN_PATH_MAX = 107  # bytes in a Unix socket's path (sockaddr_un.sun_path, less its NUL)
NOT_RUNNING = "Louver isn't running: systemctl --user start louver"
BASE = "louver://app"  # where the desktop app's window loads the UI from
# What the window gets of the daemon's reply headers.
_FORWARD = {"content-type", "cache-control", "content-security-policy", "x-content-type-options"}


def socket_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) / "louver" if runtime else Path.home() / ".cache" / "louver"


def socket_path() -> Path:
    return socket_dir() / "api.sock"


def check_length(path: Path | str) -> None:
    """Raise a clear error for a path no Unix socket can have (the default can
    get there, as ~/.cache/louver/api.sock, under a long home directory)."""
    if len(os.fsencode(path)) > SUN_PATH_MAX:
        raise OSError(
            errno.ENAMETOOLONG,
            f"a Unix socket's path can be at most {SUN_PATH_MAX} bytes; give a shorter one with --socket",
            str(path),
        )


class NotRunning(ConnectionError):
    """Nothing is serving the socket."""


class ApiError(Exception):
    """The daemon answered with an error status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)  # the Host header; the daemon ignores it
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self._path)
        except OSError:
            s.close()
            raise
        self.sock = s


def request(
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict | None = None,
    *,
    sock_path: Path | str | None = None,
    timeout: float = 3.0,
) -> tuple[int, str, list[tuple[str, str]], bytes]:
    """One request -> (status, reason, headers, body). Raises NotRunning if
    nothing listens on the socket, OSError if the exchange fails after that."""
    where = str(sock_path or socket_path())
    check_length(where)
    conn = _UnixConnection(where, timeout)
    try:
        try:
            conn.connect()
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise NotRunning(NOT_RUNNING) from e
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.reason, resp.getheaders(), resp.read()
        except http.client.HTTPException as e:  # not an HTTP reply at all
            raise ConnectionError(f"bad reply from Louver: {e!r}") from e
    finally:
        conn.close()


def call(path: str, body: dict | None = None, *, sock_path: Path | str | None = None, timeout: float = 3.0):
    """GET ``path`` (or PUT ``body`` as JSON) and return the decoded reply.
    Raises NotRunning, ApiError for an error status, OSError, ValueError."""
    data = None if body is None else json.dumps(body).encode()
    status, reason, _, raw = request(
        "GET" if body is None else "PUT",
        path,
        data,
        {"Content-Type": "application/json"},
        sock_path=sock_path,
        timeout=timeout,
    )
    if not 200 <= status < 300:
        try:
            msg = json.loads(raw)["error"]
        except (ValueError, KeyError, TypeError):
            msg = reason
        raise ApiError(status, str(msg))
    return json.loads(raw)


def same_origin(base: str, uri: str) -> bool:
    """True if ``uri`` is on ``base``'s scheme, host and port (not merely a
    string prefix of it: louver://app@evil/ isn't)."""
    try:
        a, b = urlsplit(base), urlsplit(uri)
        return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port) and b.username is None
    except ValueError:  # a malformed URI (http://[x/), a port that isn't a number
        return False


def window_may_open(uri: str) -> bool:
    """Where the app window may navigate: the UI, or about:blank, where the
    "isn't running" page goes (gui._load_failed: load_html with no base).
    That page stays off louver://app, so it has no way to the API, and the
    next load of the UI from it is a real load even if only a #hash differs."""
    return uri == "about:blank" or same_origin(BASE, uri)


class Reply(NamedTuple):
    status: int
    reason: str
    content_type: str
    # The ones in _FORWARD the daemon sent, Content-Type included: the window's
    # set_http_headers() replaces the type set_content_type() gave, and with
    # nosniff a script or stylesheet without its type is refused.
    headers: list[tuple[str, str]]
    body: bytes


class ProxyError(Exception):
    """A request the window should see fail as a load error."""


def _json_reply(status: int, reason: str, error: str) -> Reply:
    headers = [("Content-Type", "application/json"), ("Cache-Control", "no-store")]
    return Reply(status, reason, "application/json", headers, json.dumps({"error": error}).encode())


def proxy(
    method: str,
    uri: str,
    body: bytes | None,
    content_type: str | None,
    *,
    sock_path: Path | str | None = None,
    timeout: float = 5.0,
) -> Reply:
    """Answer one request the window made to ``louver://app/...`` from the
    daemon. An API call the daemon can't answer gets a JSON error (503 when
    it isn't running) that the page shows; anything else raises ProxyError,
    and the window shows its "isn't running" page."""
    if not same_origin(BASE, uri):
        raise ProxyError(f"not a Louver page: {uri}")
    parts = urlsplit(uri)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    is_api = path.startswith("/api/")
    if body is not None and len(body) > MAX_BODY:
        if is_api:
            return _json_reply(413, "Payload Too Large", f"body must be 0..{MAX_BODY} bytes")
        raise ProxyError("request body too large")
    try:
        status, reason, headers, data = request(
            method,
            path,
            body,
            {"Content-Type": content_type} if content_type else {},
            sock_path=sock_path,
            timeout=timeout,
        )
    except OSError as e:
        if not is_api:
            raise ProxyError(NOT_RUNNING if isinstance(e, NotRunning) else f"Louver didn't answer: {e}") from e
        if isinstance(e, NotRunning):
            return _json_reply(503, "Service Unavailable", NOT_RUNNING)
        return _json_reply(503, "Service Unavailable", f"Louver didn't answer: {e}")
    fwd = [(k, v) for k, v in headers if k.lower() in _FORWARD]
    ctype = next((v for k, v in fwd if k.lower() == "content-type"), None)
    if ctype is None:
        ctype = "application/octet-stream"
        fwd.append(("Content-Type", ctype))
    return Reply(status, reason, ctype, fwd, data)
