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

PROTOCOL_VERSION = 8
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
