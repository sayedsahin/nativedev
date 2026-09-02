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
        "redis",
        "Redis",
        ("redis-server", "redis-tools"),
        "redis-server",
        "redis-cli",
        "Redis Server and redis-cli are installed and removed together.",
    ),
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


# Debian's PostgreSQL meta-packages and MariaDB top-level packages can be
# removed while their actual version/core runtimes remain installed. NativeDev
# treats those runtime packages as part of the component so Uninstall removes
# the executable service/client too, without using apt purge or deleting data.
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
        """Return installed runtime packages NativeDev should remove as one component.

        Most components are represented directly by ``spec.packages``. Debian's
        PostgreSQL meta-packages are different: removing ``postgresql`` can leave
        ``postgresql-17``/``postgresql-client-17`` installed. MariaDB can likewise
        leave ``mariadb-*-core`` binaries behind. Include those runtime packages
        so the corresponding server/client executable actually disappears.

        Shared/common packages and database data are deliberately not swept up.
        This is an APT *remove* operation, never purge/autoremove.
        """
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
        installed = self.installed_component_packages(spec)
        if not installed:
            raise RuntimeError(f"{spec.title} is not installed through APT")
        if spec.service:
            # Stop and disable before package removal when the unit still exists.
            # A previously half-removed component may already have lost its unit;
            # package cleanup should still proceed in that case.
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
