from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from ..config import AppConfig
from ..system import AptManager, CommandRunner
from .php import PhpManager


@dataclass(frozen=True, slots=True)
class DeveloperToolSpec:
    key: str
    title: str
    package: str
    document_root: Path
    entrypoint: str
    description: str


DEVELOPER_WEB_TOOLS: tuple[DeveloperToolSpec, ...] = (
    DeveloperToolSpec(
        "phpmyadmin",
        "phpMyAdmin",
        "phpmyadmin",
        Path("/usr/share/phpmyadmin"),
        "index.php",
        "MariaDB / MySQL administration",
    ),
    DeveloperToolSpec(
        "adminer",
        "Adminer",
        "adminer",
        Path("/usr/share/adminer"),
        "adminer.php",
        "Database administration",
    ),
)

DEVELOPER_TOOL_BY_KEY = {spec.key: spec for spec in DEVELOPER_WEB_TOOLS}
PHPMYADMIN_NATIVEDEV_CONFIG = Path("/etc/phpmyadmin/conf.d/nativedev.php")
PHPMYADMIN_RUNTIME_ROOT = Path("/var/lib/nativedev/phpmyadmin")
PHPMYADMIN_CONFIG_MARKER = "// Managed by NativeDev. Manual edits may be replaced."
DEVELOPER_TOOL_DOMAIN = "localhost"


def developer_tool_hostname(key: str) -> str:
    if key not in DEVELOPER_TOOL_BY_KEY:
        raise RuntimeError("Developer tool is outside NativeDev's catalog")
    return f"{key}.{DEVELOPER_TOOL_DOMAIN}"


@dataclass(slots=True)
class DeveloperToolState:
    spec: DeveloperToolSpec
    installed: bool
    installable: bool
    version: str | None
    php_version: str
    php_versions: list[str]
    url: str
    document_root_ready: bool
    runtime_ready: bool
    runtime_note: str


class DeveloperToolManager:
    """APT-backed developer web applications served by NativeDev Nginx.

    Package ownership and updates remain with Debian/Ubuntu. NativeDev owns only
    the per-tool PHP-FPM selection and the exact local hostname integration.
    """

    def __init__(self, runner: CommandRunner, apt: AptManager, php: PhpManager, config: AppConfig):
        self.runner = runner
        self.apt = apt
        self.php = php
        self.config = config

    @staticmethod
    def supports(key: str) -> bool:
        return key in DEVELOPER_TOOL_BY_KEY

    def state(self, spec: DeveloperToolSpec) -> DeveloperToolState:
        installed = self.apt.is_installed(spec.package)
        php_versions = self.php.installed_fpm_versions()
        selected = self.effective_php(spec.key)
        runtime_ready, runtime_note = self._runtime_state(spec)
        return DeveloperToolState(
            spec=spec,
            installed=installed,
            installable=bool(self.apt.candidate(spec.package)),
            version=self._installed_version(spec.package) if installed else None,
            php_version=selected,
            php_versions=php_versions,
            # Database web tools are intentionally opened over plain HTTP.
            # NativeDev local HTTPS remains available for parked projects, but
            # these distro-owned admin tools do not need certificate coupling.
            url=f"http://{developer_tool_hostname(spec.key)}",
            document_root_ready=(spec.document_root / spec.entrypoint).is_file(),
            runtime_ready=runtime_ready,
            runtime_note=runtime_note,
        )

    def effective_php(self, key: str) -> str:
        selected = self.selected_php(key)
        installed = self.php.installed_fpm_versions()
        if selected in installed:
            return selected
        return self.php.default_fpm_version() or (installed[0] if installed else "")

    def selected_php(self, key: str) -> str:
        value = self.config.developer_tools.get(key, {})
        if not isinstance(value, dict):
            return ""
        version = value.get("php", "")
        return version if isinstance(version, str) else ""

    def set_selected_php(self, key: str, version: str) -> None:
        if key not in DEVELOPER_TOOL_BY_KEY:
            raise RuntimeError("Developer tool is outside NativeDev's catalog")
        if version not in self.php.installed_fpm_versions():
            raise RuntimeError(f"PHP {version} FPM is not installed")
        if not self.php.fpm_config_ready(version):
            raise RuntimeError(f"PHP {version} FPM configuration is missing. Repair FPM first.")
        values = dict(self.config.developer_tools)
        values[key] = {"php": version}
        self.config.developer_tools = values
        self.config.save()

    def clear_selected_php(self, key: str) -> None:
        if key not in self.config.developer_tools:
            return
        values = dict(self.config.developer_tools)
        values.pop(key, None)
        self.config.developer_tools = values
        self.config.save()

    def install(self, spec: DeveloperToolSpec, version: str) -> None:
        if version not in self.php.installed_fpm_versions():
            raise RuntimeError(f"Install PHP {version} FPM before installing {spec.title}")
        if not self.php.fpm_config_ready(version):
            raise RuntimeError(f"PHP {version} FPM configuration is missing. Repair FPM first.")
        if not self.apt.candidate(spec.package):
            raise RuntimeError(f"{spec.title} is unavailable in the configured Debian/Ubuntu repositories")

        self.runner.privileged_operation(
            "developer_tool.install",
            tool=spec.key,
            check=True,
            timeout=None,
        )
        # Do not report a usable integration until the distro package actually
        # provided its expected fixed document root.
        entrypoint = spec.document_root / spec.entrypoint
        if not entrypoint.is_file():
            raise RuntimeError(
                f"{spec.title} was installed, but its expected entry point is missing: {entrypoint}"
            )
        self.set_selected_php(spec.key, version)

    def uninstall(self, spec: DeveloperToolSpec) -> None:
        if not self.apt.is_installed(spec.package):
            raise RuntimeError(f"{spec.title} is not installed")
        self.runner.privileged_operation(
            "developer_tool.uninstall",
            tool=spec.key,
            check=True,
            timeout=None,
        )
        self.clear_selected_php(spec.key)

    def reconcile_runtime(self, spec: DeveloperToolSpec) -> None:
        if spec.key != "phpmyadmin":
            return
        if not self.apt.is_installed(spec.package):
            raise RuntimeError("phpMyAdmin is not installed")
        self.runner.privileged_operation(
            "developer_tool.reconcile",
            tool=spec.key,
            check=True,
            timeout=120,
        )

    def _runtime_state(self, spec: DeveloperToolSpec) -> tuple[bool, str]:
        if spec.key != "phpmyadmin":
            return True, ""
        config_path = PHPMYADMIN_NATIVEDEV_CONFIG
        temp_path = PHPMYADMIN_RUNTIME_ROOT / str(os.getuid()) / "tmp"
        config_ready = False
        if config_path.is_file() and not config_path.is_symlink():
            try:
                config_ready = PHPMYADMIN_CONFIG_MARKER in config_path.read_text(
                    encoding="utf-8", errors="strict"
                )
            except (OSError, UnicodeError):
                config_ready = False
        temp_ready = temp_path.is_dir() and os.access(temp_path, os.R_OK | os.W_OK | os.X_OK)
        if config_ready and temp_ready:
            return True, ""
        return False, "NativeDev phpMyAdmin runtime integration needs repair."

    def tools_using_php(self, version: str) -> list[str]:
        used: list[str] = []
        for spec in DEVELOPER_WEB_TOOLS:
            if self.apt.is_installed(spec.package) and self.effective_php(spec.key) == version:
                used.append(spec.title)
        return used

    def _installed_version(self, package: str) -> str | None:
        result = self.runner.run(["dpkg-query", "-W", "-f=${Version}", package], timeout=15)
        if not result.ok:
            return None
        value = result.stdout.strip()
        return value or None
