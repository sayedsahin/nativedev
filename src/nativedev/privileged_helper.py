#!/usr/bin/python3
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import struct
import subprocess
import shutil
import tempfile
import urllib.request
from pathlib import Path

PROTOCOL_VERSION = 9
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"

MANAGED_FILES = {
    "/etc/apt/sources.list.d/nativedev-sury-php.sources",
    "/etc/NetworkManager/conf.d/nativedev-dns.conf",
    "/etc/NetworkManager/dnsmasq.d/nativedev-test.conf",
    "/etc/nginx/sites-available/nativedev-sites.conf",
    "/etc/nginx/sites-enabled/nativedev-sites.conf",
    "/etc/nginx/nativedev/nativedev.pem",
    "/etc/nginx/nativedev/nativedev-key.pem",
}
MANAGED_DIRS = {
    "/etc/NetworkManager/conf.d",
    "/etc/NetworkManager/dnsmasq.d",
    "/etc/nginx/nativedev",
}
SERVICE_RE = re.compile(
    r"^(?:nginx|redis-server|memcached|mariadb|mysql|postgresql|php\d+\.\d+-fpm)(?:\.service)?$"
)
PHP_PACKAGE_RE = re.compile(r"^php\d+\.\d+(?:-[A-Za-z0-9][A-Za-z0-9.+~_-]*)?$")
PHP_FPM_PACKAGE_RE = re.compile(r"^php\d+\.\d+-fpm$")
VERSION_RE = re.compile(r"^\d+\.\d+$")
PHP_INI_DIRECTIVE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.]*$")
PHP_INI_BLOCKED_DIRECTIVES = frozenset({"extension", "zend_extension", "extension_dir"})
PHP_INI_MAX_SETTINGS = 128
PHP_INI_MAX_DIRECTIVE_LENGTH = 128
PHP_INI_MAX_VALUE_LENGTH = 4096
PHP_CONFIG_ROOT = Path("/etc/php")
PHP_MODULE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
POSTGRESQL_RUNTIME_PACKAGE_RE = re.compile(r"^postgresql(?:-client)?-\d+(?:\.\d+)*$")
MARIADB_RUNTIME_PACKAGE_RE = re.compile(r"^mariadb-(?:server|client)-core(?:-\d+(?:\.\d+)*)?$")
GENERIC_PHP_PACKAGES = {
    "php-cli", "php-fpm", "php-common",
    "php-bcmath", "php-curl", "php-gd", "php-intl", "php-mbstring",
    "php-mysql", "php-pgsql", "php-readline", "php-sqlite3",
    "php-xml", "php-zip", "php-opcache",
}
ALLOWED_PHP_MODULES = {
    "bcmath", "curl", "gd", "intl", "mbstring",
    "mysqlnd", "mysqli", "pdo_mysql",
    "pgsql", "pdo_pgsql",
    "readline", "sqlite3", "pdo_sqlite",
    "dom", "simplexml", "xml", "xmlreader", "xmlwriter", "xsl",
    "zip", "opcache",
}
PHP_EXTENSION_CATALOG = {
    "mysql": ("mysql", ("mysqlnd", "mysqli", "pdo_mysql"), None),
    "pgsql": ("pgsql", ("pgsql", "pdo_pgsql"), None),
    "sqlite3": ("sqlite3", ("sqlite3", "pdo_sqlite"), None),
    "bcmath": ("bcmath", ("bcmath",), None),
    "curl": ("curl", ("curl",), None),
    "gd": ("gd", ("gd",), None),
    "intl": ("intl", ("intl",), None),
    "mbstring": ("mbstring", ("mbstring",), None),
    "readline": ("readline", ("readline",), None),
    "xml": ("xml", ("dom", "simplexml", "xml", "xmlreader", "xmlwriter", "xsl"), None),
    "zip": ("zip", ("zip",), None),
    "opcache": ("opcache", ("opcache",), (8, 5)),
    "apcu": ("apcu", ("apcu",), None),
    "bz2": ("bz2", ("bz2",), None),
    "dba": ("dba", ("dba",), None),
    "enchant": ("enchant", ("enchant",), None),
    "gmp": ("gmp", ("gmp",), None),
    "imap": ("imap", ("imap",), None),
    "ldap": ("ldap", ("ldap",), None),
    "odbc": ("odbc", ("odbc", "pdo_odbc"), None),
    "pspell": ("pspell", ("pspell",), None),
    "snmp": ("snmp", ("snmp",), None),
    "soap": ("soap", ("soap",), None),
    "tidy": ("tidy", ("tidy",), None),
    "redis": ("redis", ("redis",), None),
    "memcached": ("memcached", ("memcached",), None),
    "imagick": ("imagick", ("imagick",), None),
    "amqp": ("amqp", ("amqp",), None),
    "igbinary": ("igbinary", ("igbinary",), None),
    "mongodb": ("mongodb", ("mongodb",), None),
    "msgpack": ("msgpack", ("msgpack",), None),
    "smbclient": ("smbclient", ("smbclient",), None),
    "ssh2": ("ssh2", ("ssh2",), None),
    "yaml": ("yaml", ("yaml",), None),
    "pcov": ("pcov", ("pcov",), None),
    "xdebug": ("xdebug", ("xdebug",), None),
}
FPM_POOL_RE = re.compile(r"^/etc/php/(?P<version>\d+\.\d+)/fpm/pool\.d/nativedev-(?P<uid>\d+)\.conf$")
TEMP_SOURCE_RE = re.compile(r"^/tmp/nativedev-[^/]+/.+$")
SURY_KEYRING_URL = "https://packages.sury.org/debsuryorg-archive-keyring.deb"
SURY_SOURCE_FILE = Path("/etc/apt/sources.list.d/nativedev-sury-php.sources")
SURY_SUPPORTED_CODENAMES = {"bullseye", "bookworm", "trixie", "jammy", "noble", "resolute"}

# Only packages NativeDev actually exposes as native stack components. PHP is
# handled separately because the version/extension portion is dynamic.
COMPONENT_PACKAGES = {
    "acl",
    "nginx",
    "redis-server",
    "redis-tools",
    "memcached",
    "mariadb-server",
    "mariadb-client",
    "mysql-server",
    "postgresql",
    "postgresql-client",
    "composer",
    "mkcert",
    "nodejs",
    "npm",
}


def _safe_temp_source(value: str) -> bool:
    try:
        resolved = str(Path(value).resolve())
    except OSError:
        return False
    return bool(TEMP_SOURCE_RE.match(resolved))



def _managed_file(value: str, uid: int | None = None) -> bool:
    if value in MANAGED_FILES:
        return True
    match = FPM_POOL_RE.fullmatch(value)
    if not match:
        return False
    return uid is None or int(match.group("uid")) == uid


def _installable_file(value: str, uid: int | None = None) -> bool:
    # The Nginx enablement path is a symlink managed only by nginx.enable_site,
    # and the Sury source is written only by the semantic sury.configure action.
    if value in {
        "/etc/NetworkManager/conf.d/nativedev-dns.conf",
        "/etc/NetworkManager/dnsmasq.d/nativedev-test.conf",
        "/etc/nginx/sites-available/nativedev-sites.conf",
        "/etc/nginx/nativedev/nativedev.pem",
        "/etc/nginx/nativedev/nativedev-key.pem",
    }:
        return True
    match = FPM_POOL_RE.fullmatch(value)
    if not match:
        return False
    return uid is None or int(match.group("uid")) == uid


def _allowed_php_package(value: str) -> bool:
    return bool(value in GENERIC_PHP_PACKAGES or PHP_PACKAGE_RE.fullmatch(value))


def _allowed_package(value: str) -> bool:
    return bool(value in COMPONENT_PACKAGES or _allowed_php_package(value))


def _allowed_remove_package(value: str) -> bool:
    # Service cleanup may remove only the concrete runtime packages that Debian
    # leaves behind after top-level PostgreSQL/MariaDB package removal. Keep
    # install requests on the narrower normal component allowlist.
    return bool(
        _allowed_package(value)
        or POSTGRESQL_RUNTIME_PACKAGE_RE.fullmatch(value)
        or MARIADB_RUNTIME_PACKAGE_RE.fullmatch(value)
    )


def _version_key(value: str) -> tuple[int, int]:
    try:
        major, minor = value.split(".", 1)
        return int(major), int(minor)
    except (TypeError, ValueError):
        return 0, 0


def _php_extension_details(request: dict) -> tuple[str, str, str, tuple[str, ...]]:
    if any(field in request for field in ("sapi", "modules", "package")):
        raise RuntimeError("PHP extension operations do not accept client-supplied package/module/SAPI selectors")
    version = request.get("version")
    extension = request.get("extension")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise RuntimeError("Invalid PHP extension version")
    if not isinstance(extension, str) or extension not in PHP_EXTENSION_CATALOG:
        raise RuntimeError("PHP extension is outside NativeDev's curated catalog")
    suffix, modules, built_in_from = PHP_EXTENSION_CATALOG[extension]
    if built_in_from and _version_key(version) >= built_in_from:
        raise RuntimeError(f"{extension} is built into PHP {version} and is not package-managed")
    return version, extension, f"php{version}-{suffix}", modules


def _php_module_link_exists(version: str, sapi: str, module: str) -> bool:
    conf_dir = Path(f"/etc/php/{version}/{sapi}/conf.d")
    if not conf_dir.is_dir():
        return False
    candidates = list(conf_dir.glob(f"*-{module}.ini"))
    candidates.extend(conf_dir.glob(f"{module}.ini"))
    return any(path.exists() for path in candidates)


def _run_extension_module_pair(version: str, modules: tuple[str, ...], enable: bool, timeout: int) -> subprocess.CompletedProcess:
    binary = "phpenmod" if enable else "phpdismod"
    snapshot = {
        sapi: {module: _php_module_link_exists(version, sapi, module) for module in modules}
        for sapi in ("cli", "fpm")
    }
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    for sapi in ("cli", "fpm"):
        argv = [_binary(binary), "-v", version, "-s", sapi, *modules]
        proc = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
        if proc.stdout:
            stdout_parts.append(proc.stdout)
        if proc.stderr:
            stderr_parts.append(proc.stderr)
        if proc.returncode == 0:
            continue

        rollback_errors: list[str] = []
        for rollback_sapi in ("cli", "fpm"):
            enabled_before = [module for module, value in snapshot[rollback_sapi].items() if value]
            disabled_before = [module for module, value in snapshot[rollback_sapi].items() if not value]
            for rollback_binary, rollback_modules in (("phpenmod", enabled_before), ("phpdismod", disabled_before)):
                if not rollback_modules:
                    continue
                rollback = subprocess.run(
                    [_binary(rollback_binary), "-v", version, "-s", rollback_sapi, *rollback_modules],
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                )
                if rollback.returncode != 0:
                    rollback_errors.append(
                        rollback.stderr.strip() or rollback.stdout.strip() or f"{rollback_binary} rollback failed"
                    )
        if rollback_errors:
            stderr_parts.append("Rollback: " + "; ".join(rollback_errors))
        return subprocess.CompletedProcess(argv, proc.returncode, "".join(stdout_parts), "\n".join(stderr_parts))

    return subprocess.CompletedProcess(
        [f"nativedev:php.extension_{'enable' if enable else 'disable'}"],
        0,
        "".join(stdout_parts),
        "\n".join(stderr_parts),
    )



def _php_ini_request_details(request: dict, *, apply: bool) -> tuple[str, dict[str, str]]:
    allowed_fields = {"protocol", "action", "version", "timeout"}
    if apply:
        allowed_fields.add("settings")
    unexpected = set(request).difference(allowed_fields)
    if unexpected:
        raise RuntimeError("PHP INI operations do not accept client-supplied paths, content, SAPI or other selectors")

    version = request.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise RuntimeError("Invalid PHP INI version")

    if not apply:
        if "settings" in request:
            raise RuntimeError("PHP INI reset does not accept settings")
        return version, {}

    settings = request.get("settings")
    if not isinstance(settings, dict) or not settings:
        raise RuntimeError("PHP INI apply requires at least one directive")
    if len(settings) > PHP_INI_MAX_SETTINGS:
        raise RuntimeError(f"Too many PHP INI overrides (maximum {PHP_INI_MAX_SETTINGS})")

    validated: dict[str, str] = {}
    for directive, value in settings.items():
        if not isinstance(directive, str) or not directive:
            raise RuntimeError("PHP INI directive name is required")
        if len(directive) > PHP_INI_MAX_DIRECTIVE_LENGTH or not PHP_INI_DIRECTIVE_RE.fullmatch(directive):
            raise RuntimeError("Invalid PHP INI directive name")
        if directive.casefold() in PHP_INI_BLOCKED_DIRECTIVES:
            raise RuntimeError(f"{directive} is managed by PHP Extensions, not PHP Settings")
        if not isinstance(value, str):
            raise RuntimeError("PHP INI value must be text")
        # Non-negotiable injection boundary: never strip or normalize these.
        # Any of them could turn one semantic value into another INI line or
        # truncate the root-side rendered file unexpectedly.
        if "\n" in value or "\r" in value or "\0" in value:
            raise RuntimeError("PHP INI value must be a single line (newline, carriage return and NUL are not allowed)")
        if len(value) > PHP_INI_MAX_VALUE_LENGTH:
            raise RuntimeError(f"PHP INI value is too long (maximum {PHP_INI_MAX_VALUE_LENGTH} characters)")
        validated[directive] = value
    return version, validated


def _php_ini_paths(version: str) -> tuple[Path, Path, Path]:
    root = PHP_CONFIG_ROOT / version
    managed = root / "mods-available" / "nativedev.ini"
    cli_link = root / "cli" / "conf.d" / "99-nativedev.ini"
    fpm_link = root / "fpm" / "conf.d" / "99-nativedev.ini"
    return managed, cli_link, fpm_link


def _snapshot_path(path: Path):
    if path.is_symlink():
        return ("symlink", os.readlink(path))
    if path.exists():
        if not path.is_file():
            raise RuntimeError(f"NativeDev PHP INI path is not a regular file: {path}")
        return ("file", path.read_bytes(), path.stat().st_mode & 0o777)
    return ("missing",)


def _unlink_if_present(path: Path) -> None:
    if path.is_symlink() or path.exists():
        path.unlink()


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.nativedev-", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_symlink(path: Path, target: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.nativedev-link-", dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    temp.unlink(missing_ok=True)
    try:
        os.symlink(target, temp)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _restore_path(path: Path, snapshot) -> None:
    _unlink_if_present(path)
    kind = snapshot[0]
    if kind == "missing":
        return
    if kind == "symlink":
        os.symlink(snapshot[1], path)
        return
    if kind == "file":
        _atomic_write_bytes(path, snapshot[1], snapshot[2])
        return
    raise RuntimeError(f"Unknown NativeDev rollback snapshot for {path}")


def _render_php_ini(version: str, settings: dict[str, str]) -> bytes:
    lines = [
        "; Managed by NativeDev for local development.",
        f"; PHP {version}; loaded by CLI and FPM through 99-nativedev.ini.",
        "; Extension loading is managed separately on the PHP Extensions page.",
    ]
    for directive in sorted(settings, key=str.casefold):
        lines.append(f"{directive} = {settings[directive]}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _php_ini_runtime_ready(version: str) -> None:
    php_binary = Path(f"/usr/bin/php{version}")
    managed, cli_link, fpm_link = _php_ini_paths(version)
    required_dirs = (managed.parent, cli_link.parent, fpm_link.parent)
    if not php_binary.is_file():
        raise RuntimeError(f"PHP {version} CLI runtime is not installed")
    missing = [str(path) for path in required_dirs if not path.is_dir()]
    if missing:
        raise RuntimeError("PHP CLI/FPM configuration directories are missing: " + ", ".join(missing))
    if not (PHP_CONFIG_ROOT / version / "fpm" / "php-fpm.conf").is_file():
        raise RuntimeError(f"PHP {version} FPM configuration is not ready")


def _validate_php_ini_runtime(version: str, timeout: int) -> None:
    php_binary = f"/usr/bin/php{version}"
    cli = subprocess.run(
        [php_binary, "-r", "exit(0);"],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    cli_text = (cli.stdout + "\n" + cli.stderr).lower()
    if cli.returncode != 0 or "syntax error" in cli_text or "failed to parse" in cli_text:
        raise RuntimeError(cli.stderr.strip() or cli.stdout.strip() or f"PHP {version} CLI rejected the INI override")

    fpm = subprocess.run(
        [_binary(f"php-fpm{version}"), "-tt"],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if fpm.returncode != 0:
        raise RuntimeError(fpm.stderr.strip() or fpm.stdout.strip() or f"PHP {version} FPM rejected the INI override")


def _fpm_is_active(version: str, timeout: int) -> bool:
    proc = subprocess.run(
        [_binary("systemctl"), "is-active", "--quiet", f"php{version}-fpm"],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return proc.returncode == 0


def _reload_fpm(version: str, timeout: int) -> None:
    service = f"php{version}-fpm"
    reload_proc = subprocess.run(
        [_binary("systemctl"), "reload", service],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if reload_proc.returncode == 0:
        return
    restart = subprocess.run(
        [_binary("systemctl"), "restart", service],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if restart.returncode != 0:
        error = restart.stderr.strip() or restart.stdout.strip() or reload_proc.stderr.strip() or reload_proc.stdout.strip()
        raise RuntimeError(error or f"Could not reload PHP {version} FPM")


def _execute_php_ini_change(version: str, settings: dict[str, str] | None, timeout: int) -> subprocess.CompletedProcess:
    _php_ini_runtime_ready(version)
    managed, cli_link, fpm_link = _php_ini_paths(version)
    snapshots = {
        managed: _snapshot_path(managed),
        cli_link: _snapshot_path(cli_link),
        fpm_link: _snapshot_path(fpm_link),
    }
    was_active = _fpm_is_active(version, timeout)

    try:
        if settings is None:
            _unlink_if_present(cli_link)
            _unlink_if_present(fpm_link)
            _unlink_if_present(managed)
        else:
            _atomic_write_bytes(managed, _render_php_ini(version, settings), 0o644)
            relative_target = "../../mods-available/nativedev.ini"
            _atomic_symlink(cli_link, relative_target)
            _atomic_symlink(fpm_link, relative_target)

        _validate_php_ini_runtime(version, timeout)
        if was_active:
            _reload_fpm(version, timeout)
    except Exception as exc:
        rollback_errors: list[str] = []
        for path in (managed, cli_link, fpm_link):
            try:
                _restore_path(path, snapshots[path])
            except Exception as rollback_exc:
                rollback_errors.append(f"restore {path}: {rollback_exc}")
        try:
            _validate_php_ini_runtime(version, timeout)
            if was_active:
                _reload_fpm(version, timeout)
        except Exception as rollback_exc:
            rollback_errors.append(f"runtime rollback: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(f"PHP {version} INI change failed ({exc}); rollback also failed: " + "; ".join(rollback_errors)) from exc
        raise RuntimeError(f"PHP {version} INI change failed and was rolled back: {exc}") from exc

    action = "apply" if settings is not None else "reset"
    return subprocess.CompletedProcess([f"nativedev:php.ini.{action}"], 0, "", "")


def _binary(name: str) -> str:
    value = shutil.which(name, path=SAFE_PATH)
    if not value:
        raise RuntimeError(f"Required system binary was not found: {name}")
    return value


def _string_list(value, field: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"Invalid {field}")
    return value


def command_for_operation(request: dict, uid: int) -> list[str]:
    """Validate one structured RPC and build the root-side argv internally."""
    if request.get("protocol") != PROTOCOL_VERSION:
        raise RuntimeError("NativeDev privileged protocol version mismatch")

    action = request.get("action")
    if action == "apt.update":
        return [_binary("apt-get"), "update"]

    if action in {"apt.install", "apt.remove"}:
        packages = _string_list(request.get("packages"), "packages")
        allowed = _allowed_package if action == "apt.install" else _allowed_remove_package
        if not all(allowed(item) for item in packages):
            raise RuntimeError("APT package request is outside NativeDev's component allowlist")
        verb = "install" if action == "apt.install" else "remove"
        return [_binary("apt-get"), verb, "-y", *packages]

    if action == "apt.reinstall_confmiss":
        packages = _string_list(request.get("packages"), "packages")
        if not all(PHP_FPM_PACKAGE_RE.fullmatch(item) for item in packages):
            raise RuntimeError("Only PHP-FPM packages may use conffile repair")
        return [
            _binary("apt-get"),
            "install",
            "--reinstall",
            "-y",
            "-o",
            "Dpkg::Options::=--force-confmiss",
            *packages,
        ]

    if action == "systemd.service":
        verb = request.get("verb")
        service = request.get("service")
        now = request.get("now", False)
        if verb not in {"start", "stop", "restart", "reload", "enable", "disable"}:
            raise RuntimeError("systemd action is outside NativeDev's allowlist")
        if not isinstance(service, str) or not SERVICE_RE.fullmatch(service):
            raise RuntimeError("systemd service is outside NativeDev's allowlist")
        if not isinstance(now, bool) or (now and verb not in {"enable", "disable"}):
            raise RuntimeError("Invalid systemd --now request")
        return [_binary("systemctl"), verb, *(("--now",) if now else ()), service]

    if action == "file.install":
        mode = request.get("mode")
        source = request.get("source")
        destination = request.get("destination")
        if mode not in {"0644", "0600"}:
            raise RuntimeError("File mode is outside NativeDev's allowlist")
        if not isinstance(source, str) or not _safe_temp_source(source):
            raise RuntimeError("Source is outside NativeDev's temporary directory")
        if not isinstance(destination, str) or not _installable_file(destination, uid):
            raise RuntimeError("Destination is outside NativeDev-installable files")
        return [_binary("install"), "-m", mode, source, destination]

    if action == "file.mkdir":
        paths = _string_list(request.get("paths"), "paths")
        if not all(item in MANAGED_DIRS for item in paths):
            raise RuntimeError("Directory is outside NativeDev-managed directories")
        return [_binary("mkdir"), "-p", *paths]

    if action == "nginx.enable_site":
        return [
            _binary("ln"),
            "-sfn",
            "/etc/nginx/sites-available/nativedev-sites.conf",
            "/etc/nginx/sites-enabled/nativedev-sites.conf",
        ]

    if action == "file.remove":
        paths = _string_list(request.get("paths"), "paths")
        if not all(_managed_file(item, uid) for item in paths):
            raise RuntimeError("Removal path is outside NativeDev-managed files")
        return [_binary("rm"), "-f", *paths]

    if action == "networkmanager.reload":
        scope = request.get("scope")
        if scope not in {"conf", "dns-full"}:
            raise RuntimeError("NetworkManager reload scope is not allowed")
        return [_binary("nmcli"), "general", "reload", scope]

    if action == "nginx.test":
        return [_binary("nginx"), "-t"]

    if action == "php_fpm.test":
        version = request.get("version")
        verbose = request.get("verbose", False)
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version) or not isinstance(verbose, bool):
            raise RuntimeError("Invalid PHP-FPM validation request")
        return [_binary(f"php-fpm{version}"), "-tt" if verbose else "-t"]

    if action == "php.set_default":
        version = request.get("version")
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            raise RuntimeError("Invalid PHP default version")
        php_binary = f"/usr/bin/php{version}"
        if not Path(php_binary).is_file():
            raise RuntimeError(f"PHP binary does not exist: {php_binary}")
        return [_binary("update-alternatives"), "--set", "php", php_binary]

    if action == "php.install_packages":
        packages = _string_list(request.get("packages"), "packages")
        allow_downgrades = request.get("allow_downgrades", False)
        if not isinstance(allow_downgrades, bool):
            raise RuntimeError("Invalid PHP downgrade policy")
        if not all(_allowed_php_package(item) for item in packages):
            raise RuntimeError("Only NativeDev PHP packages may use the PHP install operation")
        return [
            _binary("apt-get"), "install", "--reinstall", "-y",
            *(["--allow-downgrades"] if allow_downgrades else []),
            *packages,
        ]

    if action in {"php.extension_install", "php.extension_remove", "php.extension_enable", "php.extension_disable"}:
        _version, _extension, package, _modules = _php_extension_details(request)
        if action == "php.extension_install":
            return [_binary("apt-get"), "install", "--reinstall", "-y", package]
        if action == "php.extension_remove":
            return [_binary("apt-get"), "remove", "-y", package]
        # Enable/disable are executed as one CLI+FPM transaction by
        # execute_operation(); no SAPI selector crosses the privilege boundary.
        return []

    if action == "php.ini.apply":
        _php_ini_request_details(request, apply=True)
        return []

    if action == "php.ini.reset":
        _php_ini_request_details(request, apply=False)
        return []

    if action == "php.enable_modules":
        version = request.get("version")
        sapi = request.get("sapi")
        modules = _string_list(request.get("modules"), "modules")
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            raise RuntimeError("Invalid PHP module version")
        if sapi not in {"cli", "fpm"}:
            raise RuntimeError("PHP module SAPI is outside NativeDev's allowlist")
        if not all(PHP_MODULE_RE.fullmatch(module) and module in ALLOWED_PHP_MODULES for module in modules):
            raise RuntimeError("PHP module request is outside NativeDev's development allowlist")
        # phpenmod manages Debian's /etc/php/<version>/<sapi>/conf.d links;
        # it is retained for the fixed install-time baseline only.
        return [_binary("phpenmod"), "-v", version, "-s", sapi, *modules]

    # This operation is intentionally executed by execute_operation(): both
    # the HTTPS keyring URL and DEB822 source template are root-side constants.
    if action == "sury.configure":
        codename = request.get("codename")
        if codename not in SURY_SUPPORTED_CODENAMES:
            raise RuntimeError("Unsupported Sury repository suite")
        return []

    raise RuntimeError(f"Privileged operation is not allowed: {action}")


def validate_operation(request: dict, uid: int = 1000) -> tuple[bool, str]:
    try:
        command_for_operation(request, uid)
    except RuntimeError as exc:
        return False, str(exc)
    return True, ""


def execute_operation(request: dict, uid: int, timeout: int) -> subprocess.CompletedProcess:
    action = request.get("action")

    if action == "php.extension_install":
        version, _extension, _package, modules = _php_extension_details(request)
        argv = command_for_operation(request, uid)
        env = dict(os.environ)
        env["PATH"] = SAFE_PATH
        env["UCF_FORCE_CONFFMISS"] = "1"
        install_proc = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        if install_proc.returncode != 0:
            return install_proc
        module_proc = _run_extension_module_pair(version, modules, True, timeout)
        if install_proc.stdout:
            module_proc.stdout = install_proc.stdout + module_proc.stdout
        if install_proc.stderr:
            module_proc.stderr = install_proc.stderr + module_proc.stderr
        return module_proc

    if action in {"php.extension_enable", "php.extension_disable"}:
        version, _extension, _package, modules = _php_extension_details(request)
        command_for_operation(request, uid)
        return _run_extension_module_pair(version, modules, action == "php.extension_enable", timeout)

    if action == "php.ini.apply":
        version, settings = _php_ini_request_details(request, apply=True)
        command_for_operation(request, uid)
        return _execute_php_ini_change(version, settings, timeout)

    if action == "php.ini.reset":
        version, _settings = _php_ini_request_details(request, apply=False)
        command_for_operation(request, uid)
        return _execute_php_ini_change(version, None, timeout)

    if action == "php.install_packages":
        # PHP's mods-available/*.ini files are UCF-managed. UCF intentionally
        # preserves a local deletion across ordinary reinstalls, so NativeDev's
        # explicit Install operation opts into restoring *missing* definitions.
        # Existing/customized files are left untouched by UCF_FORCE_CONFFMISS.
        argv = command_for_operation(request, uid)
        env = dict(os.environ)
        env["PATH"] = SAFE_PATH
        env["UCF_FORCE_CONFFMISS"] = "1"
        return subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )

    if action == "sury.configure":
        # Validate protocol/codename through the same operation validator first.
        command_for_operation(request, uid)
        codename = request["codename"]
        previous = None
        if SURY_SOURCE_FILE.exists():
            previous = SURY_SOURCE_FILE.read_bytes()
        try:
            with tempfile.TemporaryDirectory(prefix="nativedev-root-sury-", dir="/tmp") as temp_dir:
                temp = Path(temp_dir)
                package = temp / "debsuryorg-archive-keyring.deb"
                urllib.request.urlretrieve(SURY_KEYRING_URL, package)
                proc = subprocess.run(
                    [_binary("apt-get"), "install", "-y", str(package)],
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                )
                if proc.returncode != 0:
                    return proc

                source = (
                    "Types: deb\n"
                    "URIs: https://packages.sury.org/php/\n"
                    f"Suites: {codename}\n"
                    "Components: main\n"
                    "Signed-By: /usr/share/keyrings/debsuryorg-archive-keyring.gpg\n"
                )
                source_tmp = temp / "nativedev-sury-php.sources"
                source_tmp.write_text(source, encoding="utf-8")
                install_proc = subprocess.run(
                    [_binary("install"), "-m", "0644", str(source_tmp), str(SURY_SOURCE_FILE)],
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                )
                if install_proc.returncode != 0:
                    if previous is None:
                        SURY_SOURCE_FILE.unlink(missing_ok=True)
                    else:
                        SURY_SOURCE_FILE.write_bytes(previous)
                        os.chmod(SURY_SOURCE_FILE, 0o644)
                return install_proc
        except Exception:
            # Restore only NativeDev's own source file if the repository action
            # itself fails after touching it. The keyring package is harmless to
            # retain and may be shared with a user-managed Sury source.
            if previous is None:
                SURY_SOURCE_FILE.unlink(missing_ok=True)
            else:
                SURY_SOURCE_FILE.write_bytes(previous)
                os.chmod(SURY_SOURCE_FILE, 0o644)
            raise

    argv = command_for_operation(request, uid)
    return subprocess.run(argv, text=True, capture_output=True, timeout=timeout)


def _peer_cred(conn: socket.socket) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        return -1, -1, -1
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


def _read_request(conn: socket.socket) -> dict:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > 1024 * 1024:
            raise ValueError("Request too large")
        if b"\n" in chunk:
            break
    data = b"".join(chunks).split(b"\n", 1)[0]
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Request must be an object")
    return value


def _send(conn: socket.socket, payload: dict) -> None:
    conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def serve(socket_path: Path, uid: int, gid: int, parent_pid: int) -> int:
    if os.geteuid() != 0:
        return 77
    try:
        socket_path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        os.chown(socket_path, uid, gid)
        os.chmod(socket_path, 0o600)
        listener.listen(8)
        listener.settimeout(1.0)
    except OSError:
        return 78

    running = True

    def stop(*_args):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while running:
            try:
                os.kill(parent_pid, 0)
            except OSError:
                break
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    peer_pid, peer_uid, _peer_gid = _peer_cred(conn)
                    if peer_uid != uid or peer_pid != parent_pid:
                        _send(conn, {"ok": False, "error": "Peer identity rejected"})
                        continue
                    request = _read_request(conn)
                    if request.get("protocol") != PROTOCOL_VERSION:
                        _send(conn, {"ok": False, "error": "NativeDev privileged protocol version mismatch", "protocol": PROTOCOL_VERSION})
                        continue
                    action = request.get("action")
                    if action == "ping":
                        _send(conn, {"ok": True, "protocol": PROTOCOL_VERSION})
                        continue
                    if action == "shutdown":
                        _send(conn, {"ok": True, "protocol": PROTOCOL_VERSION})
                        running = False
                        continue

                    timeout = request.get("timeout", 120)
                    try:
                        timeout = max(1, min(int(timeout), 1800))
                    except (TypeError, ValueError):
                        timeout = 120
                    proc = execute_operation(request, uid, timeout)
                    _send(
                        conn,
                        {
                            "ok": True,
                            "protocol": PROTOCOL_VERSION,
                            "returncode": proc.returncode,
                            "stdout": proc.stdout,
                            "stderr": proc.stderr,
                        },
                    )
                except subprocess.TimeoutExpired as exc:
                    _send(conn, {"ok": False, "error": f"Command timed out: {exc}", "protocol": PROTOCOL_VERSION})
                except Exception as exc:  # helper trust boundary
                    _send(conn, {"ok": False, "error": str(exc), "protocol": PROTOCOL_VERSION})
    finally:
        try:
            listener.close()
        finally:
            socket_path.unlink(missing_ok=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    return serve(Path(args.socket), args.uid, args.gid, args.parent_pid)


if __name__ == "__main__":
    raise SystemExit(main())
