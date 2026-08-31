from __future__ import annotations

import shutil
from dataclasses import dataclass

from .system import AptManager, CommandRunner, SystemdManager


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    key: str
    title: str
    packages: tuple[str, ...]
    service: str | None = None
    binary: str | None = None
    note: str = ""


COMPONENTS: tuple[ComponentSpec, ...] = (
    ComponentSpec("nginx", "Nginx", ("nginx",), "nginx", "nginx"),
    ComponentSpec("redis-server", "Redis Server", ("redis-server",), "redis-server", "redis-server"),
    ComponentSpec("redis-cli", "redis-cli", ("redis-tools",), None, "redis-cli", "Redis command-line client; no system service."),
    ComponentSpec("memcached", "Memcached", ("memcached",), "memcached", "memcached"),
    ComponentSpec("mariadb", "MariaDB", ("mariadb-server", "mariadb-client"), "mariadb", "mariadb"),
    ComponentSpec(
        "mysql",
        "MySQL",
        ("mysql-server",),
        "mysql",
        "mysql",
        "Availability depends on the configured Debian-family repositories.",
    ),
    ComponentSpec("postgresql", "PostgreSQL", ("postgresql", "postgresql-client"), "postgresql", "psql"),
    ComponentSpec("composer", "Composer", ("composer",), None, "composer", "CLI tool; no system service."),
    ComponentSpec("mkcert", "mkcert", ("mkcert",), None, "mkcert", "Local certificate tool; no system service."),
)


@dataclass(slots=True)
class ComponentState:
    spec: ComponentSpec
    installed: bool
    packages_installed: bool
    installable: bool
    running: bool
    enabled: bool
    enabled_state: str
    uninstallable: bool
    uninstall_note: str
    binary_path: str | None


class ServiceManager:
    def __init__(self, runner: CommandRunner, apt: AptManager, systemd: SystemdManager):
        self.runner = runner
        self.apt = apt
        self.systemd = systemd

    def state(self, spec: ComponentSpec) -> ComponentState:
        package_states = {pkg: self.apt.is_installed(pkg) for pkg in spec.packages}
        packages_installed = any(package_states.values())
        # For a system service, the first package is the server/primary package.
        # A client binary alone must not make the service look installed.
        installed = package_states.get(spec.packages[0], False) if spec.service else packages_installed
        binary_path = shutil.which(spec.binary) if spec.binary else None
        if binary_path and not spec.service:
            installed = True
        installable = any(self.apt.candidate(pkg) for pkg in spec.packages)
        running = bool(spec.service and self.systemd.is_active(spec.service))
        enabled_state = self.systemd.enabled_state(spec.service) if spec.service else "n/a"
        enabled = enabled_state in {"enabled", "enabled-runtime", "linked", "linked-runtime"}
        uninstallable = packages_installed
        uninstall_note = ""
        if spec.key == "redis-cli" and self.apt.is_installed("redis-server"):
            uninstallable = False
            uninstall_note = "redis-tools is required by the installed Redis Server package."
        return ComponentState(
            spec, installed, packages_installed, installable, running, enabled, enabled_state,
            uninstallable, uninstall_note, binary_path
        )

    def install(self, spec: ComponentSpec) -> None:
        if spec.key == "mysql" and self.apt.is_installed("mariadb-server"):
            raise RuntimeError("MariaDB is installed. Remove or migrate it before installing MySQL.")
        if spec.key == "mariadb" and self.apt.is_installed("mysql-server"):
            raise RuntimeError("MySQL is installed. Remove or migrate it before installing MariaDB.")
        installable = [pkg for pkg in spec.packages if self.apt.candidate(pkg)]
        if not installable:
            raise RuntimeError(f"No installable APT package found for {spec.title}")
        self.apt.install(installable)
        if spec.service:
            self.systemd.enable_now(spec.service)

    def uninstall(self, spec: ComponentSpec) -> None:
        if spec.key == "redis-cli" and self.apt.is_installed("redis-server"):
            raise RuntimeError("redis-cli cannot be removed while Redis Server is installed because Debian's redis-server depends on redis-tools.")
        installed = [pkg for pkg in spec.packages if self.apt.is_installed(pkg)]
        if not installed:
            raise RuntimeError(f"{spec.title} is not installed through APT")
        if spec.service:
            # Stop and disable before package removal when possible. If the unit is
            # already gone/disabled, package removal should still proceed.
            try:
                self.systemd.disable_now(spec.service)
            except RuntimeError:
                pass
        self.apt.remove(installed)

    def start(self, spec: ComponentSpec) -> None:
        self._require_service(spec)
        self.systemd.start(spec.service)

    def stop(self, spec: ComponentSpec) -> None:
        self._require_service(spec)
        self.systemd.stop(spec.service)

    def restart(self, spec: ComponentSpec) -> None:
        self._require_service(spec)
        self.systemd.restart(spec.service)

    def enable(self, spec: ComponentSpec) -> None:
        self._require_service(spec)
        self.systemd.enable(spec.service)

    def disable(self, spec: ComponentSpec) -> None:
        self._require_service(spec)
        self.systemd.disable(spec.service)

    @staticmethod
    def _require_service(spec: ComponentSpec) -> None:
        if not spec.service:
            raise RuntimeError(f"{spec.title} has no system service")
