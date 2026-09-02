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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


PRIVILEGE_PROTOCOL_VERSION = 5
PHP_FPM_COMMAND_RE = re.compile(r"^php-fpm(?P<version>\d+\.\d+)$")
PHP_BINARY_PATH_RE = re.compile(r"^/usr/bin/php(?P<version>\d+\.\d+)$")


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


class PrivilegeProtocolMismatch(RuntimeError):
    pass


def privileged_operation_for_command(argv: Sequence[str], timeout: int = 120) -> dict:
    """Translate the legacy subprocess-shaped call into a semantic root RPC.

    Managers still describe familiar system commands, but the root helper never
    receives or executes client-supplied argv.  Only these structured NativeDev
    operations cross the privilege boundary.
    """
    command = [str(item) for item in argv]
    if not command:
        raise RuntimeError("Empty privileged command")
    cmd = Path(command[0]).name
    args = command[1:]
    payload: dict = {
        "protocol": PRIVILEGE_PROTOCOL_VERSION,
        "timeout": max(1, min(int(timeout), 1800)),
    }

    if cmd == "apt-get":
        if args == ["update"]:
            return {**payload, "action": "apt.update"}
        if len(args) >= 3 and args[0] == "install" and args[1:5] == ["--reinstall", "-y", "-o", "Dpkg::Options::=--force-confmiss"]:
            packages = args[5:]
            if not packages:
                raise RuntimeError("No package supplied for PHP FPM repair")
            return {**payload, "action": "apt.reinstall_confmiss", "packages": packages}
        if len(args) >= 2 and args[0] in {"install", "remove"}:
            action = args[0]
            rest = args[1:]
            if rest[:1] == ["-y"]:
                rest = rest[1:]
            if not rest or any(item.startswith("-") for item in rest):
                raise RuntimeError(f"Unsupported privileged APT request: {command}")
            return {**payload, "action": f"apt.{action}", "packages": rest}
        raise RuntimeError(f"Unsupported privileged APT request: {command}")

    if cmd == "systemctl":
        if not args:
            raise RuntimeError("systemctl action missing")
        verb = args[0]
        rest = args[1:]
        now = False
        if rest[:1] == ["--now"]:
            now = True
            rest = rest[1:]
        if len(rest) != 1:
            raise RuntimeError(f"Unsupported systemctl request: {command}")
        return {**payload, "action": "systemd.service", "verb": verb, "now": now, "service": rest[0]}

    if cmd == "install" and len(args) == 4 and args[0] == "-m":
        return {
            **payload,
            "action": "file.install",
            "mode": args[1],
            "source": args[2],
            "destination": args[3],
        }

    if cmd == "mkdir" and args[:1] == ["-p"] and args[1:]:
        return {**payload, "action": "file.mkdir", "paths": args[1:]}

    if cmd == "ln" and args == [
        "-sfn",
        "/etc/nginx/sites-available/nativedev-sites.conf",
        "/etc/nginx/sites-enabled/nativedev-sites.conf",
    ]:
        return {**payload, "action": "nginx.enable_site"}

    if cmd == "rm" and args[:1] == ["-f"] and args[1:]:
        return {**payload, "action": "file.remove", "paths": args[1:]}

    if cmd == "nmcli" and args in (["general", "reload", "conf"], ["general", "reload", "dns-full"]):
        return {**payload, "action": "networkmanager.reload", "scope": args[-1]}

    if cmd == "nginx" and args == ["-t"]:
        return {**payload, "action": "nginx.test"}

    fpm = PHP_FPM_COMMAND_RE.fullmatch(cmd)
    if fpm and args in (["-t"], ["-tt"]):
        return {
            **payload,
            "action": "php_fpm.test",
            "version": fpm.group("version"),
            "verbose": args == ["-tt"],
        }

    if cmd == "update-alternatives" and len(args) == 3 and args[:2] == ["--set", "php"]:
        php = PHP_BINARY_PATH_RE.fullmatch(args[2])
        if php:
            return {**payload, "action": "php.set_default", "version": php.group("version")}

    raise RuntimeError(f"Privileged operation is outside NativeDev's client allowlist: {command}")


class PrivilegeSession:
    """One authenticated, structured root helper per application session."""

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
        if installed.is_file():
            stat = installed.stat()
            if stat.st_uid == 0 and not (stat.st_mode & 0o022):
                return installed
        if os.environ.get("NATIVEDEV_ALLOW_SOURCE_HELPER") == "1":
            return Path(__file__).with_name("privileged_helper.py").resolve()
        raise RuntimeError(
            "NativeDev's root-owned privileged helper is not installed. Run install.sh first. "
            "Source-tree helper execution is available only through explicit development mode."
        )

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
        helper = self._helper_path()
        installed_helper = helper == Path("/usr/lib/nativedev/privileged_helper.py")
        if installed_helper:
            launch = [pkexec, str(helper)]
        else:
            python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
            launch = [pkexec, python, str(helper)]
        self.process = subprocess.Popen(
            [
                *launch,
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
            reply = self._request(
                {"action": "ping", "protocol": PRIVILEGE_PROTOCOL_VERSION}, timeout=2
            )
            if reply.get("ok") and reply.get("protocol") != PRIVILEGE_PROTOCOL_VERSION:
                raise PrivilegeProtocolMismatch(
                    "NativeDev privileged helper version mismatch. Re-run install.sh to update the root-owned helper."
                )
            if not reply.get("ok") and "protocol" in str(reply.get("error", "")).lower():
                raise PrivilegeProtocolMismatch(
                    "NativeDev privileged helper version mismatch. Re-run install.sh to update the root-owned helper."
                )
            return bool(reply.get("ok"))
        except PrivilegeProtocolMismatch:
            raise
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            return False

    def operation(self, payload: dict, *, display_argv: Sequence[str], timeout: int = 120) -> CommandResult:
        with self.lock:
            self.ensure()
            request = {
                **payload,
                "protocol": PRIVILEGE_PROTOCOL_VERSION,
                "timeout": max(1, min(int(timeout), 1800)),
            }
            reply = self._request(request, timeout=timeout)
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error") or "Privileged operation rejected")
            return CommandResult(
                list(display_argv),
                int(reply.get("returncode", 1)),
                str(reply.get("stdout", "")),
                str(reply.get("stderr", "")),
            )

    def run(self, argv: Sequence[str], *, timeout: int = 120) -> CommandResult:
        payload = privileged_operation_for_command(argv, timeout)
        return self.operation(payload, display_argv=argv, timeout=timeout)

    def close(self) -> None:
        with self.lock:
            if self.socket_path.exists():
                try:
                    self._request(
                        {"action": "shutdown", "protocol": PRIVILEGE_PROTOCOL_VERSION}, timeout=2
                    )
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
            result = CommandResult(command, 127, "", str(exc))
        if check and not result.ok:
            raise CommandError(result)
        return result

    def privileged_operation(
        self,
        action: str,
        *,
        check: bool = False,
        timeout: int = 120,
        **fields,
    ) -> CommandResult:
        """Run a semantic helper operation that has no safe client-side argv form."""
        if os.geteuid() == 0:
            raise RuntimeError("NativeDev semantic privileged operations must be run from the normal-user application session")
        result = self.privilege.operation(
            {"action": action, **fields},
            display_argv=[f"nativedev:{action}"],
            timeout=timeout,
        )
        if check and not result.ok:
            raise CommandError(result)
        return result

    def bash_nvm(self, nvm_dir: Path, args: Sequence[str], *, check: bool = False) -> CommandResult:
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

    def install_php(self, packages: Iterable[str], *, allow_downgrades: bool = False) -> CommandResult:
        """Install/reinstall versioned PHP packages and restore missing UCF config.

        Debian/Sury PHP module definitions under /etc/php/<version>/mods-available
        are managed by ucf rather than ordinary dpkg conffiles. If a definition
        was locally deleted, a normal package reinstall deliberately preserves
        that deletion. NativeDev's explicit PHP Install action promises a ready
        framework baseline, so the privileged helper runs this operation with
        UCF_FORCE_CONFFMISS=1. Existing config files are not overwritten.
        """
        packages = [p for p in packages if p]
        if not packages:
            raise RuntimeError("No PHP packages supplied")
        return self.runner.privileged_operation(
            "php.install_packages",
            packages=packages,
            allow_downgrades=allow_downgrades,
            check=True,
            timeout=1200,
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
