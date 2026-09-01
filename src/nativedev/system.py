from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import socket
import sys
import tempfile
import threading
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


@dataclass(slots=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout or self.stderr).strip()


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult):
        self.result = result
        message = result.stderr.strip() or result.stdout.strip() or f"Command failed: {result.argv}"
        super().__init__(message)


class PrivilegeSession:
    """One authenticated root helper per GUI session.

    The helper is started lazily through pkexec on the first privileged action.
    Subsequent privileged actions use a private Unix socket, so the user is not
    prompted for a password for every apt/systemctl operation.
    """

    def __init__(self):
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.parent_pid = os.getpid()
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        self.socket_path = runtime / f"nativedev-{self.uid}-{self.parent_pid}.sock"
        self.process: subprocess.Popen | None = None
        self.lock = threading.RLock()

    def _helper_path(self) -> Path:
        installed = Path("/usr/lib/nativedev/privileged_helper.py")
        if installed.is_file() and installed.stat().st_uid == 0 and not (installed.stat().st_mode & 0o022):
            return installed
        return Path(__file__).with_name("privileged_helper.py").resolve()

    def ensure(self) -> None:
        if self._ping():
            return
        if self.process and self.process.poll() is None:
            self._wait_for_socket()
            return
        self.socket_path.unlink(missing_ok=True)
        pkexec = shutil.which("pkexec")
        if not pkexec:
            raise RuntimeError("pkexec is required for privileged GUI operations")
        python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
        self.process = subprocess.Popen(
            [
                pkexec, python, str(self._helper_path()),
                "--socket", str(self.socket_path),
                "--uid", str(self.uid),
                "--gid", str(self.gid),
                "--parent-pid", str(self.parent_pid),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_for_socket()

    def _wait_for_socket(self) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self._ping():
                return
            if self.process and self.process.poll() is not None:
                raise RuntimeError("System authorization was cancelled or the privileged helper could not start")
            time.sleep(0.1)
        raise RuntimeError("Timed out waiting for system authorization")

    def _request(self, payload: dict, *, timeout: int = 120) -> dict:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(max(5, min(timeout + 5, 1805)))
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
                raise RuntimeError("Privileged helper returned no response")
            return json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))
        finally:
            client.close()

    def _ping(self) -> bool:
        if not self.socket_path.exists():
            return False
        try:
            reply = self._request({"action": "ping"}, timeout=2)
            return bool(reply.get("ok"))
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            return False

    def run(self, argv: Sequence[str], *, timeout: int = 120) -> CommandResult:
        with self.lock:
            self.ensure()
            reply = self._request(
                {"action": "run", "argv": list(argv), "timeout": timeout},
                timeout=timeout,
            )
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error") or "Privileged operation rejected")
            return CommandResult(
                list(argv),
                int(reply.get("returncode", 1)),
                str(reply.get("stdout", "")),
                str(reply.get("stderr", "")),
            )

    def close(self) -> None:
        with self.lock:
            if self.socket_path.exists():
                try:
                    self._request({"action": "shutdown"}, timeout=2)
                except Exception:
                    pass
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                except OSError:
                    pass
            self.socket_path.unlink(missing_ok=True)


class CommandRunner:
    """Small subprocess wrapper. Never uses shell=True."""

    def __init__(self):
        self.privilege = PrivilegeSession()

    def run(
        self,
        argv: Sequence[str],
        *,
        privileged: bool = False,
        check: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: int | None = 120,
    ) -> CommandResult:
        command = [str(item) for item in argv]
        effective_timeout = timeout or 120
        if privileged and os.geteuid() != 0:
            result = self.privilege.run(command, timeout=effective_timeout)
            if check and not result.ok:
                raise CommandError(result)
            return result

        try:
            proc = subprocess.run(
                command,
                text=True,
                capture_output=True,
                env=dict(os.environ, **(env or {})),
                timeout=timeout,
            )
            result = CommandResult(command, proc.returncode, proc.stdout, proc.stderr)
        except FileNotFoundError as exc:
            # A managed component being absent is normal state for NativeDev.
            # Surface the conventional shell-style 127 result so status probes
            # can render Install/Configure actions instead of crashing the GUI.
            result = CommandResult(command, 127, "", str(exc))
        if check and not result.ok:
            raise CommandError(result)
        return result

    def bash_nvm(self, nvm_dir: Path, args: Sequence[str], *, check: bool = False) -> CommandResult:
        # NVM is a sourced shell function, so this is the one intentionally shell-parsed path.
        # All dynamic values are shell-quoted before interpolation.
        quoted_dir = shlex.quote(str(nvm_dir))
        quoted_args = " ".join(shlex.quote(str(a)) for a in args)
        script = (
            f"export NVM_DIR={quoted_dir}; "
            f"[ -s \"$NVM_DIR/nvm.sh\" ] || exit 127; "
            f". \"$NVM_DIR/nvm.sh\"; nvm {quoted_args}"
        )
        result = self.run(["/bin/bash", "-lc", script], check=False, timeout=900)
        if check and not result.ok:
            raise CommandError(result)
        return result

    def close(self) -> None:
        self.privilege.close()


@dataclass(slots=True)
class DistroInfo:
    id: str
    name: str
    version_id: str
    codename: str
    id_like: tuple[str, ...]
    pretty_name: str

    @property
    def is_debian_family(self) -> bool:
        family = {self.id, *self.id_like}
        return bool(family.intersection({"debian", "ubuntu"}))


def read_os_release(path: Path = Path("/etc/os-release")) -> DistroInfo:
    data: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key] = value.strip().strip('"')
    except OSError:
        pass

    # Ubuntu derivatives usually expose UBUNTU_CODENAME. Prefer the base codename
    # so repositories can target the parent release rather than Mint/Pop codenames.
    codename = (
        data.get("UBUNTU_CODENAME")
        or data.get("DEBIAN_CODENAME")
        or data.get("VERSION_CODENAME")
        or ""
    )
    return DistroInfo(
        id=data.get("ID", "unknown").lower(),
        name=data.get("NAME", "Unknown Linux"),
        version_id=data.get("VERSION_ID", ""),
        codename=codename.lower(),
        id_like=tuple(x.lower() for x in data.get("ID_LIKE", "").split()),
        pretty_name=data.get("PRETTY_NAME", data.get("NAME", "Unknown Linux")),
    )


class AptManager:
    def __init__(self, runner: CommandRunner):
        self.runner = runner

    @property
    def available(self) -> bool:
        return bool(shutil.which("apt-get") and shutil.which("dpkg-query"))

    def is_installed(self, package: str) -> bool:
        result = self.runner.run(
            ["dpkg-query", "-W", "-f=${db:Status-Abbrev}", package], timeout=15
        )
        return result.ok and result.stdout.startswith("ii ")

    def candidate(self, package: str) -> str | None:
        result = self.runner.run(["apt-cache", "policy", package], timeout=30)
        if not result.ok:
            return None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Candidate:"):
                value = stripped.partition(":")[2].strip()
                return None if value in {"", "(none)"} else value
        return None

    def refresh(self) -> CommandResult:
        return self.runner.run(["apt-get", "update"], privileged=True, check=True, timeout=900)

    def install(self, packages: Iterable[str], *, refresh: bool = False) -> CommandResult:
        packages = [p for p in packages if p]
        if refresh:
            self.refresh()
        return self.runner.run(
            ["apt-get", "install", "-y", *packages], privileged=True, check=True, timeout=1200
        )

    def remove(self, packages: Iterable[str]) -> CommandResult:
        return self.runner.run(
            ["apt-get", "remove", "-y", *list(packages)], privileged=True, check=True, timeout=1200
        )


class SystemdManager:
    def __init__(self, runner: CommandRunner):
        self.runner = runner

    @property
    def available(self) -> bool:
        return bool(shutil.which("systemctl"))

    def is_active(self, service: str) -> bool:
        return self.runner.run(["systemctl", "is-active", "--quiet", service], timeout=10).ok

    def enabled_state(self, service: str) -> str:
        result = self.runner.run(["systemctl", "is-enabled", service], timeout=10)
        return result.stdout.strip() or result.stderr.strip() or "unknown"

    def is_enabled(self, service: str) -> bool:
        return self.enabled_state(service) in {"enabled", "enabled-runtime", "linked", "linked-runtime"}

    def start(self, service: str) -> CommandResult:
        return self.runner.run(["systemctl", "start", service], privileged=True, check=True)

    def stop(self, service: str) -> CommandResult:
        return self.runner.run(["systemctl", "stop", service], privileged=True, check=True)

    def restart(self, service: str) -> CommandResult:
        return self.runner.run(["systemctl", "restart", service], privileged=True, check=True)

    def reload(self, service: str) -> CommandResult:
        return self.runner.run(["systemctl", "reload", service], privileged=True, check=True)

    def enable(self, service: str) -> CommandResult:
        return self.runner.run(["systemctl", "enable", service], privileged=True, check=True)

    def disable(self, service: str) -> CommandResult:
        return self.runner.run(["systemctl", "disable", service], privileged=True, check=True)

    def enable_now(self, service: str) -> CommandResult:
        return self.runner.run(
            ["systemctl", "enable", "--now", service], privileged=True, check=True
        )

    def disable_now(self, service: str) -> CommandResult:
        return self.runner.run(
            ["systemctl", "disable", "--now", service], privileged=True, check=True
        )
