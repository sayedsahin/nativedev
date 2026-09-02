from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
import tempfile
from pathlib import Path

from ..system import AptManager, CommandRunner, DistroInfo, SystemdManager


SURY_SOURCE_FILE = Path("/etc/apt/sources.list.d/nativedev-sury-php.sources")
APT_SOURCES_LIST = Path("/etc/apt/sources.list")
APT_SOURCES_DIR = Path("/etc/apt/sources.list.d")
SURY_SUPPORTED_CODENAMES = {
    "bullseye",
    "bookworm",
    "trixie",
    "jammy",
    "noble",
    "resolute",
}
# NativeDev installs a practical local-development baseline rather than only the
# minimum PHP runtime. These cover Laravel/Symfony requirements plus the common
# database/image/archive modules used by typical projects. Core/common modules
# such as ctype, fileinfo, iconv, PDO, tokenizer and OpenSSL come from PHP itself
# or phpX.Y-common on Debian/Sury.
DEVELOPMENT_EXTENSIONS = (
    "bcmath",
    "curl",
    "gd",
    "intl",
    "mbstring",
    "mysql",
    "pgsql",
    "readline",
    "sqlite3",
    "xml",
    "zip",
)
# Debian/Sury package names do not always map one-to-one to PHP module names.
# NativeDev enables this known framework-development baseline only when a PHP
# version is installed. It never re-enables modules during refresh/startup, so
# a developer remains free to disable individual modules afterwards.
DEVELOPMENT_MODULES = (
    "bcmath",
    "curl",
    "gd",
    "intl",
    "mbstring",
    "mysqlnd",
    "mysqli",
    "pdo_mysql",
    "pgsql",
    "pdo_pgsql",
    "readline",
    "sqlite3",
    "pdo_sqlite",
    "dom",
    "simplexml",
    "xml",
    "xmlreader",
    "xmlwriter",
    "xsl",
    "zip",
)
OPCACHE_BUILT_IN_FROM = (8, 5)

DEBIAN_BASE_PACKAGES = ("php-cli", "php-fpm", "php-common")


class PhpManager:
    def __init__(
        self,
        runner: CommandRunner,
        apt: AptManager,
        systemd: SystemdManager,
        distro: DistroInfo,
    ):
        self.runner = runner
        self.apt = apt
        self.systemd = systemd
        self.distro = distro
        self.developer_uid = os.getuid()
        self.developer_gid = os.getgid()
        self.developer_user = pwd.getpwuid(self.developer_uid).pw_name
        self.developer_group = grp.getgrgid(self.developer_gid).gr_name

    def sury_configured(self) -> bool:
        """Return True only when an *active* binary Sury APT source exists.

        APT source directories commonly retain editor backups, ``*.save`` files,
        disabled deb822 stanzas, or old commented-out ``deb`` lines.  Merely
        finding the Sury URL anywhere below sources.list.d therefore produces
        false positives after the repository has been removed.

        NativeDev mirrors what APT actually considers a configured source:
        ``/etc/apt/sources.list`` plus ``*.list`` and ``*.sources`` files only,
        ignoring comments, source-only entries and deb822 stanzas with
        ``Enabled: no``.
        """
        candidates: list[Path] = []
        if APT_SOURCES_LIST.is_file():
            candidates.append(APT_SOURCES_LIST)
        try:
            candidates.extend(sorted(APT_SOURCES_DIR.glob("*.list")))
            candidates.extend(sorted(APT_SOURCES_DIR.glob("*.sources")))
        except OSError:
            pass

        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            if path.suffix == ".sources":
                if self._deb822_has_active_sury(text):
                    return True
            elif self._list_has_active_sury(text):
                return True
        return False

    @staticmethod
    def _is_sury_uri(value: str) -> bool:
        return any(
            uri.rstrip("/") == "https://packages.sury.org/php"
            or uri.rstrip("/") == "http://packages.sury.org/php"
            for uri in value.split()
        )

    @classmethod
    def _list_has_active_sury(cls, text: str) -> bool:
        for raw in text.splitlines():
            # Legacy one-line sources: only an uncommented binary ``deb`` entry
            # enables installation. ``deb-src`` alone is not enough.
            line = raw.split("#", 1)[0].strip()
            if not line or not re.match(r"^deb(?:\s|$)", line):
                continue
            if "packages.sury.org/php" in line:
                return True
        return False

    @classmethod
    def _deb822_has_active_sury(cls, text: str) -> bool:
        # Deb822 source files are split into blank-line-separated stanzas.
        # Continuation lines are folded into the preceding field so normal APT
        # multiline URIs/Types values are handled as well.
        for stanza_text in re.split(r"\n\s*\n", text):
            fields: dict[str, str] = {}
            current: str | None = None
            for raw in stanza_text.splitlines():
                if not raw.strip() or raw.lstrip().startswith("#"):
                    continue
                if raw[:1].isspace() and current:
                    fields[current] = f"{fields[current]} {raw.strip()}".strip()
                    continue
                if ":" not in raw:
                    current = None
                    continue
                key, value = raw.split(":", 1)
                current = key.strip().lower()
                fields[current] = value.strip()

            enabled = fields.get("enabled", "yes").strip().lower()
            if enabled in {"no", "false", "0"}:
                continue
            types = set(fields.get("types", "").split())
            if "deb" not in types:
                continue
            if cls._is_sury_uri(fields.get("uris", "")):
                return True
        return False

    def additional_sury_sources_configured(self) -> list[str]:
        """Return active Sury source files not owned by NativeDev.

        NativeDev may remove only its own repository file. Provider migration
        back to Debian is blocked while another Sury source remains active,
        otherwise APT could immediately select Sury packages again.
        """
        paths: list[Path] = []
        if APT_SOURCES_LIST.is_file():
            paths.append(APT_SOURCES_LIST)
        try:
            paths.extend(sorted(APT_SOURCES_DIR.glob("*.list")))
            paths.extend(sorted(APT_SOURCES_DIR.glob("*.sources")))
        except OSError:
            pass

        found: list[str] = []
        for path in paths:
            try:
                if path.resolve() == SURY_SOURCE_FILE.resolve():
                    continue
            except OSError:
                pass
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            active = self._deb822_has_active_sury(text) if path.suffix == ".sources" else self._list_has_active_sury(text)
            if active:
                found.append(str(path))
        return found

    def installed_package_provider(self, package: str) -> str:
        """Resolve the repository that supplied the installed package version."""
        if not self.apt.is_installed(package):
            return "none"
        result = self.runner.run(["apt-cache", "policy", package], timeout=30)
        if not result.ok:
            return "unknown"
        installed = ""
        lines = result.stdout.splitlines()
        for raw in lines:
            stripped = raw.strip()
            if stripped.startswith("Installed:"):
                installed = stripped.partition(":")[2].strip()
                break
        if not installed or installed == "(none)":
            return "none"

        for index, raw in enumerate(lines):
            stripped = raw.strip()
            if not stripped.startswith("*** "):
                continue
            parts = stripped.split()
            if len(parts) < 3 or parts[1] != installed:
                continue
            for following in lines[index + 1 :]:
                item = following.strip()
                parts2 = item.split()
                if item.startswith("*** ") or (len(parts2) == 2 and parts2[-1].isdigit()):
                    break
                if "packages.sury.org/php" in item:
                    return "sury"
            return "debian"
        return "unknown"

    def provider(self) -> str:
        """Return the NativeDev PHP mode: ``sury``, ``debian`` or ``none``.

        Provider selection is intentionally one-way in the UI. Once any active
        Sury source exists, NativeDev is in multi-PHP mode and never offers a
        Debian-provider switch. Installed package origin is still inspected for
        migration diagnostics, but it does not turn Sury mode back into a mixed
        or Debian-selectable state.
        """
        if self.sury_configured():
            return "sury"
        if self.installed_versions():
            return "debian"
        return "none"

    def sury_migration_needed(self) -> bool:
        """Return True when Sury mode still has non-Sury PHP runtimes installed."""
        if not self.sury_configured():
            return False
        for version in self.installed_versions():
            package = f"php{version}-cli" if self.apt.is_installed(f"php{version}-cli") else f"php{version}-fpm"
            if self.installed_package_provider(package) != "sury":
                return True
        return False

    @property
    def sury_supported(self) -> bool:
        return self.distro.codename in SURY_SUPPORTED_CODENAMES

    def configure_sury(self, *, explicit: bool = False) -> None:
        """Install/configure Sury only after an explicit user request.

        Sury is optional in NativeDev.  Debian's own PHP packages remain fully
        manageable without it.  Requiring an explicit flag here makes it
        impossible for refresh/discovery or a future convenience path to
        silently opt the host into a third-party APT repository.
        """
        if not explicit:
            raise RuntimeError("Sury setup requires an explicit user action")
        if self.sury_configured():
            return
        if not self.sury_supported:
            raise RuntimeError(
                f"Sury PHP repository is not known to publish suite '{self.distro.codename}'."
            )

        # The helper owns the entire privileged repository setup: it downloads
        # the fixed Sury keyring URL and writes a fixed DEB822 template for the
        # validated distro suite. No client-supplied .deb path or APT source
        # content crosses the privilege boundary.
        self.runner.privileged_operation(
            "sury.configure",
            codename=self.distro.codename,
            check=True,
            timeout=600,
        )
        self.apt.refresh()

    def available_versions(self) -> list[str]:
        # Parallel version discovery is a Sury feature on Debian-family hosts.
        # Without Sury configured, keep already-installed PHP manageable but do
        # not present Debian's single-version metadata as a parallel PHP catalog.
        if not self.sury_configured():
            return []
        result = self.runner.run(["apt-cache", "pkgnames"], timeout=45)
        versions: set[str] = set()
        if result.ok:
            for package in result.stdout.splitlines():
                match = re.fullmatch(r"php(\d+\.\d+)-fpm", package.strip())
                if match and self.apt.candidate(package.strip()):
                    versions.add(match.group(1))
        return sorted(versions, key=self._version_key, reverse=True)

    def installed_versions(self) -> list[str]:
        """Return versions with an actually installed CLI or FPM package.

        ``dpkg-query -W`` also reports packages in states such as ``rc``
        (removed, config-files remain).  Those packages must not make NativeDev
        render a PHP version as installed.  Require dpkg's ``ii`` state.
        """
        result = self.runner.run(
            ["dpkg-query", "-W", "-f=${db:Status-Abbrev}\t${binary:Package}\n"],
            timeout=45,
        )
        versions: set[str] = set()
        if result.ok:
            for line in result.stdout.splitlines():
                status, separator, package = line.partition("\t")
                if not separator or not status.startswith("ii "):
                    continue
                # binary:Package can carry an architecture qualifier for some
                # multi-arch packages; it is irrelevant to PHP version parsing.
                package = package.strip().split(":", 1)[0]
                match = re.fullmatch(r"php(\d+\.\d+)-(?:cli|fpm)", package)
                if match:
                    versions.add(match.group(1))
        return sorted(versions, key=self._version_key, reverse=True)

    def installed_fpm_versions(self) -> list[str]:
        return [version for version in self.installed_versions() if self.apt.is_installed(f"php{version}-fpm")]

    def cli_version(self) -> str:
        """Return the current CLI PHP major.minor version, or an empty string.

        A machine with no PHP installed is a normal NativeDev state.  Check PATH
        before invoking the runner so PHP discovery never depends on subprocess
        error handling merely to decide whether the runtime exists.
        """
        php_binary = shutil.which("php")
        if not php_binary:
            return ""
        try:
            result = self.runner.run(
                [php_binary, "-r", "echo PHP_MAJOR_VERSION.'.'.PHP_MINOR_VERSION;"],
                timeout=10,
            )
        except OSError:
            # PHP may disappear between which() and exec() during package
            # removal. Treat that race exactly like a clean no-PHP machine.
            return ""
        return result.stdout.strip() if result.ok else ""

    def default_fpm_version(self) -> str:
        """Resolve the PHP-FPM version used by projects set to Default.

        Prefer the system CLI default when its matching FPM package exists; if
        the CLI points at a version without FPM, fall back to the newest
        installed FPM version rather than requiring a separate global setting.
        """
        cli = self.cli_version()
        versions = self.installed_fpm_versions()
        if cli in versions:
            return cli
        return versions[0] if versions else ""

    def fpm_master_config_file(self, version: str) -> Path:
        return Path(f"/etc/php/{version}/fpm/php-fpm.conf")

    def fpm_config_ready(self, version: str) -> bool:
        """Return whether an installed FPM package has its package config.

        dpkg can report ``phpX.Y-fpm`` as installed even when the user has
        manually removed /etc/php/X.Y/fpm.  systemd cannot start that state, so
        package presence and FPM readiness must be tracked separately.
        """
        return self.apt.is_installed(f"php{version}-fpm") and self.fpm_master_config_file(version).is_file()

    def fpm_running(self, version: str) -> bool:
        return self.fpm_config_ready(version) and self.systemd.is_active(f"php{version}-fpm")

    def fpm_enabled_state(self, version: str) -> str:
        return self.systemd.enabled_state(f"php{version}-fpm")

    def fpm_enabled(self, version: str) -> bool:
        return self.fpm_enabled_state(version) in {"enabled", "enabled-runtime", "linked", "linked-runtime"}

    def start_fpm(self, version: str) -> None:
        if not self.fpm_config_ready(version):
            raise RuntimeError(f"PHP {version} FPM configuration is missing. Repair FPM first.")
        self.systemd.start(f"php{version}-fpm")

    def stop_fpm(self, version: str) -> None:
        self.systemd.stop(f"php{version}-fpm")

    def restart_fpm(self, version: str) -> None:
        if not self.fpm_config_ready(version):
            raise RuntimeError(f"PHP {version} FPM configuration is missing. Repair FPM first.")
        self.systemd.restart(f"php{version}-fpm")

    def enable_fpm(self, version: str) -> None:
        self.systemd.enable(f"php{version}-fpm")

    def disable_fpm(self, version: str) -> None:
        # systemctl disable only changes boot policy; it leaves the current FPM
        # master running, so existing *.test sockets keep serving requests. In
        # NativeDev the UI action means "disable this FPM runtime", therefore
        # stop it immediately as well as disabling autostart. CLI PHP is a
        # separate runtime and intentionally remains available.
        self.systemd.disable_now(f"php{version}-fpm")

    # ---- NativeDev per-user FPM pool -------------------------------------
    # We deliberately do not modify Debian/Sury's [www] pool. NativeDev owns a
    # separate pool for every PHP version, and the workers run as the logged-in
    # developer. This keeps CLI-created and web-created application files under
    # the same Unix user while Nginx stays on Debian's www-data account.

    def developer_pool_name(self) -> str:
        return f"nativedev-{self.developer_uid}"

    def developer_pool_file(self, version: str) -> Path:
        return Path(f"/etc/php/{version}/fpm/pool.d/{self.developer_pool_name()}.conf")

    def developer_socket_path(self, version: str) -> Path:
        return Path(f"/run/php/php{version}-fpm-{self.developer_pool_name()}.sock")

    def developer_pool_configured(self, version: str) -> bool:
        return self.developer_pool_file(version).is_file()

    def render_developer_pool(self, version: str) -> str:
        # Names come from the local passwd/group databases, not user input.
        user = self.developer_user.replace("\n", "").replace("\r", "")
        group = self.developer_group.replace("\n", "").replace("\r", "")
        socket_path = self.developer_socket_path(version)
        return (
            f"; Managed by NativeDev for local development only.\n"
            f"; System pool /etc/php/{version}/fpm/pool.d/www.conf is not modified.\n"
            f"[{self.developer_pool_name()}]\n"
            f"user = {user}\n"
            f"group = {group}\n\n"
            f"listen = {socket_path}\n"
            "listen.owner = www-data\n"
            "listen.group = www-data\n"
            "listen.mode = 0660\n\n"
            "pm = ondemand\n"
            "pm.max_children = 12\n"
            "pm.process_idle_timeout = 10s\n"
            "pm.max_requests = 500\n\n"
            "; Keep worker output visible in the normal PHP-FPM log.\n"
            "catch_workers_output = yes\n"
        )

    def ensure_developer_pool(self, version: str) -> None:
        if not self.apt.is_installed(f"php{version}-fpm"):
            raise RuntimeError(f"PHP {version} FPM is not installed")
        if not self.fpm_config_ready(version):
            raise RuntimeError(f"PHP {version} FPM configuration is missing. Repair FPM first.")

        target = self.developer_pool_file(version)
        if not target.parent.is_dir():
            raise RuntimeError(f"PHP {version} FPM pool directory does not exist: {target.parent}")

        content = self.render_developer_pool(version)
        try:
            current = target.read_text(encoding="utf-8") if target.exists() else None
        except OSError:
            current = None

        changed = current != content
        if changed:
            with tempfile.TemporaryDirectory(prefix="nativedev-fpm-", dir="/tmp") as temp_dir:
                temp = Path(temp_dir)
                source = temp / target.name
                source.write_text(content, encoding="utf-8")
                self.runner.run(
                    ["install", "-m", "0644", str(source), str(target)],
                    privileged=True,
                    check=True,
                )

                # Validate the entire FPM configuration before restarting. If
                # validation fails, restore exactly what was present before.
                check = self.runner.run(
                    [f"php-fpm{version}", "-tt"],
                    privileged=True,
                    timeout=30,
                )
                if not check.ok:
                    if current is None:
                        self.runner.run(["rm", "-f", str(target)], privileged=True)
                    else:
                        rollback = temp / "rollback.conf"
                        rollback.write_text(current, encoding="utf-8")
                        self.runner.run(
                            ["install", "-m", "0644", str(rollback), str(target)],
                            privileged=True,
                        )
                    raise RuntimeError(check.output or f"PHP {version} FPM configuration validation failed")

            self.systemd.restart(f"php{version}-fpm")
        elif not self.fpm_running(version):
            self.systemd.start(f"php{version}-fpm")

    def remove_developer_pool(self, version: str, *, restart: bool = True) -> None:
        target = self.developer_pool_file(version)
        if not target.exists():
            return
        self.runner.run(["rm", "-f", str(target)], privileged=True, check=True)
        if restart and self.apt.is_installed(f"php{version}-fpm") and self.fpm_running(version):
            self.systemd.restart(f"php{version}-fpm")

    def installed_version_packages(self, version: str) -> list[str]:
        """Return every installed version-scoped PHP package.

        This intentionally includes extension/dev/debug packages that may have
        been added after NativeDev installed the version. Uninstalling a PHP
        version should not leave phpX.Y-* extension packages behind. The bare
        phpX.Y metapackage is included as well when present.
        """
        result = self.runner.run(
            ["dpkg-query", "-W", "-f=${db:Status-Abbrev}\t${binary:Package}\n"],
            timeout=45,
        )
        if not result.ok:
            return []

        pattern = re.compile(rf"^php{re.escape(version)}(?:$|-)")
        packages: set[str] = set()
        for line in result.stdout.splitlines():
            status, separator, package = line.partition("\t")
            if not separator or not status.startswith("ii "):
                continue
            package = package.strip().split(":", 1)[0]
            if pattern.match(package):
                packages.add(package)
        return sorted(packages)

    def uninstall_version(self, version: str) -> None:
        packages = self.installed_version_packages(version)
        if not packages:
            raise RuntimeError(f"PHP {version} is not installed")

        # Remove only NativeDev's own pool before the package disappears.
        self.remove_developer_pool(version, restart=False)
        try:
            self.systemd.disable_now(f"php{version}-fpm")
        except RuntimeError:
            pass
        self.apt.remove(packages)

    def repair_fpm(self, version: str) -> None:
        """Restore missing distro-owned FPM conffiles without purging user config.

        Debian/dpkg intentionally preserves locally modified conffiles.  The
        ``--force-confmiss`` option restores only package conffiles that are
        missing, while ``--reinstall`` refreshes the package payload. Existing
        custom FPM configuration is not purged or replaced.
        """
        package = f"php{version}-fpm"
        if not self.apt.candidate(package):
            raise RuntimeError(f"{package} is not available from configured APT repositories")

        was_installed = self.apt.is_installed(package)
        was_enabled = False
        was_running = False
        if was_installed:
            enabled_state = self.fpm_enabled_state(version)
            was_enabled = enabled_state in {"enabled", "enabled-runtime", "linked", "linked-runtime"}
            was_running = self.systemd.is_active(f"php{version}-fpm")

            self.runner.run(
                [
                    "apt-get",
                    "install",
                    "--reinstall",
                    "-y",
                    "-o",
                    "Dpkg::Options::=--force-confmiss",
                    package,
                ],
                privileged=True,
                check=True,
                timeout=1200,
            )
        else:
            # This path is mainly defensive; the GUI offers Repair only for an
            # installed package. A normal install needs no destructive cleanup.
            self.apt.install([package])

        if not self.fpm_master_config_file(version).is_file():
            raise RuntimeError(
                f"{package} was reinstalled but {self.fpm_master_config_file(version)} is still missing"
            )

        # Package maintainer scripts may start/enable the unit during reinstall.
        # Restore the user's prior service policy when this was a repair.
        if was_installed and not was_enabled:
            self.systemd.disable(f"php{version}-fpm")
        elif was_enabled:
            self.systemd.enable(f"php{version}-fpm")
        if was_installed and not was_running:
            self.systemd.stop(f"php{version}-fpm")
        elif was_running:
            self.systemd.start(f"php{version}-fpm")

    def remove_sury_repository(self) -> None:
        external = self.additional_sury_sources_configured()
        if external:
            raise RuntimeError(
                "Cannot switch PHP provider to Debian while another Sury source is active: "
                + ", ".join(external)
            )
        if SURY_SOURCE_FILE.exists():
            self.runner.run(["rm", "-f", str(SURY_SOURCE_FILE)], privileged=True, check=True)
            self.apt.refresh()
        if self.sury_configured():
            raise RuntimeError("Sury is still configured from a source NativeDev does not own")

    def generic_development_packages(self) -> list[str]:
        extensions = list(DEVELOPMENT_EXTENSIONS) + ["opcache"]
        packages: list[str] = []
        for extension in extensions:
            package = f"php-{extension}"
            if self.apt.candidate(package):
                packages.append(package)
        return packages

    def debian_default_version(self) -> str:
        """Resolve Debian's current php-cli dependency after repository selection."""
        result = self.runner.run(["apt-cache", "depends", "php-cli"], timeout=30)
        if result.ok:
            for raw in result.stdout.splitlines():
                match = re.search(r"Depends:\s*php(\d+\.\d+)-cli", raw)
                if match:
                    return match.group(1)
        return ""

    def install_debian_default(self, *, allow_downgrades: bool = False) -> str:
        target = self.debian_default_version()
        packages = [package for package in DEBIAN_BASE_PACKAGES if self.apt.candidate(package)]
        packages.extend(self.generic_development_packages())
        if not {"php-cli", "php-fpm"}.issubset(packages):
            raise RuntimeError("Debian PHP CLI/FPM packages are not available from configured repositories")
        self.apt.install_php(packages, allow_downgrades=allow_downgrades)
        version = target or self.cli_version()
        if not version:
            raise RuntimeError("Debian PHP was installed but no default CLI version could be detected")
        # A provider migration can leave update-alternatives pointing at a still-
        # installed Sury-only version. Make Debian's resolved runtime explicit.
        if Path(f"/usr/bin/php{version}").exists():
            self.set_cli_default(version)
        if not self.fpm_config_ready(version):
            self.repair_fpm(version)
        self.enable_development_modules(version)
        self.systemd.enable_now(f"php{version}-fpm")
        self.ensure_developer_pool(version)
        return version

    def enable_sury_multi_php(self) -> str:
        """Enable Sury multi-PHP and migrate existing Debian PHP runtimes.

        Debian and Sury publish the same versioned PHP package names, so a
        literal uninstall-first migration can remove reverse dependants such as
        Composer. NativeDev therefore replaces/reinstalls those package names
        from the Sury candidate in place. From the user's perspective the old
        Debian provider is retired: once Sury is active, no Debian-provider
        install/switch action is exposed.
        """
        previous = self.default_fpm_version() or self.cli_version()
        installed_before = list(self.installed_versions())

        # Debian and Sury intentionally use the same versioned package names.
        # Configure Sury first, then reinstall each compatible runtime so APT
        # replaces/upgrades it in place. This preserves reverse dependencies
        # such as Debian's composer package that could be removed by an
        # uninstall-first migration.
        had_sury = self.sury_configured()
        self.configure_sury(explicit=True)
        available = self.available_versions()
        if not available:
            if not had_sury:
                self.remove_sury_repository()
            raise RuntimeError("Sury is configured but no PHP-FPM versions are available")

        if installed_before:
            missing = [version for version in installed_before if version not in available]
            if missing:
                if not had_sury:
                    self.remove_sury_repository()
                raise RuntimeError(
                    "Sury does not provide the currently installed PHP version(s): "
                    + ", ".join(missing)
                )
            for version in installed_before:
                self.install_version(version)
            target = previous if previous in installed_before else installed_before[0]
            self.set_cli_default(target)
            return target

        target = available[0]
        self.install_version(target)
        self.set_cli_default(target)
        return target

    def development_extension_packages(self, version: str) -> list[str]:
        """Resolve NativeDev's framework-friendly extension set for one PHP version.

        PHP 8.5 made OPcache mandatory/built-in, so Debian/Sury no longer needs
        (or publishes) a separate php8.5-opcache package. Keep the explicit
        package for older versions where it exists. Every optional package is
        candidate-checked so the same code works across supported Debian-family
        repositories without turning a missing non-core extension into a failed
        PHP installation.
        """
        extensions = list(DEVELOPMENT_EXTENSIONS)
        if self._version_key(version) < OPCACHE_BUILT_IN_FROM:
            extensions.append("opcache")

        packages: list[str] = []
        for extension in extensions:
            package = f"php{version}-{extension}"
            if self.apt.candidate(package):
                packages.append(package)
        return packages

    def development_modules(self, version: str) -> list[str]:
        modules = list(DEVELOPMENT_MODULES)
        if self._version_key(version) < OPCACHE_BUILT_IN_FROM:
            modules.append("opcache")
        return modules

    def enable_development_modules(self, version: str) -> None:
        """Enable NativeDev's baseline for CLI and FPM after installation only.

        Debian/Sury deliberately keeps module enablement separate from package
        installation. A package may therefore be installed while its conf.d
        symlinks remain absent after a previous phpdismod. NativeDev promises a
        ready-to-use local development stack after *Install*, so explicitly
        enable the baseline at that point. Normal refresh/start/stop operations
        never call this method and therefore never override later user choices.
        """
        modules = self.development_modules(version)
        for sapi in ("cli", "fpm"):
            self.runner.privileged_operation(
                "php.enable_modules",
                version=version,
                sapi=sapi,
                modules=modules,
                check=True,
                timeout=120,
            )

    def install_version(self, version: str) -> None:
        if version not in self.available_versions():
            raise RuntimeError(f"PHP {version} is not available from configured APT repositories")

        packages = [f"php{version}-cli", f"php{version}-fpm", f"php{version}-common"]
        packages.extend(self.development_extension_packages(version))

        # PHP module .ini definitions are UCF-managed on Debian/Sury. A normal
        # reinstall honors a previously deleted /etc/php/<version>/mods-available
        # file, which would leave the package installed but make phpenmod report
        # "No module matches". The PHP-specific install path restores only
        # missing UCF-managed definitions, without overwriting existing custom ini.
        self.apt.install_php(packages)

        # A package can be in dpkg's installed state while its conffile tree is
        # absent. Repair that state before touching systemd or NativeDev pools.
        if not self.fpm_config_ready(version):
            self.repair_fpm(version)

        # Package installation alone does not guarantee enabled modules on
        # Debian/Sury (for example after a prior phpdismod). Enforce NativeDev's
        # framework baseline once, specifically as part of Install.
        self.enable_development_modules(version)

        self.systemd.enable_now(f"php{version}-fpm")
        self.ensure_developer_pool(version)

    def set_cli_default(self, version: str) -> None:
        binary = Path(f"/usr/bin/php{version}")
        if not binary.exists():
            raise RuntimeError(f"{binary} does not exist")
        self.runner.run(
            ["update-alternatives", "--set", "php", str(binary)],
            privileged=True,
            check=True,
        )

    @staticmethod
    def _version_key(version: str) -> tuple[int, int]:
        try:
            major, minor = version.split(".", 1)
            return int(major), int(minor)
        except ValueError:
            return 0, 0
