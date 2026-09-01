from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
import tempfile
import urllib.request
from pathlib import Path

from ..system import AptManager, CommandRunner, DistroInfo, SystemdManager


SURY_KEYRING_URL = "https://packages.sury.org/debsuryorg-archive-keyring.deb"
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
DEFAULT_EXTENSIONS = ("curl", "mbstring", "xml", "zip", "intl", "bcmath")


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

        with tempfile.TemporaryDirectory(prefix="nativedev-sury-", dir="/tmp") as temp_dir:
            keyring_deb = Path(temp_dir) / "debsuryorg-archive-keyring.deb"
            urllib.request.urlretrieve(SURY_KEYRING_URL, keyring_deb)
            self.runner.run(
                ["apt-get", "install", "-y", str(keyring_deb)],
                privileged=True,
                check=True,
                timeout=600,
            )

            source = (
                "Types: deb\n"
                "URIs: https://packages.sury.org/php/\n"
                f"Suites: {self.distro.codename}\n"
                "Components: main\n"
                "Signed-By: /usr/share/keyrings/debsuryorg-archive-keyring.gpg\n"
            )
            source_tmp = Path(temp_dir) / "nativedev-sury-php.sources"
            source_tmp.write_text(source, encoding="utf-8")
            self.runner.run(
                ["install", "-m", "0644", str(source_tmp), str(SURY_SOURCE_FILE)],
                privileged=True,
                check=True,
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
        self.systemd.disable(f"php{version}-fpm")

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

    def uninstall_version(self, version: str) -> None:
        result = self.runner.run(["dpkg-query", "-W", "-f=${Package}\n"], timeout=45)
        prefix = f"php{version}-"
        packages = sorted(
            package.strip()
            for package in result.stdout.splitlines()
            if package.strip().startswith(prefix) and self.apt.is_installed(package.strip())
        ) if result.ok else []
        if not packages:
            raise RuntimeError(f"PHP {version} is not installed")

        # Remove only NativeDev's own pool before the package disappears.
        self.remove_developer_pool(version, restart=False)
        try:
            self.systemd.disable_now(f"php{version}-fpm")
        except RuntimeError:
            pass
        self.apt.remove(packages)

    def _package_status_abbrev(self, package: str) -> str:
        result = self.runner.run(
            ["dpkg-query", "-W", "-f=${db:Status-Abbrev}", package],
            timeout=15,
        )
        return result.stdout.strip() if result.ok else ""

    def _purge_stale_package_state(self, packages: list[str]) -> None:
        """Forget ``rc`` conffile state before a fresh PHP installation.

        ``apt remove`` intentionally leaves package configuration behind.  If a
        developer later deletes /etc/php manually, dpkg can remember those
        deleted conffiles and preserve the deletion on reinstall.  Purging only
        packages that are already in ``rc`` state is non-destructive to running
        packages and lets a fresh install recreate its package-owned defaults.
        """
        stale = [package for package in packages if self._package_status_abbrev(package).startswith("rc")]
        if stale:
            self.runner.run(
                ["apt-get", "purge", "-y", *stale],
                privileged=True,
                check=True,
                timeout=1200,
            )

    def repair_fpm(self, version: str) -> None:
        """Restore a broken/missing distro FPM configuration explicitly.

        NativeDev does not fabricate Debian/Sury's master FPM configuration.
        If dpkg says FPM is installed while php-fpm.conf is missing, purge and
        reinstall only that FPM package so its package-owned defaults are
        recreated.  Other PHP packages and versions are left untouched.
        """
        package = f"php{version}-fpm"
        if not self.apt.candidate(package):
            raise RuntimeError(f"{package} is not available from configured APT repositories")

        was_installed = self.apt.is_installed(package)
        was_enabled = False
        if was_installed:
            enabled_state = self.fpm_enabled_state(version)
            was_enabled = enabled_state in {"enabled", "enabled-runtime", "linked", "linked-runtime"}
            was_running = self.systemd.is_active(f"php{version}-fpm")
            self.runner.run(
                ["apt-get", "purge", "-y", package],
                privileged=True,
                check=True,
                timeout=1200,
            )
        else:
            was_running = False

        self.apt.install([package])
        if not self.fpm_master_config_file(version).is_file():
            raise RuntimeError(
                f"{package} was reinstalled but {self.fpm_master_config_file(version)} is still missing"
            )

        # Preserve explicit service policy where possible.  A package reinstall
        # may enable/start the unit by default even if the user had disabled or
        # stopped it before repair.
        if was_installed and not was_enabled:
            self.systemd.disable(f"php{version}-fpm")
        elif was_enabled:
            self.systemd.enable(f"php{version}-fpm")
        if was_installed and not was_running:
            self.systemd.stop(f"php{version}-fpm")
        elif was_running:
            self.systemd.start(f"php{version}-fpm")

    def install_version(self, version: str) -> None:
        if version not in self.available_versions():
            raise RuntimeError(f"PHP {version} is not available from configured APT repositories")

        packages = [f"php{version}-cli", f"php{version}-fpm", f"php{version}-common"]
        packages.extend(
            f"php{version}-{ext}"
            for ext in DEFAULT_EXTENSIONS
            if self.apt.candidate(f"php{version}-{ext}")
        )

        # Clear stale conffile bookkeeping for packages that were removed but
        # not purged before installing them again.
        self._purge_stale_package_state(packages)
        self.apt.install(packages)

        # A package can be in dpkg's installed state while its conffile tree is
        # absent. Repair that state before touching systemd or NativeDev pools.
        if not self.fpm_config_ready(version):
            self.repair_fpm(version)

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
