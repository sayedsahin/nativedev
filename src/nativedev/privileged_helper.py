#!/usr/bin/python3
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import signal
import secrets
import socket
import struct
import subprocess
import shutil
import tempfile
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

PROTOCOL_VERSION = 21
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
    r"^(?:nginx|redis-server|memcached|rabbitmq-server|mailpit|mariadb|mysql|postgresql|php\d+\.\d+-fpm)(?:\.service)?$"
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
PHPMYADMIN_NATIVEDEV_CONFIG = Path("/etc/phpmyadmin/conf.d/nativedev.php")
PHPMYADMIN_ENTRYPOINT = Path("/usr/share/phpmyadmin/index.php")
PHPMYADMIN_RUNTIME_ROOT = Path("/var/lib/nativedev/phpmyadmin")
PHPMYADMIN_CONFIG_MARKER = "// Managed by NativeDev. Manual edits may be replaced."
DATABASE_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
DATABASE_PASSWORD_RE = re.compile(r"^[A-Za-z0-9!@#$%^&*()_+\-=.,:?/]{1,128}$")
MYSQL_DEV_PRIVILEGES = (
    "SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, REFERENCES, INDEX, ALTER, "
    "CREATE TEMPORARY TABLES, LOCK TABLES, EXECUTE, CREATE VIEW, SHOW VIEW, "
    "CREATE ROUTINE, ALTER ROUTINE, EVENT, TRIGGER"
)
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
SURY_SUPPORTED_CODENAMES = {"bullseye", "bookworm", "trixie"}
ONDREJ_PPA = "ppa:ondrej/php"
ONDREJ_PPA_URI = "https://ppa.launchpadcontent.net/ondrej/php/ubuntu"
ONDREJ_SUPPORTED_CODENAMES = {"jammy", "noble"}


MAILPIT_RELEASE_API = "https://api.github.com/repos/axllent/mailpit/releases/latest"
MAILPIT_BINARY_PATH = Path("/usr/local/bin/mailpit")
MAILPIT_SERVICE_PATH = Path("/etc/systemd/system/mailpit.service")
MAILPIT_MANAGED_MARKER = "# Managed by NativeDev"
MAILPIT_MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAILPIT_SERVICE_CONTENT = f"""{MAILPIT_MANAGED_MARKER}
[Unit]
Description=Mailpit local email testing server
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/mailpit --listen 127.0.0.1:8025 --smtp 127.0.0.1:1025 -d /var/lib/mailpit/mailpit.db
Restart=on-failure
RestartSec=2
DynamicUser=yes
StateDirectory=mailpit
StateDirectoryMode=0750
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
"""


# Only packages NativeDev actually exposes as native stack components. PHP is
# handled separately because the version/extension portion is dynamic.
DEVELOPER_TOOL_PACKAGES = {
    "phpmyadmin": "phpmyadmin",
    "adminer": "adminer",
}


COMPONENT_PACKAGES = {
    "acl",
    "nginx",
    "redis-server",
    "redis-tools",
    "memcached",
    "rabbitmq-server",
    "mariadb-server",
    "mariadb-client",
    "postgresql",
    "postgresql-client",
    "composer",
    "mkcert",
    "nodejs",
    "npm",
}


def _read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
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
    return data


def _php_multi_repo_target() -> tuple[str, str]:
    """Resolve the fixed multi-PHP backend from the actual root-side distro."""
    data = _read_os_release()
    distro_id = data.get("ID", "").lower()
    id_like = set(data.get("ID_LIKE", "").lower().split())
    ubuntu_codename = data.get("UBUNTU_CODENAME", "").lower()
    if ubuntu_codename or distro_id == "ubuntu" or "ubuntu" in id_like:
        codename = ubuntu_codename or data.get("VERSION_CODENAME", "").lower()
        return "ondrej", codename
    if distro_id == "debian" or "debian" in id_like:
        codename = (data.get("DEBIAN_CODENAME") or data.get("VERSION_CODENAME") or "").lower()
        return "sury", codename
    return "", ""


def _validate_php_multi_repo_request(request: dict) -> tuple[str, str]:
    backend = request.get("backend")
    codename = request.get("codename")
    actual_backend, actual_codename = _php_multi_repo_target()
    if backend != actual_backend or codename != actual_codename:
        raise RuntimeError("Multi-PHP repository request does not match the detected system")
    if backend == "sury" and codename in SURY_SUPPORTED_CODENAMES:
        return backend, codename
    if backend == "ondrej" and codename in ONDREJ_SUPPORTED_CODENAMES:
        return backend, codename
    raise RuntimeError("Unsupported multi-PHP repository suite")


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
    # and the Sury source is written only by the semantic php.multi_repo.configure action.
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



def _database_request_details(request: dict) -> tuple[str, str, str | None, str | None]:
    """Validate fixed NativeDev database-account RPCs.

    No database username, host, privilege list, SQL text or executable selector
    is accepted from the client. The database username is derived root-side from
    the authenticated NativeDev process UID. The target development-account
    password remains narrowly validated because it is inserted into fixed SQL.

    MariaDB/MySQL may additionally receive a one-shot ``admin_password`` only for
    ``ensure_dev_account``. It is never interpolated into SQL or command-line
    arguments; the helper writes it to a root-owned temporary client option file.
    """
    action = request.get("action")
    status_actions = {
        "database.mysql.account_status",
        "database.postgresql.account_status",
    }
    ensure_actions = {
        "database.mysql.ensure_dev_account",
        "database.postgresql.ensure_dev_account",
    }
    if action not in status_actions | ensure_actions:
        raise RuntimeError("Unsupported database account operation")

    allowed = {"protocol", "action", "timeout"}
    password = None
    admin_password = None
    if action in ensure_actions:
        allowed.add("password")
        password = request.get("password")
        if not isinstance(password, str) or not DATABASE_PASSWORD_RE.fullmatch(password):
            raise RuntimeError("Invalid NativeDev database password")

    if action == "database.mysql.ensure_dev_account" and "admin_password" in request:
        allowed.add("admin_password")
        admin_password = request.get("admin_password")
        if (
            not isinstance(admin_password, str)
            or not admin_password
            or len(admin_password) > 512
            or any(ch in admin_password for ch in ("\0", "\n", "\r"))
        ):
            raise RuntimeError("Invalid MariaDB/MySQL administrator password")

    if set(request).difference(allowed):
        raise RuntimeError("Database account operation contains unsupported fields")

    family = "mysql" if ".mysql." in action else "postgresql"
    verb = "status" if action.endswith("account_status") else "ensure"
    return family, verb, password, admin_password


def _database_username_for_uid(uid: int) -> str:
    if not isinstance(uid, int) or uid <= 0:
        raise RuntimeError("Database access requires a non-root developer user")
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError as exc:
        raise RuntimeError(f"Could not resolve developer account for uid {uid}") from exc
    if not DATABASE_USERNAME_RE.fullmatch(username):
        raise RuntimeError("Developer username is not safe for NativeDev database access")
    return username


def _mysql_admin_argv(defaults_file: Path | None = None) -> list[str]:
    client = None
    for name in ("mariadb", "mysql"):
        try:
            client = _binary(name)
            break
        except RuntimeError:
            continue
    if not client:
        raise RuntimeError("MariaDB/MySQL client binary was not found")
    argv = [client]
    if defaults_file is not None:
        # MySQL-family clients require defaults-file selectors before ordinary
        # options. The password therefore stays out of argv/process listings.
        argv.append(f"--defaults-extra-file={defaults_file}")
    argv.extend([
        "--protocol=socket",
        "--user=root",
        "--batch",
        "--skip-column-names",
        "--silent",
    ])
    return argv


def _mysql_option_value(value: str) -> str:
    """Escape a password for a quoted MySQL/MariaDB option-file value."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
    )


def _run_mysql_admin(sql: str, admin_password: str | None, timeout: int, env: dict) -> subprocess.CompletedProcess:
    if admin_password is None:
        return subprocess.run(
            _mysql_admin_argv(),
            input=sql,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )

    # The GUI-provided database-root password is one-shot only. Keep it out of
    # command-line arguments and remove the root-owned 0600 option file promptly.
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="nativedev-mysql-admin-",
        delete=False,
    ) as handle:
        defaults_file = Path(handle.name)
        os.chmod(defaults_file, 0o600)
        handle.write("[client]\n")
        handle.write(f'password="{_mysql_option_value(admin_password)}"\n')
    try:
        return subprocess.run(
            _mysql_admin_argv(defaults_file),
            input=sql,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    finally:
        defaults_file.unlink(missing_ok=True)


def _postgres_admin_argv() -> list[str]:
    return [
        _binary("runuser"),
        "-u",
        "postgres",
        "--",
        _binary("psql"),
        "--no-psqlrc",
        "--no-align",
        "--tuples-only",
        "--dbname=postgres",
        "--set=ON_ERROR_STOP=1",
    ]


def _mysql_ensure_sql(username: str, password: str) -> str:
    # Username is derived from the authenticated peer UID and validated against
    # DATABASE_USERNAME_RE. Password validation excludes quote/backslash/control
    # characters, so this fixed SQL cannot be extended by client input.
    account = f"'{username}'@'localhost'"
    return (
        f"CREATE USER IF NOT EXISTS {account} IDENTIFIED BY '{password}';\n"
        f"ALTER USER {account} IDENTIFIED BY '{password}';\n"
        f"GRANT {MYSQL_DEV_PRIVILEGES} ON *.* TO {account};\n"
        "FLUSH PRIVILEGES;\n"
    )


def _postgres_ensure_sql(username: str, password: str) -> str:
    # The role name comes from the authenticated peer UID and is constrained by
    # DATABASE_USERNAME_RE. Quote it as an identifier so valid Unix names such
    # as ``dev-user`` remain valid PostgreSQL role names.
    role = f'"{username}"'
    return (
        "DO $nativedev$\n"
        "BEGIN\n"
        f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{username}') THEN\n"
        f"    CREATE ROLE {role} LOGIN PASSWORD '{password}';\n"
        "  ELSE\n"
        f"    ALTER ROLE {role} WITH LOGIN PASSWORD '{password}';\n"
        "  END IF;\n"
        "END\n"
        "$nativedev$;\n"
        f"ALTER ROLE {role} WITH LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS;\n"
    )


def _execute_database_operation(request: dict, uid: int, timeout: int) -> subprocess.CompletedProcess:
    family, verb, password, admin_password = _database_request_details(request)
    username = _database_username_for_uid(uid)
    env = dict(os.environ)
    env["PATH"] = SAFE_PATH

    if family == "mysql":
        if verb == "status":
            sql = (
                "SELECT COUNT(*) FROM mysql.user "
                f"WHERE User='{username}' AND Host='localhost';\n"
            )
            return _run_mysql_admin(sql, None, timeout, env)

        # Default-user flow: first prove local root/no-password (including the
        # common Debian unix_socket root setup). Only if that login itself fails
        # do we tell the GUI to ask for the MariaDB/MySQL root password.
        probe = _run_mysql_admin("SELECT 1;\n", admin_password, timeout, env)
        if probe.returncode != 0:
            if admin_password is None:
                return subprocess.CompletedProcess(
                    probe.args,
                    77,
                    probe.stdout,
                    "NATIVEDEV_MYSQL_ROOT_PASSWORD_REQUIRED",
                )
            return probe

        sql = _mysql_ensure_sql(username, password or "")
        return _run_mysql_admin(sql, admin_password, timeout, env)

    argv = _postgres_admin_argv()
    if verb == "status":
        sql = f"SELECT 1 FROM pg_roles WHERE rolname='{username}';\n"
    else:
        sql = _postgres_ensure_sql(username, password or "")
    return subprocess.run(
        argv,
        input=sql,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def _installed_postgresql_versions() -> list[str]:
    """Return installed PostgreSQL server major versions, newest first."""
    base = Path("/usr/lib/postgresql")
    try:
        children = list(base.iterdir())
    except FileNotFoundError:
        return []

    versions: list[tuple[tuple[int, ...], str]] = []
    for child in children:
        if not child.is_dir() or not re.fullmatch(r"\d+(?:\.\d+)?", child.name):
            continue
        if not (child / "bin" / "postgres").is_file():
            continue
        versions.append((tuple(int(part) for part in child.name.split(".")), child.name))
    versions.sort(reverse=True)
    return [version for _parts, version in versions]


def _execute_postgresql_ensure_cluster(timeout: int | None) -> subprocess.CompletedProcess:
    """Ensure the default local PostgreSQL cluster exists and is online.

    NativeDev's connection profile is intentionally the conventional local
    port 5432. A destructive database reset removes both /var/lib/postgresql
    and /etc/postgresql after the server packages are removed, so package
    reinstallation cannot be allowed to depend on postinst heuristics alone.
    This operation repairs that lifecycle explicitly with postgresql-common's
    own cluster tools.
    """
    env = dict(os.environ)
    env["PATH"] = SAFE_PATH
    effective_timeout = timeout if timeout is not None else 120

    try:
        list_argv = [_binary("pg_lsclusters"), "--no-header"]
    except RuntimeError as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))

    listed = subprocess.run(
        list_argv,
        text=True,
        capture_output=True,
        timeout=effective_timeout,
        env=env,
    )
    if listed.returncode != 0:
        return listed

    clusters: list[tuple[str, str, str, str]] = []
    for raw in listed.stdout.splitlines():
        parts = raw.split()
        if len(parts) < 4:
            continue
        version, name, port, status = parts[:4]
        clusters.append((version, name, port, status))

    if not clusters:
        versions = _installed_postgresql_versions()
        if not versions:
            return subprocess.CompletedProcess(
                list_argv,
                1,
                listed.stdout,
                "PostgreSQL server packages are installed, but no server runtime version was found.",
            )
        version = versions[0]
        try:
            create_argv = [_binary("pg_createcluster"), "--start", version, "main"]
        except RuntimeError as exc:
            return subprocess.CompletedProcess([], 1, "", str(exc))
        return subprocess.run(
            create_argv,
            text=True,
            capture_output=True,
            timeout=effective_timeout,
            env=env,
        )

    default = next((row for row in clusters if row[2] == "5432"), None)
    if default is None:
        return subprocess.CompletedProcess(
            list_argv,
            1,
            listed.stdout,
            "PostgreSQL is installed, but no cluster is configured on the NativeDev local port 5432.",
        )

    version, name, _port, status = default
    if status.startswith("online"):
        return subprocess.CompletedProcess(list_argv, 0, listed.stdout, "")

    try:
        start_argv = [_binary("pg_ctlcluster"), version, name, "start"]
    except RuntimeError as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))
    return subprocess.run(
        start_argv,
        text=True,
        capture_output=True,
        timeout=effective_timeout,
        env=env,
    )


def command_for_operation(request: dict, uid: int) -> list[str]:
    """Validate one structured RPC and build the root-side argv internally."""
    if request.get("protocol") != PROTOCOL_VERSION:
        raise RuntimeError("NativeDev privileged protocol version mismatch")

    action = request.get("action")
    if action == "database.postgresql.ensure_cluster":
        if set(request).difference({"protocol", "action", "timeout"}):
            raise RuntimeError("PostgreSQL cluster operation contains unsupported fields")
        _database_username_for_uid(uid)
        return []
    if action == "database.delete_all_data":
        if set(request).difference({"protocol", "action", "timeout", "key"}):
            raise RuntimeError("Database reset operation contains unsupported fields")
        if request.get("key") not in {"mariadb", "postgresql"}:
            raise RuntimeError("Database reset target is outside NativeDev's allowlist")
        _database_username_for_uid(uid)
        return []
    if isinstance(action, str) and action.startswith("database."):
        _database_request_details(request)
        _database_username_for_uid(uid)
        return []

    if action in {"mailpit.install", "mailpit.uninstall"}:
        if set(request).difference({"protocol", "action", "timeout"}):
            raise RuntimeError("Mailpit operation contains unsupported fields")
        return []

    if action == "developer_tool.reconcile":
        if set(request).difference({"protocol", "action", "timeout", "tool"}):
            raise RuntimeError("Developer Tool reconciliation contains unsupported fields")
        if request.get("tool") != "phpmyadmin":
            raise RuntimeError("Only phpMyAdmin currently has NativeDev runtime reconciliation")
        _database_username_for_uid(uid)
        return []

    if action in {"developer_tool.install", "developer_tool.uninstall"}:
        if set(request).difference({"protocol", "action", "timeout", "tool"}):
            raise RuntimeError("Developer Tool operation contains unsupported fields")
        tool = request.get("tool")
        if tool not in DEVELOPER_TOOL_PACKAGES:
            raise RuntimeError("Developer Tool is outside NativeDev's allowlist")
        package = DEVELOPER_TOOL_PACKAGES[tool]
        verb = "install" if action == "developer_tool.install" else "remove"
        return [
            _binary("apt-get"),
            "-o", "DPkg::Lock::Timeout=0",
            verb,
            "-y",
            *(["--no-install-recommends"] if verb == "install" else []),
            package,
        ]

    if action == "apt.update":
        return [_binary("apt-get"), "update"]

    if action in {"apt.install", "apt.remove"}:
        packages = _string_list(request.get("packages"), "packages")
        allowed = _allowed_package if action == "apt.install" else _allowed_remove_package
        if not all(allowed(item) for item in packages):
            raise RuntimeError("APT package request is outside NativeDev's component allowlist")
        verb = "install" if action == "apt.install" else "remove"
        if action == "apt.remove":
            # Never wait behind another package-manager process. If dpkg is
            # busy, fail immediately and let the GUI surface the real lock
            # owner/error. This avoids a fake spinner wait without imposing a
            # wall-clock limit on a removal that has actually started.
            return [_binary("apt-get"), "-o", "DPkg::Lock::Timeout=0", verb, "-y", *packages]
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

    # Repository URLs/PPA names are root-side constants. The request can select
    # only the backend/codename that matches /etc/os-release on this host.
    if action in {"php.multi_repo.configure", "php.multi_repo.remove"}:
        _validate_php_multi_repo_request(request)
        return []

    raise RuntimeError(f"Privileged operation is not allowed: {action}")



def _mailpit_managed_unit() -> bool:
    try:
        return MAILPIT_MANAGED_MARKER in MAILPIT_SERVICE_PATH.read_text(encoding="utf-8")
    except OSError:
        return False


def _mailpit_release_architecture() -> str:
    machine = os.uname().machine.lower()
    mapping = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "i386": "386",
        "i486": "386",
        "i586": "386",
        "i686": "386",
    }
    arch = mapping.get(machine)
    if arch is None:
        raise RuntimeError(f"Mailpit does not provide a NativeDev-supported Linux binary for architecture: {machine}")
    return arch


def _mailpit_latest_asset_url(timeout: int | None) -> str:
    request = urllib.request.Request(
        MAILPIT_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "NativeDev",
        },
    )
    network_timeout = 120 if timeout is None else max(10, min(timeout, 120))
    with urllib.request.urlopen(request, timeout=network_timeout) as response:
        release = json.load(response)
    if not isinstance(release, dict):
        raise RuntimeError("Mailpit release metadata is invalid")

    expected_name = f"mailpit-linux-{_mailpit_release_architecture()}.tar.gz"
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("Mailpit release metadata does not contain assets")

    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != expected_name:
            continue
        url = asset.get("browser_download_url")
        if not isinstance(url, str):
            break
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or not parsed.path.startswith("/axllent/mailpit/releases/download/")
        ):
            raise RuntimeError("Mailpit release asset URL is outside the official upstream repository")
        return url
    raise RuntimeError(f"Mailpit release does not contain {expected_name}")


def _download_mailpit_asset(url: str, destination: Path, timeout: int | None) -> None:
    network_timeout = 120 if timeout is None else max(10, min(timeout, 120))
    request = urllib.request.Request(url, headers={"User-Agent": "NativeDev"})
    total = 0
    with urllib.request.urlopen(request, timeout=network_timeout) as response, destination.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAILPIT_MAX_DOWNLOAD_BYTES:
                raise RuntimeError("Mailpit release asset exceeds NativeDev's download size limit")
            handle.write(chunk)
    if total == 0:
        raise RuntimeError("Downloaded Mailpit release asset is empty")


def _execute_mailpit_install(timeout: int | None) -> subprocess.CompletedProcess:
    if MAILPIT_SERVICE_PATH.exists() or MAILPIT_BINARY_PATH.exists():
        return subprocess.CompletedProcess(
            [],
            1,
            "",
            "Mailpit files already exist; NativeDev will not overwrite an existing installation.",
        )

    installed_binary = False
    installed_unit = False
    success = False
    try:
        with tempfile.TemporaryDirectory(prefix="nativedev-root-mailpit-", dir="/tmp") as temp_dir:
            temp = Path(temp_dir)
            archive = temp / "mailpit.tar.gz"
            extracted = temp / "mailpit"
            unit = temp / "mailpit.service"

            url = _mailpit_latest_asset_url(timeout)
            _download_mailpit_asset(url, archive, timeout)

            with tarfile.open(archive, mode="r:gz") as bundle:
                members = [
                    member for member in bundle.getmembers()
                    if member.isfile() and Path(member.name).name == "mailpit"
                ]
                if len(members) != 1:
                    raise RuntimeError("Mailpit release archive does not contain exactly one mailpit binary")
                if members[0].size > MAILPIT_MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("Mailpit binary exceeds NativeDev's extraction size limit")
                source = bundle.extractfile(members[0])
                if source is None:
                    raise RuntimeError("Mailpit binary could not be read from the release archive")
                with source, extracted.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
            os.chmod(extracted, 0o755)

            validation = subprocess.run(
                [str(extracted), "version"],
                text=True,
                capture_output=True,
                timeout=30,
                env={**os.environ, "PATH": SAFE_PATH},
            )
            if validation.returncode != 0:
                return subprocess.CompletedProcess(
                    validation.args,
                    validation.returncode,
                    validation.stdout,
                    validation.stderr or "Downloaded Mailpit binary failed validation",
                )

            install_binary = subprocess.run(
                [_binary("install"), "-m", "0755", str(extracted), str(MAILPIT_BINARY_PATH)],
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            if install_binary.returncode != 0:
                return install_binary
            installed_binary = True

            unit.write_text(MAILPIT_SERVICE_CONTENT, encoding="utf-8")
            install_unit = subprocess.run(
                [_binary("install"), "-m", "0644", str(unit), str(MAILPIT_SERVICE_PATH)],
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            if install_unit.returncode != 0:
                return install_unit
            installed_unit = True

        reload_proc = subprocess.run(
            [_binary("systemctl"), "daemon-reload"],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if reload_proc.returncode != 0:
            return reload_proc

        enable_proc = subprocess.run(
            [_binary("systemctl"), "enable", "--now", "mailpit"],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if enable_proc.returncode != 0:
            return enable_proc
        success = True
        return enable_proc
    except Exception as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))
    finally:
        # A failed install must not leave a half-managed service behind.
        if not success and (installed_unit or installed_binary):
            try:
                subprocess.run(
                    [_binary("systemctl"), "disable", "--now", "mailpit"],
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
            except Exception:
                pass
            if installed_unit:
                try:
                    MAILPIT_SERVICE_PATH.unlink(missing_ok=True)
                except OSError:
                    pass
            if installed_binary:
                try:
                    MAILPIT_BINARY_PATH.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                subprocess.run(
                    [_binary("systemctl"), "daemon-reload"],
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
            except Exception:
                pass


def _execute_mailpit_uninstall(timeout: int | None) -> subprocess.CompletedProcess:
    if not _mailpit_managed_unit():
        return subprocess.CompletedProcess([], 1, "", "Mailpit is not managed by NativeDev")

    try:
        subprocess.run(
            [_binary("systemctl"), "disable", "--now", "mailpit"],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        MAILPIT_SERVICE_PATH.unlink(missing_ok=True)
        MAILPIT_BINARY_PATH.unlink(missing_ok=True)
        reload_proc = subprocess.run(
            [_binary("systemctl"), "daemon-reload"],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if reload_proc.returncode != 0:
            return reload_proc
        return subprocess.CompletedProcess([], 0, "", "")
    except (OSError, RuntimeError) as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))

def _phpmyadmin_runtime_paths(uid: int) -> tuple[pwd.struct_passwd, Path]:
    _database_username_for_uid(uid)
    account = pwd.getpwuid(uid)
    runtime_dir = PHPMYADMIN_RUNTIME_ROOT / str(uid) / "tmp"
    return account, runtime_dir


def _phpmyadmin_existing_secret() -> str | None:
    if not PHPMYADMIN_NATIVEDEV_CONFIG.exists():
        return None
    if PHPMYADMIN_NATIVEDEV_CONFIG.is_symlink() or not PHPMYADMIN_NATIVEDEV_CONFIG.is_file():
        raise RuntimeError("Refusing to replace unexpected phpMyAdmin NativeDev config path")
    text = PHPMYADMIN_NATIVEDEV_CONFIG.read_text(encoding="utf-8", errors="strict")
    if PHPMYADMIN_CONFIG_MARKER not in text:
        raise RuntimeError("Refusing to replace phpMyAdmin config not owned by NativeDev")
    match = re.search(r"\$cfg\['blowfish_secret'\]\s*=\s*'([0-9a-f]{32})';", text)
    return match.group(1) if match else None


def _execute_phpmyadmin_reconcile(uid: int) -> subprocess.CompletedProcess:
    try:
        if not PHPMYADMIN_ENTRYPOINT.is_file():
            raise RuntimeError("phpMyAdmin package files are not installed")
        conf_dir = PHPMYADMIN_NATIVEDEV_CONFIG.parent
        if not conf_dir.is_dir() or conf_dir.is_symlink():
            raise RuntimeError("phpMyAdmin configuration directory is unavailable or unsafe")

        account, runtime_dir = _phpmyadmin_runtime_paths(uid)
        for path in (PHPMYADMIN_RUNTIME_ROOT, PHPMYADMIN_RUNTIME_ROOT / str(uid), runtime_dir):
            if path.exists() and path.is_symlink():
                raise RuntimeError(f"Refusing unsafe phpMyAdmin runtime path: {path}")
            path.mkdir(mode=0o700 if path != PHPMYADMIN_RUNTIME_ROOT else 0o755, parents=True, exist_ok=True)

        user_root = PHPMYADMIN_RUNTIME_ROOT / str(uid)
        for path in (user_root, runtime_dir):
            os.chown(path, uid, account.pw_gid)
            os.chmod(path, 0o700)

        secret = _phpmyadmin_existing_secret() or secrets.token_hex(16)
        temp_literal = str(runtime_dir).replace("\\", "\\\\").replace("'", "\\'")
        content = (
            "<?php\n"
            f"{PHPMYADMIN_CONFIG_MARKER}\n"
            f"$cfg['blowfish_secret'] = '{secret}';\n"
            f"$cfg['TempDir'] = '{temp_literal}';\n"
            "$cfg['PmaNoRelation_DisableWarning'] = true;\n"
        ).encode("utf-8")
        _atomic_write_bytes(PHPMYADMIN_NATIVEDEV_CONFIG, content, 0o640)
        os.chown(PHPMYADMIN_NATIVEDEV_CONFIG, 0, account.pw_gid)
        os.chmod(PHPMYADMIN_NATIVEDEV_CONFIG, 0o640)
        return subprocess.CompletedProcess([], 0, "", "")
    except (OSError, RuntimeError, UnicodeError) as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))


def _execute_phpmyadmin_cleanup(uid: int) -> subprocess.CompletedProcess:
    try:
        if PHPMYADMIN_NATIVEDEV_CONFIG.exists():
            if PHPMYADMIN_NATIVEDEV_CONFIG.is_symlink() or not PHPMYADMIN_NATIVEDEV_CONFIG.is_file():
                raise RuntimeError("Refusing to remove unexpected phpMyAdmin NativeDev config path")
            text = PHPMYADMIN_NATIVEDEV_CONFIG.read_text(encoding="utf-8", errors="strict")
            if PHPMYADMIN_CONFIG_MARKER in text:
                PHPMYADMIN_NATIVEDEV_CONFIG.unlink(missing_ok=True)
        _account, runtime_dir = _phpmyadmin_runtime_paths(uid)
        user_root = runtime_dir.parent
        if user_root.exists() and not user_root.is_symlink():
            shutil.rmtree(user_root)
        return subprocess.CompletedProcess([], 0, "", "")
    except (OSError, RuntimeError, UnicodeError) as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))


def validate_operation(request: dict, uid: int = 1000) -> tuple[bool, str]:
    try:
        command_for_operation(request, uid)
    except RuntimeError as exc:
        return False, str(exc)
    return True, ""


def _remove_fixed_tree(path: Path) -> None:
    """Remove one fixed root-owned tree without following a symlink target."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path)
    except FileNotFoundError:
        pass


def _execute_database_delete_all_data(request: dict) -> subprocess.CompletedProcess:
    key = request.get("key")
    try:
        if key == "mariadb":
            # MariaDB/MySQL users, grants and passwords live in the mysql system
            # database under the datadir, so removing the complete default
            # datadir resets accounts together with user databases.
            _remove_fixed_tree(Path("/var/lib/mysql"))
        elif key == "postgresql":
            # PostgreSQL roles/passwords live inside cluster data. Remove both
            # the default cluster data and its per-cluster configuration so a
            # later package install can initialize a genuinely fresh cluster.
            _remove_fixed_tree(Path("/var/lib/postgresql"))
            _remove_fixed_tree(Path("/etc/postgresql"))
        else:
            raise RuntimeError("Database reset target is outside NativeDev's allowlist")
    except OSError as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))
    return subprocess.CompletedProcess([], 0, "", "")


def execute_operation(request: dict, uid: int, timeout: int | None) -> subprocess.CompletedProcess:
    action = request.get("action")

    if action == "mailpit.install":
        command_for_operation(request, uid)
        return _execute_mailpit_install(timeout)

    if action == "mailpit.uninstall":
        command_for_operation(request, uid)
        return _execute_mailpit_uninstall(timeout)

    if action == "database.postgresql.ensure_cluster":
        command_for_operation(request, uid)
        return _execute_postgresql_ensure_cluster(timeout)

    if action == "database.delete_all_data":
        command_for_operation(request, uid)
        return _execute_database_delete_all_data(request)

    if isinstance(action, str) and action.startswith("database."):
        command_for_operation(request, uid)
        return _execute_database_operation(request, uid, timeout)

    if action == "developer_tool.reconcile":
        command_for_operation(request, uid)
        return _execute_phpmyadmin_reconcile(uid)

    if action in {"apt.install", "apt.remove", "developer_tool.install", "developer_tool.uninstall"}:
        argv = command_for_operation(request, uid)
        env = dict(os.environ)
        env["PATH"] = SAFE_PATH
        # APT must never wait for a debconf/needrestart prompt behind the GTK
        # spinner. Package scripts can still take as long as they genuinely
        # need; timeout=None intentionally permits that.
        env["DEBIAN_FRONTEND"] = "noninteractive"
        env["APT_LISTCHANGES_FRONTEND"] = "none"
        env["NEEDRESTART_MODE"] = "a"

        # Debian/Ubuntu phpMyAdmin ships optional Apache/dbconfig integration.
        # NativeDev owns the Nginx route and never needs a phpMyAdmin control
        # database for basic local DB administration, so pin both answers to
        # safe noninteractive values before the fixed package install.
        if action == "developer_tool.install" and request.get("tool") == "phpmyadmin":
            preseed = (
                "phpmyadmin phpmyadmin/reconfigure-webserver multiselect \n"
                "phpmyadmin phpmyadmin/dbconfig-install boolean false\n"
            )
            seeded = subprocess.run(
                [_binary("debconf-set-selections")],
                input=preseed,
                text=True,
                capture_output=True,
                timeout=60,
                env=env,
            )
            if seeded.returncode != 0:
                return seeded

        package_proc = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        if package_proc.returncode != 0:
            return package_proc

        if request.get("tool") == "phpmyadmin":
            if action == "developer_tool.install":
                runtime_proc = _execute_phpmyadmin_reconcile(uid)
            elif action == "developer_tool.uninstall":
                runtime_proc = _execute_phpmyadmin_cleanup(uid)
            else:
                runtime_proc = subprocess.CompletedProcess([], 0, "", "")
            if runtime_proc.returncode != 0:
                if package_proc.stdout:
                    runtime_proc.stdout = package_proc.stdout + runtime_proc.stdout
                if package_proc.stderr:
                    runtime_proc.stderr = package_proc.stderr + runtime_proc.stderr
                return runtime_proc
        return package_proc

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

    if action in {"php.multi_repo.configure", "php.multi_repo.remove"}:
        backend, codename = _validate_php_multi_repo_request(request)
        command_for_operation(request, uid)

        if backend == "sury":
            if action == "php.multi_repo.remove":
                try:
                    SURY_SOURCE_FILE.unlink(missing_ok=True)
                    return subprocess.CompletedProcess([], 0, "", "")
                except OSError as exc:
                    return subprocess.CompletedProcess([], 1, "", str(exc))

            previous = SURY_SOURCE_FILE.read_bytes() if SURY_SOURCE_FILE.exists() else None
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
                if previous is None:
                    SURY_SOURCE_FILE.unlink(missing_ok=True)
                else:
                    SURY_SOURCE_FILE.write_bytes(previous)
                    os.chmod(SURY_SOURCE_FILE, 0o644)
                raise

        # Ubuntu/Ubuntu-derivative path. Use the official PPA helper so Launchpad
        # key management stays with Ubuntu's software-properties implementation.
        add_repo = shutil.which("add-apt-repository", path=SAFE_PATH)
        if not add_repo:
            install_tool = subprocess.run(
                [_binary("apt-get"), "install", "-y", "software-properties-common"],
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            if install_tool.returncode != 0:
                return install_tool
            add_repo = shutil.which("add-apt-repository", path=SAFE_PATH)
        if not add_repo:
            return subprocess.CompletedProcess([], 127, "", "add-apt-repository is unavailable")

        # Use an explicit source line with the root-side resolved Ubuntu base
        # suite. This is important on derivatives whose own VERSION_CODENAME
        # (for example a Mint codename) is not a Launchpad distro series.
        source_line = f"deb {ONDREJ_PPA_URI} {codename} main"
        argv = [add_repo, "-y"]
        if action == "php.multi_repo.remove":
            argv.append("--remove")
        argv.extend(["--sourceslist", source_line])
        env = dict(os.environ)
        env["PATH"] = SAFE_PATH
        env["LC_ALL"] = "C.UTF-8"
        return subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )

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

                    timeout_value = request.get("timeout", 120)
                    if timeout_value is None:
                        timeout = None
                    else:
                        try:
                            timeout = max(1, min(int(timeout_value), 1800))
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
