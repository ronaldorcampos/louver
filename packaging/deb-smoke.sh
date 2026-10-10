#!/usr/bin/env bash
# Takes a built .deb through install, upgrade, remove, reinstall and purge
# with apt, the way a user would, running the installed copy along the way.
# For CI or a throwaway container: it changes the system.
set -euo pipefail
deb="$(realpath "$1")"
sudo=; [ "$(id -u)" = 0 ] || sudo=sudo
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
pkg=/usr/lib/python3/dist-packages/louver

status() { dpkg-query -W -f='${db:Status-Abbrev}${Version}' louver 2>/dev/null || true; }
expect() {  # expect WHAT GOT WANT
  if [ "$2" != "$3" ]; then
    echo "FAIL $1: got '$2', want '$3'" >&2
    exit 1
  fi
  echo "ok   $1"
}
wants=/etc/systemd/system/sockets.target.wants/louver-ram.socket
mask=/etc/systemd/system/louver-ram.socket
ram_socket() {  # ram_socket WHEN: enabled (and listening, with systemd running)
  if command -v deb-systemd-helper >/dev/null 2>&1; then
    expect "RAM socket enabled $1" "$(test -L $wants && echo yes || echo no)" "yes"
    expect "RAM socket not masked $1" "$(test -L $mask && echo masked || echo no)" "no"
  fi
  if [ -d /run/systemd/system ]; then
    expect "RAM socket listening $1" "$(systemctl is-active louver-ram.socket || true)" "active"
  fi
}
lock_dir() {  # lock_dir WHEN: the machine-wide hardware lock's directory, made by postinst
  expect "lock dir conf installed $1" "$(grep -v '^#' /usr/lib/tmpfiles.d/louver.conf)" "d /run/louver 3770 root plugdev -"
  # postinst runs systemd-tmpfiles only with systemd running (else it's made at boot)
  if [ -d /run/systemd/system ] && command -v systemd-tmpfiles >/dev/null 2>&1; then
    expect "lock dir made $1" "$(stat -c '%a %U %G' /run/louver)" "3770 root plugdev"
  fi
}
install_deb() { $sudo apt-get install -y -qq --allow-downgrades "$1" >"$work/apt.log" 2>&1 || { cat "$work/apt.log"; exit 1; }; }

run_installed() {  # the installed copy runs, from well away from any checkout
  (
    cd /
    louver --help >/dev/null
    python3 -I -c "import louver, sys; sys.exit(not louver.__file__.startswith('$pkg/'))"
    louver daemon --demo --socket "$work/api.sock" &
    pid=$!
    trap 'kill $pid 2>/dev/null; wait $pid 2>/dev/null || true' EXIT  # stop it before the next run
    python3 - "$work/api.sock" <<'PY'
import http.client, json, socket, sys, time

class Unix(http.client.HTTPConnection):  # the API is HTTP on a Unix socket
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(3)
        self.sock.connect(sys.argv[1])

def get(path):
    c = Unix("localhost")
    c.request("GET", path)
    r = c.getresponse()
    return r.status, r.getheader("Content-Type"), r.read()

for _ in range(50):
    try:
        status, _, body = get("/api/state")
        break
    except OSError:
        time.sleep(0.2)
else:
    raise SystemExit("demo daemon never answered")
state = json.loads(body)
assert status == 200 and state["hub"]["connected"], state
status, ctype, font = get("/fonts/unbounded.woff2")
assert status == 200 and ctype == "font/woff2" and font[:4] == b"wOF2", (status, ctype)
PY
  )
  echo "ok   installed copy runs"
}

version="$(dpkg-deb -f "$deb" Version)"
$sudo apt-get update -qq

install_deb "$deb"
expect "install" "$(status)" "ii $version"
expect "RAM helper units installed" "$(ls /usr/lib/systemd/system/louver-ram.socket /usr/lib/systemd/system/louver-ram.service | wc -l)" "2"
ram_socket "after install"
lock_dir "after install"
dpkg -s python3-gi gir1.2-webkit2-4.1 gir1.2-ayatanaappindicator3-0.1 >/dev/null  # Recommends resolved
expect "not enabled for every account" "$(ls /etc/systemd/user/default.target.wants/ 2>/dev/null | grep -c louver || true)" "0"
expect "postinst says how to start it" "$(grep -c 'systemctl --user enable --now louver' "$work/apt.log" || true)" "1"
run_installed

# Upgrade: the same package one version up.
dpkg-deb -R "$deb" "$work/next"
sed -i "s/^Version: .*/Version: $version.1/" "$work/next/DEBIAN/control"
dpkg-deb --root-owner-group -b "$work/next" "$work/next.deb" >/dev/null
install_deb "$work/next.deb"
expect "upgrade" "$(status)" "ii $version.1"
ram_socket "after upgrade"
lock_dir "after upgrade"
run_installed

$sudo apt-get remove -y -qq louver >/dev/null
expect "remove" "$(status)" "rc $version.1"
expect "remove takes the code" "$(test -e $pkg && echo present || echo gone)" "gone"
if command -v deb-systemd-helper >/dev/null 2>&1; then
  expect "remove masks the RAM socket" "$(readlink $mask || true)" "/dev/null"
fi

install_deb "$deb"  # reinstall after remove (dpkg calls it an upgrade from the rc state)
expect "reinstall" "$(status)" "ii $version"
ram_socket "after reinstall"
lock_dir "after reinstall"
run_installed

$sudo apt-get purge -y -qq louver >/dev/null
expect "purge" "$(status)" ""
expect "purge leaves nothing" "$(ls -d $pkg /usr/bin/louver /usr/lib/systemd/user/louver.service /usr/lib/systemd/system/louver-ram.socket /usr/lib/systemd/system/louver-ram.service /usr/lib/udev/rules.d/70-louver.rules /usr/lib/tmpfiles.d/louver.conf $wants $mask 2>/dev/null | wc -l)" "0"
echo "deb smoke test passed"
