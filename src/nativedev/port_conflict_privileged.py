#!/usr/bin/python3
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
from pathlib import Path


PROTOCOL_VERSION = 1
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
TARGET_PORTS = {80, 443}
SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")
SS_PID_RE = re.compile(r"\bpid=(\d+)\b")
SS_PROCESS_RE = re.compile(r'\(\(\"([^\"]+)\"')
SYSTEMCTL_UNIT_RE = re.compile(
    r"^\s*(?:[●○×]\s+)?([A-Za-z0-9_.@:-]+\.service)\b"
)


def _binary(name: str) -> str:
    for directory in SAFE_PATH.split(":"):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise RuntimeError(f"Required system command is unavailable: {name}")


def _extract_port(local_address: str) -> int | None:
    match = re.search(r":(\d+)$", local_address)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value in TARGET_PORTS else None


def _service_for_pid(pid: int) -> str:
    result = subprocess.run(
        [
            _binary("systemctl"),
            "status",
            str(pid),
            "--no-pager",
            "--full",
            "--lines=0",
        ],
        text=True,
        capture_output=True,
        timeout=15,
        env={**os.environ, "PATH": SAFE_PATH},
    )
    for raw in result.stdout.splitlines():
        match = SYSTEMCTL_UNIT_RE.match(raw)
        if match and SERVICE_RE.fullmatch(match.group(1)):
            return match.group(1)
    return ""


def _service_description(service: str) -> str:
    result = subprocess.run(
        [_binary("systemctl"), "show", service, "-p", "Description", "--value"],
        text=True,
        capture_output=True,
        timeout=15,
        env={**os.environ, "PATH": SAFE_PATH},
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _service_enabled_state(service: str) -> str:
    result = subprocess.run(
        [_binary("systemctl"), "is-enabled", service],
        text=True,
        capture_output=True,
        timeout=15,
        env={**os.environ, "PATH": SAFE_PATH},
    )
    return (result.stdout or result.stderr).strip() or "unknown"


def _service_active(service: str) -> bool:
    result = subprocess.run(
        [_binary("systemctl"), "is-active", "--quiet", service],
        text=True,
        capture_output=True,
        timeout=15,
        env={**os.environ, "PATH": SAFE_PATH},
    )
    return result.returncode == 0


def _inspect_conflicts() -> list[dict]:
    result = subprocess.run(
        [_binary("ss"), "-H", "-ltnp"],
        text=True,
        capture_output=True,
        timeout=20,
        env={**os.environ, "PATH": SAFE_PATH},
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not inspect port listeners")

    by_service: dict[str, dict] = {}
    unmanaged: dict[tuple[str, tuple[int, ...]], dict] = {}

    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 5)
        if len(parts) < 4:
            continue
        port = _extract_port(parts[3])
        if port is None:
            continue

        pids = tuple(sorted({int(value) for value in SS_PID_RE.findall(line)}))
        process_match = SS_PROCESS_RE.search(line)
        process = process_match.group(1) if process_match else ""

        service = ""
        for pid in pids:
            service = _service_for_pid(pid)
            if service:
                break

        if process == "nginx" or service == "nginx.service":
            continue

        if service:
            item = by_service.setdefault(
                service,
                {
                    "ports": set(),
                    "process": process,
                    "pids": set(),
                    "service": service,
                },
            )
            item["ports"].add(port)
            item["pids"].update(pids)
            if not item["process"] and process:
                item["process"] = process
            continue

        key = (process or "System listener", pids)
        item = unmanaged.setdefault(
            key,
            {
                "ports": set(),
                "process": process,
                "pids": set(pids),
                "service": "",
            },
        )
        item["ports"].add(port)

    conflicts: list[dict] = []
    for service, item in by_service.items():
        conflicts.append(
            {
                "ports": sorted(item["ports"]),
                "process": item["process"],
                "pids": sorted(item["pids"]),
                "service": service,
                "description": _service_description(service),
                "enabled_state": _service_enabled_state(service),
                "active": _service_active(service),
                "manageable": True,
            }
        )

    for item in unmanaged.values():
        conflicts.append(
            {
                "ports": sorted(item["ports"]),
                "process": item["process"],
                "pids": sorted(item["pids"]),
                "service": "",
                "description": "",
                "enabled_state": "",
                "active": True,
                "manageable": False,
            }
        )

    return sorted(
        conflicts,
        key=lambda item: (
            min(item["ports"]) if item["ports"] else 65535,
            (item["service"] or item["process"]).casefold(),
        ),
    )


def _disable_and_stop_ports(ports: list[int], expected_service: str = "") -> dict:
    requested = {int(port) for port in ports}
    if not requested or not requested.issubset(TARGET_PORTS):
        raise RuntimeError("Port-conflict action accepts only port 80/443")
    if expected_service and (
        not SERVICE_RE.fullmatch(expected_service)
        or expected_service == "nginx.service"
    ):
        raise RuntimeError("Expected service is outside the port-conflict operation")

    current = _inspect_conflicts()
    relevant = [
        item
        for item in current
        if requested.intersection(set(item.get("ports", [])))
    ]
    if not relevant:
        raise RuntimeError(
            "The selected port is no longer in conflict. Refresh Dashboard and try again."
        )

    unmanaged = [item for item in relevant if not item.get("service")]
    if unmanaged:
        names = sorted(
            {
                str(item.get("process") or "unknown process")
                for item in unmanaged
            }
        )
        raise RuntimeError(
            "The selected port is currently owned by a process that is not a "
            "manageable systemd service: " + ", ".join(names)
        )

    services = sorted(
        {
            str(item.get("service"))
            for item in relevant
            if item.get("service")
        },
        key=str.casefold,
    )
    if expected_service:
        if expected_service not in services:
            raise RuntimeError(
                f"{expected_service} no longer owns the selected port. "
                "Refresh Dashboard and try again."
            )
        services = [expected_service]

    if not services:
        raise RuntimeError(
            "No manageable systemd service currently owns the selected port."
        )

    completed: list[str] = []
    for service in services:
        if not SERVICE_RE.fullmatch(service) or service == "nginx.service":
            raise RuntimeError("Service is outside the port-conflict operation")
        result = subprocess.run(
            [_binary("systemctl"), "disable", "--now", service],
            text=True,
            capture_output=True,
            timeout=120,
            env={**os.environ, "PATH": SAFE_PATH},
        )
        if result.returncode != 0:
            suffix = (
                f" Services already disabled/stopped: {', '.join(completed)}."
                if completed
                else ""
            )
            raise RuntimeError(
                (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"Could not disable and stop {service}"
                )
                + suffix
            )
        completed.append(service)

    remaining = _inspect_conflicts()
    still_relevant = [
        item
        for item in remaining
        if requested.intersection(set(item.get("ports", [])))
    ]
    if still_relevant:
        owners = sorted(
            {
                str(item.get("service") or item.get("process") or "unknown listener")
                for item in still_relevant
            }
        )
        raise RuntimeError(
            "The requested service action completed, but the selected port is "
            "still occupied by: " + ", ".join(owners)
        )

    return {"services": completed}


def _parent_uid(parent_pid: int) -> int | None:
    try:
        text = Path(f"/proc/{parent_pid}/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        if raw.startswith("Uid:"):
            fields = raw.split()
            if len(fields) >= 2:
                try:
                    return int(fields[1])
                except ValueError:
                    return None
    return None


def _validate_socket_path(path: Path, uid: int, parent_pid: int) -> None:
    expected_name = f"nativedev-port-conflict-{uid}-{parent_pid}.sock"
    if path.name != expected_name:
        raise RuntimeError("Unexpected port-conflict helper socket name")

    parent = path.parent
    stat = parent.stat()
    if stat.st_uid != uid or stat.st_mode & 0o022:
        raise RuntimeError("Unsafe runtime directory for port-conflict helper")
    if path.exists() or path.is_symlink():
        raise RuntimeError("Port-conflict helper socket path already exists")


def _reply(connection: socket.socket, payload: dict) -> None:
    connection.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))


def _handle(request: dict) -> dict:
    if request.get("protocol") != PROTOCOL_VERSION:
        raise RuntimeError("Port-conflict helper protocol mismatch")

    action = request.get("action")
    if action == "ping":
        if set(request).difference({"protocol", "action"}):
            raise RuntimeError("Ping contains unsupported fields")
        return {"ok": True, "protocol": PROTOCOL_VERSION}

    if action == "inspect":
        if set(request).difference({"protocol", "action"}):
            raise RuntimeError("Inspect contains unsupported fields")
        return {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "conflicts": _inspect_conflicts(),
        }

    if action == "disable_stop_ports":
        if set(request).difference(
            {"protocol", "action", "ports", "expected_service"}
        ):
            raise RuntimeError("Disable/stop contains unsupported fields")
        ports = request.get("ports")
        expected_service = request.get("expected_service", "")
        if not isinstance(ports, list) or not isinstance(expected_service, str):
            raise RuntimeError("Invalid disable/stop request")
        if any(isinstance(port, bool) or not isinstance(port, int) for port in ports):
            raise RuntimeError("Invalid port-conflict port list")
        result = _disable_and_stop_ports(ports, expected_service)
        return {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "result": result,
        }

    if action == "shutdown":
        if set(request).difference({"protocol", "action"}):
            raise RuntimeError("Shutdown contains unsupported fields")
        return {"ok": True, "protocol": PROTOCOL_VERSION, "shutdown": True}

    raise RuntimeError("Unsupported port-conflict helper action")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    parser.add_argument("--parent-pid", required=True, type=int)
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise SystemExit("Port-conflict helper must run as root")
    if args.uid < 0 or args.gid < 0 or args.parent_pid <= 1:
        raise SystemExit("Invalid helper identity")
    if _parent_uid(args.parent_pid) != args.uid:
        raise SystemExit("Parent process does not belong to the requested user")

    socket_path = Path(args.socket)
    _validate_socket_path(socket_path, args.uid, args.parent_pid)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chown(socket_path, args.uid, args.gid)
    os.chmod(socket_path, 0o600)
    server.listen(4)
    server.settimeout(1.0)

    try:
        shutting_down = False
        while not shutting_down:
            if _parent_uid(args.parent_pid) != args.uid:
                break
            try:
                connection, _address = server.accept()
            except TimeoutError:
                continue
            except socket.timeout:
                continue

            with connection:
                connection.settimeout(130)
                try:
                    chunks: list[bytes] = []
                    while True:
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        if b"\n" in chunk:
                            break
                    if not chunks:
                        continue
                    request = json.loads(
                        b"".join(chunks).split(b"\n", 1)[0].decode("utf-8")
                    )
                    if not isinstance(request, dict):
                        raise RuntimeError("Invalid helper request")
                    response = _handle(request)
                    shutting_down = bool(response.pop("shutdown", False))
                except Exception as exc:
                    response = {
                        "ok": False,
                        "protocol": PROTOCOL_VERSION,
                        "error": str(exc),
                    }
                _reply(connection, response)
    finally:
        server.close()
        try:
            socket_path.unlink(missing_ok=True)
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
