from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..system import AptManager, CommandRunner, SystemdManager
from .php import PhpManager


SAPIS = ("cli", "fpm")


@dataclass(frozen=True, slots=True)
class PhpExtensionSpec:
    key: str
    title: str
    category: str
    package_suffix: str
    modules: tuple[str, ...]
    note: str = ""
    built_in_from: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class PhpExtensionState:
    spec: PhpExtensionSpec
    package: str
    installed: bool
    enabled: bool
    installable: bool
    built_in: bool


EXTENSIONS: tuple[PhpExtensionSpec, ...] = (
    PhpExtensionSpec("mysql", "MySQL", "Database", "mysql", ("mysqlnd", "mysqli", "pdo_mysql")),
    PhpExtensionSpec("pgsql", "PostgreSQL", "Database", "pgsql", ("pgsql", "pdo_pgsql")),
    PhpExtensionSpec("sqlite3", "SQLite", "Database", "sqlite3", ("sqlite3", "pdo_sqlite")),
    PhpExtensionSpec("bcmath", "BCMath", "Common", "bcmath", ("bcmath",)),
    PhpExtensionSpec("curl", "cURL", "Common", "curl", ("curl",)),
    PhpExtensionSpec("gd", "GD", "Common", "gd", ("gd",)),
    PhpExtensionSpec("intl", "Intl", "Common", "intl", ("intl",)),
    PhpExtensionSpec("mbstring", "Mbstring", "Common", "mbstring", ("mbstring",)),
    PhpExtensionSpec("readline", "Readline", "Common", "readline", ("readline",)),
    PhpExtensionSpec("xml", "XML", "Common", "xml", ("dom", "simplexml", "xml", "xmlreader", "xmlwriter", "xsl")),
    PhpExtensionSpec("zip", "ZIP", "Common", "zip", ("zip",)),
    PhpExtensionSpec(
        "opcache",
        "OPcache",
        "Common",
        "opcache",
        ("opcache",),
        "PHP 8.5+ provides OPcache as part of the runtime rather than a separate package.",
        built_in_from=(8, 5),
    ),
    PhpExtensionSpec("apcu", "APCu", "Optional", "apcu", ("apcu",)),
    PhpExtensionSpec("bz2", "BZip2", "Optional", "bz2", ("bz2",)),
    PhpExtensionSpec("dba", "DBA", "Optional", "dba", ("dba",)),
    PhpExtensionSpec("enchant", "Enchant", "Optional", "enchant", ("enchant",)),
    PhpExtensionSpec("gmp", "GMP", "Optional", "gmp", ("gmp",)),
    PhpExtensionSpec("imap", "IMAP", "Optional", "imap", ("imap",)),
    PhpExtensionSpec("ldap", "LDAP", "Optional", "ldap", ("ldap",)),
    PhpExtensionSpec("odbc", "ODBC", "Optional", "odbc", ("odbc", "pdo_odbc")),
    PhpExtensionSpec("pspell", "Pspell", "Optional", "pspell", ("pspell",)),
    PhpExtensionSpec("snmp", "SNMP", "Optional", "snmp", ("snmp",)),
    PhpExtensionSpec("soap", "SOAP", "Optional", "soap", ("soap",)),
    PhpExtensionSpec("tidy", "Tidy", "Optional", "tidy", ("tidy",)),
    PhpExtensionSpec(
        "redis",
        "Redis PHP extension",
        "Integrations",
        "redis",
        ("redis",),
        "Separate from the Redis server/redis-cli component on Services & tools.",
    ),
    PhpExtensionSpec(
        "memcached",
        "Memcached PHP extension",
        "Integrations",
        "memcached",
        ("memcached",),
        "Separate from the Memcached system service.",
    ),
    PhpExtensionSpec("imagick", "Imagick", "Integrations", "imagick", ("imagick",)),
    PhpExtensionSpec("amqp", "AMQP", "Integrations", "amqp", ("amqp",)),
    PhpExtensionSpec("igbinary", "Igbinary", "Integrations", "igbinary", ("igbinary",)),
    PhpExtensionSpec("mongodb", "MongoDB", "Integrations", "mongodb", ("mongodb",)),
    PhpExtensionSpec("msgpack", "MessagePack", "Integrations", "msgpack", ("msgpack",)),
    PhpExtensionSpec("smbclient", "SMB Client", "Integrations", "smbclient", ("smbclient",)),
    PhpExtensionSpec("ssh2", "SSH2", "Integrations", "ssh2", ("ssh2",)),
    PhpExtensionSpec("yaml", "YAML", "Integrations", "yaml", ("yaml",)),
    PhpExtensionSpec("pcov", "PCOV", "Debugging", "pcov", ("pcov",)),
    PhpExtensionSpec("xdebug", "Xdebug", "Debugging", "xdebug", ("xdebug",)),
)

EXTENSIONS_BY_KEY = {item.key: item for item in EXTENSIONS}


class PhpExtensionManager:
    """Manage version-scoped Debian/Sury PHP extensions as one CLI+FPM state.

    Package presence and module enablement are intentionally separate states:
    an installed package can remain disabled. NativeDev always enables/disables
    the selected extension for CLI and FPM together; there is no split-SAPI UI.
    Normal refresh never changes module state.
    """

    def __init__(
        self,
        runner: CommandRunner,
        apt: AptManager,
        systemd: SystemdManager,
        php: PhpManager,
        *,
        config_root: Path = Path("/etc/php"),
    ):
        self.runner = runner
        self.apt = apt
        self.systemd = systemd
        self.php = php
        self.config_root = config_root

    def installed_versions(self) -> list[str]:
        return self.php.installed_versions()

    @staticmethod
    def _version_key(version: str) -> tuple[int, int]:
        try:
            major, minor = version.split(".", 1)
            return int(major), int(minor)
        except (TypeError, ValueError):
            return 0, 0

    def spec(self, key: str) -> PhpExtensionSpec:
        try:
            return EXTENSIONS_BY_KEY[key]
        except KeyError as exc:
            raise RuntimeError(f"Unknown PHP extension: {key}") from exc

    def package_name(self, version: str, spec: PhpExtensionSpec | str) -> str:
        item = self.spec(spec) if isinstance(spec, str) else spec
        return f"php{version}-{item.package_suffix}"

    def generic_meta_package(self, spec: PhpExtensionSpec | str) -> str:
        item = self.spec(spec) if isinstance(spec, str) else spec
        return f"php-{item.package_suffix}"

    def is_built_in(self, version: str, spec: PhpExtensionSpec | str) -> bool:
        item = self.spec(spec) if isinstance(spec, str) else spec
        return bool(item.built_in_from and self._version_key(version) >= item.built_in_from)

    def _module_link_exists(self, version: str, sapi: str, module: str) -> bool:
        conf_dir = self.config_root / version / sapi / "conf.d"
        if not conf_dir.is_dir():
            return False
        candidates = list(conf_dir.glob(f"*-{module}.ini"))
        candidates.extend(conf_dir.glob(f"{module}.ini"))
        return any(path.exists() for path in candidates)

    def extension_enabled(self, version: str, spec: PhpExtensionSpec | str) -> bool:
        item = self.spec(spec) if isinstance(spec, str) else spec
        if self.is_built_in(version, item):
            return True
        package = self.package_name(version, item)
        if not self.apt.is_installed(package):
            return False
        return all(
            self._module_link_exists(version, sapi, module)
            for sapi in SAPIS
            for module in item.modules
        )

    def runtime_version(self, version: str) -> str:
        """Return the selected runtime's full PHP_VERSION string when available."""
        if version not in self.php.installed_versions():
            return ""
        binary = Path(f"/usr/bin/php{version}")
        if not binary.is_file():
            return ""
        result = self.runner.run([str(binary), "-r", "echo PHP_VERSION;"], timeout=20)
        return result.stdout.strip() if result.ok else ""

    def is_prerelease(self, version: str) -> bool:
        """Detect alpha/beta/RC/dev runtimes from PHP's own version string."""
        runtime = self.runtime_version(version)
        return bool(re.search(r"(?:alpha|beta|rc|dev)", runtime, re.IGNORECASE))

    def runtime_modules(self, version: str) -> list[str]:
        """Return PHP runtime/common modules that are not separate extension packages.

        NativeDev presents these as read-only ``Built-in`` inventory. The list is
        derived from the selected version instead of a hard-coded PHP-version
        matrix: compiled modules come from ``phpX.Y -n -m`` and modules shipped by
        ``phpX.Y-common`` come from that package's mods-available files.
        """

        if version not in self.php.installed_versions():
            return []

        display_by_key: dict[str, str] = {}

        binary = Path(f"/usr/bin/php{version}")
        if binary.is_file():
            result = self.runner.run([str(binary), "-n", "-m"], timeout=20)
            if result.ok:
                in_php_modules = False
                for raw in result.stdout.splitlines():
                    value = raw.strip()
                    if value == "[PHP Modules]":
                        in_php_modules = True
                        continue
                    if value == "[Zend Modules]":
                        break
                    if not in_php_modules or not value or value == "Core":
                        continue
                    display_by_key.setdefault(value.casefold(), value)

        common_package = f"php{version}-common"
        if self.apt.is_installed(common_package):
            result = self.runner.run(["dpkg-query", "-L", common_package], timeout=30)
            if result.ok:
                prefix = f"/etc/php/{version}/mods-available/"
                for raw in result.stdout.splitlines():
                    path = raw.strip()
                    if not path.startswith(prefix) or not path.endswith(".ini"):
                        continue
                    module = Path(path).stem
                    if module:
                        display_by_key.setdefault(module.casefold(), module)

        # Catalog entries that become part of the runtime in newer PHP versions
        # (currently OPcache from PHP 8.5) belong in the same read-only inventory.
        for spec in EXTENSIONS:
            if self.is_built_in(version, spec):
                for module in spec.modules:
                    display_by_key.setdefault(module.casefold(), module)

        # Package-managed modules have their own rows and must not be duplicated
        # in the runtime/core inventory.
        managed_modules = {
            module.casefold()
            for spec in EXTENSIONS
            if not self.is_built_in(version, spec)
            for module in spec.modules
        }
        return sorted(
            (name for key, name in display_by_key.items() if key not in managed_modules),
            key=str.casefold,
        )

    def states(self, version: str) -> list[PhpExtensionState]:
        if version not in self.php.installed_versions():
            return []
        states: list[PhpExtensionState] = []
        for spec in EXTENSIONS:
            package = self.package_name(version, spec)
            built_in = self.is_built_in(version, spec)
            installed = built_in or self.apt.is_installed(package)
            states.append(
                PhpExtensionState(
                    spec=spec,
                    package=package,
                    installed=installed,
                    enabled=True if built_in else (self.extension_enabled(version, spec) if installed else False),
                    installable=False if built_in else bool(self.apt.candidate(package)),
                    built_in=built_in,
                )
            )
        return states

    def install(self, version: str, key: str) -> None:
        spec = self.spec(key)
        self._require_runtime(version)
        if self.is_built_in(version, spec):
            raise RuntimeError(f"{spec.title} is built into PHP {version}")
        package = self.package_name(version, spec)
        if not self.apt.candidate(package):
            raise RuntimeError(f"{package} is not available from the configured APT repositories")

        was_installed = self.apt.is_installed(package)
        try:
            self.runner.privileged_operation(
                "php.extension_install",
                version=version,
                extension=key,
                check=True,
                timeout=1200,
            )
            self._validate_and_reload_fpm(version)
        except Exception as exc:
            rollback_error = None
            if not was_installed and self.apt.is_installed(package):
                try:
                    self.runner.privileged_operation(
                        "php.extension_remove",
                        version=version,
                        extension=key,
                        check=True,
                        timeout=1200,
                    )
                    if self.php.fpm_config_ready(version):
                        self._validate_and_reload_fpm(version)
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
            if rollback_error is not None:
                raise RuntimeError(
                    f"{spec.title} activation failed ({exc}); package rollback also failed ({rollback_error})"
                ) from exc
            raise

    def enable(self, version: str, key: str) -> None:
        spec = self.spec(key)
        self._require_runtime(version)
        if self.is_built_in(version, spec):
            raise RuntimeError(f"{spec.title} is built into PHP {version}")
        package = self.package_name(version, spec)
        if not self.apt.is_installed(package):
            raise RuntimeError(f"Install {spec.title} for PHP {version} first")
        self._set_enabled(version, spec, True)

    def disable(self, version: str, key: str) -> None:
        spec = self.spec(key)
        self._require_runtime(version)
        if self.is_built_in(version, spec):
            raise RuntimeError(f"{spec.title} is built into PHP {version} and cannot be disabled here")
        package = self.package_name(version, spec)
        if not self.apt.is_installed(package):
            raise RuntimeError(f"{spec.title} is not installed for PHP {version}")
        self._set_enabled(version, spec, False)

    def uninstall(self, version: str, key: str) -> None:
        spec = self.spec(key)
        if self.is_built_in(version, spec):
            raise RuntimeError(f"{spec.title} is built into PHP {version} and cannot be uninstalled")
        package = self.package_name(version, spec)
        if not self.apt.is_installed(package):
            raise RuntimeError(f"{spec.title} is not installed for PHP {version}")
        impact = self.removal_impact(version, key)
        if impact:
            raise RuntimeError(
                f"NativeDev will not uninstall {spec.title} because APT would also remove manually installed package(s): "
                + ", ".join(impact)
            )
        self.runner.privileged_operation(
            "php.extension_remove",
            version=version,
            extension=key,
            check=True,
            timeout=1200,
        )
        if self.php.fpm_config_ready(version):
            self._validate_and_reload_fpm(version)

    def removal_impact(self, version: str, key: str) -> list[str]:
        spec = self.spec(key)
        package = self.package_name(version, spec)
        result = self.runner.run(["apt-get", "-s", "remove", package], timeout=60)
        if not result.ok:
            raise RuntimeError(result.output or f"Could not calculate removal impact for {package}")

        removed: set[str] = set()
        for raw in result.stdout.splitlines():
            match = re.match(r"^Remv\s+(\S+)", raw.strip())
            if match:
                removed.add(match.group(1).split(":", 1)[0])

        manual_result = self.runner.run(["apt-mark", "showmanual"], timeout=30)
        if not manual_result.ok:
            raise RuntimeError(manual_result.output or "Could not read manually installed APT packages")
        manual = {line.strip().split(":", 1)[0] for line in manual_result.stdout.splitlines() if line.strip()}

        # Debian's generic php-<extension> meta package may depend on the selected
        # versioned package. It represents the same extension intent, so its
        # removal is expected rather than an unrelated dependency loss.
        expected = {package, self.generic_meta_package(spec)}
        return sorted((removed & manual).difference(expected))

    def _require_runtime(self, version: str) -> None:
        if version not in self.php.installed_versions():
            raise RuntimeError(f"PHP {version} is not installed")
        if not self.apt.is_installed(f"php{version}-cli"):
            raise RuntimeError(f"PHP {version} CLI is not installed")
        if not self.apt.is_installed(f"php{version}-fpm") or not self.php.fpm_config_ready(version):
            raise RuntimeError(f"PHP {version} FPM is not ready; repair/install FPM before managing extensions")

    def _set_enabled(self, version: str, spec: PhpExtensionSpec, enabled: bool) -> None:
        action = "php.extension_enable" if enabled else "php.extension_disable"
        rollback_action = "php.extension_disable" if enabled else "php.extension_enable"

        # The root helper applies CLI+FPM together and restores the pre-action
        # module links if phpenmod/phpdismod itself fails. Only a later FPM
        # validation/reload failure needs a manager-side opposite-action rollback.
        self.runner.privileged_operation(
            action,
            version=version,
            extension=spec.key,
            check=True,
            timeout=120,
        )
        try:
            self._validate_and_reload_fpm(version)
        except Exception as exc:
            rollback_error = None
            try:
                self.runner.privileged_operation(
                    rollback_action,
                    version=version,
                    extension=spec.key,
                    check=True,
                    timeout=120,
                )
                self._validate_and_reload_fpm(version)
            except Exception as rollback_exc:
                rollback_error = rollback_exc
            if rollback_error is not None:
                raise RuntimeError(
                    f"Could not {'enable' if enabled else 'disable'} {spec.title} for PHP {version} ({exc}); "
                    f"rollback also failed ({rollback_error})"
                ) from exc
            raise

    def _validate_and_reload_fpm(self, version: str) -> None:
        if not self.php.fpm_config_ready(version):
            raise RuntimeError(f"PHP {version} FPM configuration is missing")
        self.runner.run(
            [f"php-fpm{version}", "-t"],
            privileged=True,
            check=True,
            timeout=30,
        )
        service = f"php{version}-fpm"
        if not self.systemd.is_active(service):
            return
        try:
            self.systemd.reload(service)
        except RuntimeError:
            self.systemd.restart(service)
