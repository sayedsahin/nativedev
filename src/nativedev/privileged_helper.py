from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import struct
import subprocess
import shutil
from pathlib import Path
from typing import Sequence

MANAGED_FILES = {
    "/etc/apt/sources.list.d/nativedev-sury-php.sources",
    "/etc/NetworkManager/conf.d/nativedev-dns.conf",
    "/etc/NetworkManager/dnsmasq.d/nativedev-test.conf",
    "/etc/nginx/sites-available/nativedev-sites.conf",
    "/etc/nginx/sites-enabled/nativedev-sites.conf",
    "/etc/nginx/nativedev/nativedev.pem",
    "/etc/nginx/nativedev/nativedev-key.pem",
}
MANAGED_DIRS = {
    "/etc/NetworkManager/conf.d",
    "/etc/NetworkManager/dnsmasq.d",
    "/etc/nginx/nativedev",
}
SERVICE_RE = re.compile(
    r"^(?:nginx|redis-server|memcached|mariadb|mysql|postgresql|NetworkManager(?:\.service)?|php\d+\.\d+-fpm)(?:\.service)?$"
)
PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+:~_-]*$")
PHP_BINARY_RE = re.compile(r"^/usr/bin/php\d+\.\d+$")
PHP_FPM_BINARY_RE = re.compile(r"^php-fpm\d+\.\d+$")
FPM_POOL_RE = re.compile(r"^/etc/php/(?P<version>\d+\.\d+)/fpm/pool\.d/nativedev-(?P<uid>\d+)\.conf$")
TEMP_SOURCE_RE = re.compile(r"^/tmp/nativedev-[^/]+/.+$")


def _safe_temp_source(value: str) -> bool:
    try:
        resolved = str(Path(value).resolve())
    except OSError:
        return False
    return bool(TEMP_SOURCE_RE.match(resolved))


def _managed_file(value: str, uid: int | None = None) -> bool:
    if value in MANAGED_FILES:
        return True
    match = FPM_POOL_RE.fullmatch(value)
    if not match:
        return False
    return uid is None or int(match.group("uid")) == uid


def validate_command(argv: Sequence[str], uid: int | None = None) -> tuple[bool, str]:
    if not argv:
        return False, "Empty command"
    cmd = Path(argv[0]).name
    args = list(argv[1:])

    if cmd == "apt-get":
        if not args:
            return False, "APT subcommand missing"
        action = args[0]
        rest = args[1:]
        if action == "update" and not rest:
            return True, ""
        if action not in {"install", "remove", "purge"}:
            return False, f"APT action not allowed: {action}"
        values = [item for item in rest if item != "-y"]
        if len(values) != len(rest) - rest.count("-y"):
            return False, "Unsupported APT option"
        if not values:
            return False, "No packages supplied"
        for value in values:
            if PACKAGE_RE.fullmatch(value):
                continue
            if value.endswith(".deb") and _safe_temp_source(value):
                continue
            return False, f"Unsafe package argument: {value}"
        return True, ""

    if cmd == "systemctl":
        if not args:
            return False, "systemctl action missing"
        action = args[0]
        rest = args[1:]
        if action in {"start", "stop", "restart", "reload", "disable"}:
            if action == "disable" and rest[:1] == ["--now"]:
                rest = rest[1:]
            if len(rest) == 1 and SERVICE_RE.fullmatch(rest[0]):
                return True, ""
        if action == "enable":
            if rest[:1] == ["--now"]:
                rest = rest[1:]
            if len(rest) == 1 and SERVICE_RE.fullmatch(rest[0]):
                return True, ""
        return False, "systemctl request is outside NativeDev's allowlist"

    if cmd == "install":
        if len(args) != 4 or args[0] != "-m" or args[1] not in {"0644", "0600"}:
            return False, "install arguments not allowed"
        source, dest = args[2], args[3]
        if not _safe_temp_source(source) or not _managed_file(dest, uid):
            return False, "install path is outside NativeDev-managed files"
        return True, ""

    if cmd == "mkdir":
        if args[:1] != ["-p"] or not args[1:]:
            return False, "mkdir arguments not allowed"
        if all(item in MANAGED_DIRS for item in args[1:]):
            return True, ""
        return False, "mkdir path is outside NativeDev-managed directories"

    if cmd == "ln":
        expected = [
            "-sfn",
            "/etc/nginx/sites-available/nativedev-sites.conf",
            "/etc/nginx/sites-enabled/nativedev-sites.conf",
        ]
        return (args == expected, "" if args == expected else "Only NativeDev's Nginx symlink may be changed")

    if cmd == "rm":
        if args[:1] != ["-f"] or not args[1:]:
            return False, "rm arguments not allowed"
        if all(_managed_file(item, uid) for item in args[1:]):
            return True, ""
        return False, "rm path is outside NativeDev-managed files"

    if cmd == "nmcli":
        allowed = [
            ["general", "reload", "conf"],
            ["general", "reload", "dns-full"],
        ]
        return (args in allowed, "" if args in allowed else "Only NativeDev DNS reload operations are allowed")

    if cmd == "nginx":
        return (args == ["-t"], "" if args == ["-t"] else "Only nginx -t is allowed")

    if PHP_FPM_BINARY_RE.fullmatch(cmd):
        return (args in [["-t"], ["-tt"]], "" if args in [["-t"], ["-tt"]] else "Only PHP-FPM config validation is allowed")

    if cmd == "update-alternatives":
        if len(args) == 3 and args[:2] == ["--set", "php"] and PHP_BINARY_RE.fullmatch(args[2]):
            return True, ""
        return False, "Only PHP default selection is allowed"

    return False, f"Privileged command is not allowed: {cmd}"



SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"


def resolved_argv(argv: Sequence[str]) -> list[str]:
    binary = shutil.which(Path(argv[0]).name, path=SAFE_PATH)
    if not binary:
        raise RuntimeError(f"Required system binary was not found: {argv[0]}")
    return [binary, *argv[1:]]

def _peer_cred(conn: socket.socket) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        return -1, -1, -1
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


def _read_request(conn: socket.socket) -> dict:
    chunks: list[bytes] = []
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if sum(map(len, chunks)) > 1024 * 1024:
            raise ValueError("Request too large")
        if b"\n" in chunk:
            break
    data = b"".join(chunks).split(b"\n", 1)[0]
    return json.loads(data.decode("utf-8"))


def _send(conn: socket.socket, payload: dict) -> None:
    conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def serve(socket_path: Path, uid: int, gid: int, parent_pid: int) -> int:
    if os.geteuid() != 0:
        return 77
    try:
        socket_path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        os.chown(socket_path, uid, gid)
        os.chmod(socket_path, 0o600)
        listener.listen(8)
        listener.settimeout(1.0)
    except OSError:
        return 78

    running = True

    def stop(*_args):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while running:
            try:
                os.kill(parent_pid, 0)
            except OSError:
                break
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    peer_pid, peer_uid, _peer_gid = _peer_cred(conn)
                    if peer_uid != uid or peer_pid != parent_pid:
                        _send(conn, {"ok": False, "error": "Peer identity rejected"})
                        continue
                    request = _read_request(conn)
                    if request.get("action") == "ping":
                        _send(conn, {"ok": True})
                        continue
                    if request.get("action") == "shutdown":
                        _send(conn, {"ok": True})
                        running = False
                        continue
                    argv = request.get("argv")
                    if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
                        _send(conn, {"ok": False, "error": "Invalid command payload"})
                        continue
                    allowed, reason = validate_command(argv, uid=uid)
                    if not allowed:
                        _send(conn, {"ok": False, "error": reason})
                        continue
                    timeout = request.get("timeout", 120)
                    try:
                        timeout = max(1, min(int(timeout), 1800))
                    except (TypeError, ValueError):
                        timeout = 120
                    proc = subprocess.run(resolved_argv(argv), text=True, capture_output=True, timeout=timeout)
                    _send(
                        conn,
                        {
                            "ok": True,
                            "returncode": proc.returncode,
                            "stdout": proc.stdout,
                            "stderr": proc.stderr,
                        },
                    )
                except subprocess.TimeoutExpired as exc:
                    _send(conn, {"ok": False, "error": f"Command timed out: {exc}"})
                except Exception as exc:  # helper boundary
                    _send(conn, {"ok": False, "error": str(exc)})
    finally:
        try:
            listener.close()
        finally:
            socket_path.unlink(missing_ok=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    return serve(Path(args.socket), args.uid, args.gid, args.parent_pid)


if __name__ == "__main__":
    raise SystemExit(main())
