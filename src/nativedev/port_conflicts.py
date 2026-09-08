from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .system import CommandRunner, SystemdManager


TARGET_PORTS = (80, 443)
PORT_CONFLICT_PROTOCOL_VERSION = 1
_SS_PID_RE = re.compile(r"\bpid=(\d+)\b")
_SS_PROCESS_RE = re.compile(r'\(\("([^"]+)"')
_SYSTEMCTL_UNIT_RE = re.compile(
    r"^\s*(?:[●○×]\s+)?([A-Za-z0-9_.@:-]+\.service)\b"
)


@dataclass(frozen=True, slots=True)
class PortConflict:
    ports: tuple[int, ...]
    process: str = ""
    pids: tuple[int, ...] = ()
    service: str = ""
    description: str = ""
    enabled_state: str = ""
    active: bool = True
    manageable: bool = True

    @property
    def service_name(self) -> str:
        if self.service.endswith(".service"):
            return self.service[:-8]
        return self.service

    @property
    def title(self) -> str:
        if self.service_name:
            return self.service_name
        if self.process:
            return self.process
        if len(self.ports) == 1:
            return f"Port {self.ports[0]} listener"
        return "Port listener"


class PortConflictPrivilegeSession:
    """Authorize exactly when the user clicks a conflict action.

    Dashboard discovery never starts this helper. One button click starts one
    pkexec-authenticated helper, performs the complete root-side resolve +
    verify + disable --now transaction, then shuts the helper down. This keeps
    one password prompt per action and no password prompt merely for opening or
    refreshing NativeDev.
    """

    def __init__(self):
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.parent_pid = os.getpid()
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{self.uid}")
        self.socket_path = runtime / (
            f"nativedev-port-conflict-{self.uid}-{self.parent_pid}.sock"
        )
        self.process: subprocess.Popen | None = None
        self.lock = threading.RLock()

    def _helper_path(self) -> Path:
        helper = Path(__file__).with_name("port_conflict_privileged.py").resolve()
        if helper.is_file():
            stat = helper.stat()
            if stat.st_uid == 0 and not (stat.st_mode & 0o022):
                return helper
            if os.environ.get("NATIVEDEV_ALLOW_SOURCE_HELPER") == "1":
                return helper
        raise RuntimeError(
            "NativeDev's port-conflict helper is not installed as a root-owned file. "
            "Install/reinstall NativeDev. Source-tree helper execution is available "
            "only with NATIVEDEV_ALLOW_SOURCE_HELPER=1."
        )

    def _request(self, payload: dict, timeout: int = 120) -> dict:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(max(5, timeout + 5))
        try:
            client.connect(str(self.socket_path))
            client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            if not chunks:
                raise RuntimeError("Port-conflict helper returned no response")
            reply = json.loads(
                b"".join(chunks).split(b"\n", 1)[0].decode("utf-8")
            )
            if not isinstance(reply, dict):
                raise RuntimeError("Port-conflict helper returned an invalid response")
            return reply
        finally:
            client.close()

    def _ping(self) -> bool:
        if not self.socket_path.exists():
            return False
        try:
            reply = self._request(
                {"action": "ping", "protocol": PORT_CONFLICT_PROTOCOL_VERSION},
                timeout=2,
            )
            return bool(
                reply.get("ok")
                and reply.get("protocol") == PORT_CONFLICT_PROTOCOL_VERSION
            )
        except Exception:
            return False

    def _wait_for_socket(self) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self._ping():
                return
            if self.process and self.process.poll() is not None:
                raise RuntimeError(
                    "System authorization was cancelled or the port-conflict "
                    "helper could not start"
                )
            time.sleep(0.1)
        raise RuntimeError("Timed out waiting for port-conflict authorization")

    def _start(self) -> None:
        if self._ping():
            return

        runtime = self.socket_path.parent
        try:
            stat = runtime.stat()
        except OSError as exc:
            raise RuntimeError(
                f"NativeDev runtime directory is unavailable: {runtime}"
            ) from exc
        if stat.st_uid != self.uid or stat.st_mode & 0o022:
            raise RuntimeError(
                f"NativeDev runtime directory is not private: {runtime}"
            )

        self.socket_path.unlink(missing_ok=True)
        pkexec = shutil.which("pkexec")
        if not pkexec:
            raise RuntimeError("pkexec is required for system service changes")

        helper = self._helper_path()
        python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
        self.process = subprocess.Popen(
            [
                pkexec,
                python,
                str(helper),
                "--socket",
                str(self.socket_path),
                "--uid",
                str(self.uid),
                "--gid",
                str(self.gid),
                "--parent-pid",
                str(self.parent_pid),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_for_socket()

    def disable_ports(
        self,
        ports: tuple[int, ...],
        *,
        expected_service: str = "",
    ) -> tuple[str, ...]:
        """Resolve and disable current systemd owners in one authorization."""

        with self.lock:
            try:
                self._start()  # password prompt happens here, only on click
                reply = self._request(
                    {
                        "action": "disable_stop_ports",
                        "protocol": PORT_CONFLICT_PROTOCOL_VERSION,
                        "ports": list(ports),
                        "expected_service": expected_service,
                    },
                    timeout=120,
                )
                if not reply.get("ok"):
                    raise RuntimeError(
                        reply.get("error")
                        or "Could not disable conflicting service"
                    )
                result = reply.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("Port-conflict helper returned invalid result")
                services = result.get("services", [])
                if not isinstance(services, list):
                    raise RuntimeError("Port-conflict helper returned invalid services")
                return tuple(str(service) for service in services if service)
            finally:
                # Deliberately end authorization after this click. Opening the
                # app never authenticates; a later explicit action can prompt
                # once again if required by policykit.
                self.close()

    def close(self) -> None:
        if self.socket_path.exists():
            try:
                self._request(
                    {"action": "shutdown", "protocol": PORT_CONFLICT_PROTOCOL_VERSION},
                    timeout=2,
                )
            except Exception:
                pass
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass
        self.process = None
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass


class PortConflictManager:
    """Read-only dashboard detection; privileged mutation only on user click."""

    def __init__(self, runner: CommandRunner, systemd: SystemdManager, config):
        self.runner = runner
        self.systemd = systemd
        self.config = config
        self.privilege = PortConflictPrivilegeSession()

    @staticmethod
    def _extract_port(local_address: str) -> int | None:
        match = re.search(r":(\d+)$", local_address)
        if not match:
            return None
        try:
            port = int(match.group(1))
        except ValueError:
            return None
        return port if port in TARGET_PORTS else None

    def _service_for_pid(self, pid: int) -> str:
        result = self.runner.run(
            [
                "systemctl",
                "status",
                str(pid),
                "--no-pager",
                "--full",
                "--lines=0",
            ],
            timeout=10,
        )
        for raw in result.stdout.splitlines():
            match = _SYSTEMCTL_UNIT_RE.match(raw)
            if match:
                return match.group(1)
        return ""

    def _service_description(self, service: str) -> str:
        result = self.runner.run(
            ["systemctl", "show", service, "-p", "Description", "--value"],
            timeout=10,
        )
        return result.stdout.strip() if result.ok else ""

    def _read_listeners(self) -> list[tuple[int, str, tuple[int, ...]]]:
        ss = shutil.which("ss")
        if not ss:
            raise RuntimeError(
                "The 'ss' command is required to inspect port 80/443 listeners "
                "(install the iproute2 package)."
            )

        # -p is intentionally unprivileged here. Linux may hide another user's
        # process/PID; the port itself remains visible and is enough to show the
        # Conflict card without authentication.
        result = self.runner.run([ss, "-H", "-ltnp"], timeout=15)
        if not result.ok:
            raise RuntimeError(result.output or "Could not inspect port 80/443")

        listeners: list[tuple[int, str, tuple[int, ...]]] = []
        for raw in result.stdout.splitlines():
            line = raw.strip()
            parts = line.split(None, 5)
            if len(parts) < 4:
                continue
            port = self._extract_port(parts[3])
            if port is None:
                continue
            process_match = _SS_PROCESS_RE.search(line)
            process = process_match.group(1) if process_match else ""
            pids = tuple(sorted({int(value) for value in _SS_PID_RE.findall(line)}))
            listeners.append((port, process, pids))
        return listeners

    def conflicts(self) -> list[PortConflict]:
        listeners = self._read_listeners()
        if not listeners:
            return []

        nginx_active = self.systemd.is_active("nginx")
        native_nginx_ports = {80}
        if bool(getattr(self.config, "https_enabled", False)):
            native_nginx_ports.add(443)

        by_service: dict[str, dict] = {}
        by_process: dict[tuple[str, tuple[int, ...]], set[int]] = {}
        unresolved_ports: set[int] = set()

        for port, process, pids in listeners:
            service = ""
            for pid in pids:
                service = self._service_for_pid(pid)
                if service:
                    break

            if process == "nginx" or service == "nginx.service":
                continue

            # When root-owned Nginx hides its PID from an unprivileged ss, avoid
            # showing NativeDev's own known listener as a conflict. If Nginx is
            # stopped/failed (the Apache test case), occupied ports remain.
            if not process and not service and nginx_active and port in native_nginx_ports:
                continue

            if service:
                item = by_service.setdefault(
                    service,
                    {"ports": set(), "process": process, "pids": set()},
                )
                item["ports"].add(port)
                item["pids"].update(pids)
                if not item["process"] and process:
                    item["process"] = process
            elif process:
                by_process.setdefault((process, pids), set()).add(port)
            else:
                unresolved_ports.add(port)

        conflicts: list[PortConflict] = []
        for service, item in by_service.items():
            conflicts.append(
                PortConflict(
                    ports=tuple(sorted(item["ports"])),
                    process=item["process"],
                    pids=tuple(sorted(item["pids"])),
                    service=service,
                    description=self._service_description(service),
                    enabled_state=self.systemd.enabled_state(service),
                    active=self.systemd.is_active(service),
                    manageable=True,
                )
            )

        for (process, pids), ports in by_process.items():
            conflicts.append(
                PortConflict(
                    ports=tuple(sorted(ports)),
                    process=process,
                    pids=pids,
                    manageable=True,
                )
            )

        for port in sorted(unresolved_ports):
            conflicts.append(PortConflict(ports=(port,), manageable=True))

        return sorted(
            conflicts,
            key=lambda item: (
                min(item.ports) if item.ports else 65535,
                item.title.casefold(),
            ),
        )

    def disable_and_stop(self, conflict: PortConflict) -> str:
        services = self.privilege.disable_ports(
            conflict.ports,
            expected_service=conflict.service,
        )
        if not services:
            return "Conflicting service"
        return ", ".join(
            service[:-8] if service.endswith(".service") else service
            for service in services
        )
