from __future__ import annotations

import re
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
    ComponentSpec(
        "mariadb",
        "MariaDB / MySQL",
        ("mariadb-server", "mariadb-client"),
        "mariadb",
        "mariadb",
        "MySQL-compatible server; NativeDev installs MariaDB from Debian repositories.",
    ),
    ComponentSpec("postgresql", "PostgreSQL", ("postgresql", "postgresql-client"), "postgresql", "psql"),
    ComponentSpec(
        "redis",
        "Redis",
        ("redis-server", "redis-tools"),
        "redis-server",
        "redis-cli",
        "Redis Server and redis-cli are installed and removed together.",
    ),
    ComponentSpec("memcached", "Memcached", ("memcached",), "memcached", "memcached"),
    ComponentSpec("composer", "Composer", ("composer",), None, "composer", "CLI tool; no system service."),
    ComponentSpec("mkcert", "mkcert", ("mkcert",), None, "mkcert", "Local certificate tool; no system service."),
)


# Debian's PostgreSQL meta-packages and MariaDB top-level packages can be
# removed while their actual version/core runtimes remain installed. Include
# those runtime packages so Uninstall removes the executable service/client,
# but deliberately leave common/shared packages and database data alone.
POSTGRESQL_RUNTIME_RE = re.compile(r"^postgresql(?:-client)?-\d+(?:\.\d+)*$")
MARIADB_RUNTIME_RE = re.compile(r"^mariadb-(?:server|client)-core(?:-\d+(?:\.\d+)*)?$")


@dataclass(slots=True)
class ComponentState:
    spec: ComponentSpec
    installed: bool
    packages_installed: bool
    installable: bool
    running: bool
    enabled: bool
    enabled_state: str
    service_available: bool
    uninstallable: bool
    uninstall_note: str
    binary_path: str | None
    version: str | None


class ServiceManager:
    def __init__(self, runner: CommandRunner, apt: AptManager, systemd: SystemdManager):
        self.runner = runner
        self.apt = apt
        self.systemd = systemd

    def _installed_package_names(self) -> set[str]:
        result = self.runner.run(
            ["dpkg-query", "-W", "-f=${db:Status-Abbrev}\t${binary:Package}\n"],
            timeout=45,
        )
        if not result.ok:
            return set()

        packages: set[str] = set()
        for raw in result.stdout.splitlines():
            status, separator, package = raw.partition("\t")
            if not separator or not status.startswith("ii "):
                continue
            packages.add(package.strip().split(":", 1)[0])
        return packages

    def installed_component_packages(self, spec: ComponentSpec) -> list[str]:
        """Return installed packages that make a component present in the UI."""
        installed = self._installed_package_names()
        wanted = {package for package in spec.packages if package in installed}

        if spec.key == "postgresql":
            wanted.update(package for package in installed if POSTGRESQL_RUNTIME_RE.fullmatch(package))
        elif spec.key == "mariadb":
            wanted.update(package for package in installed if MARIADB_RUNTIME_RE.fullmatch(package))

        return sorted(wanted)

    def state(self, spec: ComponentSpec) -> ComponentState:
        installed_packages = self.installed_component_packages(spec)
        packages_installed = bool(installed_packages)
        binary_path = shutil.which(spec.binary) if spec.binary else None

        # A partially removed component (for example postgresql meta-package gone
        # but postgresql-client-17 still installed) must remain visible as
        # installed/uninstallable so NativeDev can finish the cleanup.
        installed = packages_installed
        if not spec.service and binary_path:
            installed = True

        installable = any(self.apt.candidate(pkg) for pkg in spec.packages)

        enabled_state = "n/a"
        service_available = False
        running = False
        enabled = False
        if spec.service and packages_installed:
            enabled_state = self.systemd.enabled_state(spec.service)
            service_available = enabled_state not in {"n/a", "not-found"} and not enabled_state.startswith("Failed ")
            if service_available:
                running = self.systemd.is_active(spec.service)
                enabled = enabled_state in {"enabled", "enabled-runtime", "linked", "linked-runtime"}

        version = self._component_version(spec, binary_path) if installed else None

        return ComponentState(
            spec=spec,
            installed=installed,
            packages_installed=packages_installed,
            installable=installable,
            running=running,
            enabled=enabled,
            enabled_state=enabled_state,
            service_available=service_available,
            uninstallable=packages_installed,
            uninstall_note="",
            binary_path=binary_path,
            version=version,
        )

    def install(self, spec: ComponentSpec) -> None:
        installable = [pkg for pkg in spec.packages if self.apt.candidate(pkg)]
        if not installable:
            raise RuntimeError(f"No installable APT package found for {spec.title}")
        self.apt.install(installable)
        if spec.service:
            self.systemd.enable_now(spec.service)

    def uninstall(self, spec: ComponentSpec) -> None:
        """Remove runtime packages without purge/autoremove or database-data deletion.

        Database credential metadata is cleared by the controller after this
        succeeds. Debian common/shared packages and on-disk database clusters
        are intentionally preserved, matching NativeDev's original stable
        uninstall behavior.
        """
        installed = self.installed_component_packages(spec)
        if not installed:
            raise RuntimeError(f"{spec.title} is not installed through APT")
        if spec.service:
            try:
                self.systemd.disable_now(spec.service)
            except RuntimeError:
                pass
        self.apt.remove(installed)

    def _component_version(self, spec: ComponentSpec, binary_path: str | None) -> str | None:
        if spec.key != "mariadb" or not binary_path:
            return None
        result = self.runner.run([binary_path, "--version"], timeout=15)
        if not result.ok:
            return None
        match = re.search(r"\bDistrib\s+([^,\s]+)", result.output)
        if match:
            return match.group(1)
        match = re.search(r"\b(\d+\.\d+(?:\.\d+)?(?:-MariaDB)?)\b", result.output)
        return match.group(1) if match else None

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
