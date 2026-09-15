from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


LOCALDEV_NGINX_SITE = Path("/etc/nginx/sites-available/nativedev-sites.conf")
LOCALDEV_NGINX_ENABLED = Path("/etc/nginx/sites-enabled/nativedev-sites.conf")
LOCALDEV_NGINX_MARKER = "# NativeDev wildcard router v1"
TOOLS_NGINX = Path("/etc/nginx/conf.d/nativedev-tools.conf")
TOOLS_NGINX_MARKER = "# Managed by NativeDev Developer Tools v1"
TOOLS_HOSTS = (
    "phpmyadmin.localhost",
    "adminer.localhost",
    "adminer-sqlite.localhost",
)
NM_CONF = Path("/etc/NetworkManager/conf.d/nativedev-dns.conf")
NM_DNSMASQ = Path("/etc/NetworkManager/dnsmasq.d/nativedev-test.conf")
DNS_MARKER = "# Managed by NativeDev Local Development"
OLD_ADMINER_SQLITE = Path("/usr/lib/nativedev/adminer-sqlite/index.php")
OLD_ADMINER_SQLITE_ROOT = OLD_ADMINER_SQLITE.parent
NEW_ADMINER_SQLITE = Path("/var/lib/nativedev/adminer-sqlite/index.php")
ADMINER_SQLITE_MARKER = "// Managed by NativeDev: Adminer SQLite"


def _warn(message: str) -> None:
    print(f"NativeDev package lifecycle: {message}", file=sys.stderr)


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


def _systemd_active(service: str) -> bool:
    result = _run(["systemctl", "is-active", "--quiet", service])
    return result.returncode == 0


def _nginx_validate_and_reload() -> None:
    if not shutil.which("nginx"):
        return
    check = _run(["nginx", "-t"])
    if check.returncode != 0:
        _warn("Nginx configuration changed, but nginx -t failed; the running Nginx process was not reloaded")
        if check.stderr.strip():
            _warn(check.stderr.strip())
        return
    if _systemd_active("nginx"):
        reload_result = _run(["systemctl", "reload", "nginx"])
        if reload_result.returncode != 0:
            _warn("could not reload running Nginx after package lifecycle changes")


def _reload_networkmanager_dns() -> None:
    if not shutil.which("nmcli"):
        return
    for scope in ("conf", "dns-full"):
        result = _run(["nmcli", "general", "reload", scope])
        if result.returncode != 0:
            _warn(f"could not reload NetworkManager {scope} state after Local Development DNS cleanup")


def _read_text(path: Path) -> str | None:
    try:
        if path.is_file() and not path.is_symlink():
            return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return None
    return None


def _managed_nm_conf(path: Path) -> bool:
    text = _read_text(path)
    if text is None:
        return False
    if DNS_MARKER in text:
        return True
    return text.strip() == "[main]\ndns=dnsmasq"


def _managed_dnsmasq(path: Path) -> bool:
    text = _read_text(path)
    if text is None:
        return False
    if DNS_MARKER in text:
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return len(lines) == 1 and bool(re.fullmatch(r"address=/\.[A-Za-z0-9._-]+/127\.0\.0\.1", lines[0]))


def _enabled_points_to_localdev() -> bool:
    if not LOCALDEV_NGINX_ENABLED.is_symlink():
        return False
    try:
        return LOCALDEV_NGINX_ENABLED.resolve(strict=False) == LOCALDEV_NGINX_SITE.resolve(strict=False)
    except OSError:
        return False


def cleanup_localdev() -> None:
    """Remove only NativeDev's core wildcard DNS and park-router integration.

    Standalone services, PHP INI overrides, Mailpit and Developer Tool localhost
    routing are deliberately preserved; NativeDev is only their management UI.
    """
    dns_changed = False
    for path, predicate in ((NM_CONF, _managed_nm_conf), (NM_DNSMASQ, _managed_dnsmasq)):
        if path.exists() or path.is_symlink():
            if predicate(path):
                try:
                    path.unlink()
                    dns_changed = True
                except OSError as exc:
                    _warn(f"could not remove {path}: {exc}")
            else:
                _warn(f"left unrecognised file untouched: {path}")
    if dns_changed:
        _reload_networkmanager_dns()

    site_text = _read_text(LOCALDEV_NGINX_SITE)
    site_managed = bool(site_text and LOCALDEV_NGINX_MARKER in site_text)
    enabled_managed = _enabled_points_to_localdev()
    nginx_changed = False

    if enabled_managed and (site_managed or not LOCALDEV_NGINX_SITE.exists()):
        try:
            LOCALDEV_NGINX_ENABLED.unlink()
            nginx_changed = True
        except OSError as exc:
            _warn(f"could not remove {LOCALDEV_NGINX_ENABLED}: {exc}")
    elif LOCALDEV_NGINX_ENABLED.exists() and not enabled_managed:
        _warn(f"left unexpected Nginx enablement path untouched: {LOCALDEV_NGINX_ENABLED}")

    if LOCALDEV_NGINX_SITE.exists():
        if site_managed:
            try:
                LOCALDEV_NGINX_SITE.unlink()
                nginx_changed = True
            except OSError as exc:
                _warn(f"could not remove {LOCALDEV_NGINX_SITE}: {exc}")
        else:
            _warn(f"left unrecognised Nginx site untouched: {LOCALDEV_NGINX_SITE}")

    if nginx_changed:
        _nginx_validate_and_reload()


def _atomic_write(path: Path, data: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".nativedev-{os.getpid()}.tmp")
    try:
        temp.write_text(data, encoding="utf-8")
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _server_blocks(text: str) -> list[tuple[int, int, str]]:
    lines = text.splitlines(keepends=True)
    blocks: list[tuple[int, int, str]] = []
    depth = 0
    start: int | None = None
    for index, line in enumerate(lines):
        before = depth
        opens = line.count("{")
        closes = line.count("}")
        if start is None and before == 0 and line.lstrip().startswith("server {"):
            start = index
        depth += opens - closes
        if start is not None and depth == 0:
            blocks.append((start, index + 1, "".join(lines[start:index + 1])))
            start = None
    return blocks


def _migrate_legacy_tool_nginx() -> bool:
    legacy = _read_text(LOCALDEV_NGINX_SITE)
    if not legacy or LOCALDEV_NGINX_MARKER not in legacy:
        return False

    existing_tools = _read_text(TOOLS_NGINX)
    if TOOLS_NGINX.exists() and (existing_tools is None or TOOLS_NGINX_MARKER not in existing_tools):
        _warn(f"cannot migrate Developer Tool routing because {TOOLS_NGINX} is not NativeDev-managed")
        return False

    selected: list[tuple[int, int, str]] = []
    for block in _server_blocks(legacy):
        if any(f"server_name {host};" in block[2] for host in TOOLS_HOSTS):
            selected.append(block)
    if not selected:
        return False

    # Pre-0.2.0 Adminer SQLite Nginx blocks referenced the wrapper inside
    # /usr/lib/nativedev. The wrapper is now persistent state under /var/lib,
    # so migrate that fixed path together with the server block.
    selected = [
        (start, end, block.replace(str(OLD_ADMINER_SQLITE), str(NEW_ADMINER_SQLITE)))
        for start, end, block in selected
    ]

    old_tools = existing_tools
    old_legacy = legacy
    if existing_tools:
        tool_text = existing_tools.rstrip() + "\n\n"
        for _start, _end, block in selected:
            if not any(f"server_name {host};" in existing_tools and f"server_name {host};" in block for host in TOOLS_HOSTS):
                tool_text += block.strip() + "\n\n"
    else:
        tool_text = "\n".join(
            [
                TOOLS_NGINX_MARKER,
                "# Persistent service/tool integration; not part of NativeDev Local Development routing.",
                "# Migrated from the pre-0.2.0 combined NativeDev Nginx file.",
                "",
                *(block[2].strip() for block in selected),
                "",
            ]
        )

    lines = legacy.splitlines(keepends=True)
    remove_lines: set[int] = set()
    for start, end, _block in selected:
        remove_lines.update(range(start, end))
    stripped_legacy = "".join(line for idx, line in enumerate(lines) if idx not in remove_lines)

    try:
        _atomic_write(TOOLS_NGINX, tool_text)
        _atomic_write(LOCALDEV_NGINX_SITE, stripped_legacy)
        if shutil.which("nginx"):
            check = _run(["nginx", "-t"])
            if check.returncode != 0:
                raise RuntimeError(check.stderr.strip() or check.stdout.strip() or "nginx -t failed")
    except Exception as exc:
        _warn(f"Developer Tool Nginx migration failed; restoring previous files: {exc}")
        try:
            _atomic_write(LOCALDEV_NGINX_SITE, old_legacy)
            if old_tools is None:
                TOOLS_NGINX.unlink(missing_ok=True)
            else:
                _atomic_write(TOOLS_NGINX, old_tools)
        except OSError as rollback_exc:
            _warn(f"Nginx migration rollback also failed: {rollback_exc}")
        return False

    _nginx_validate_and_reload()
    return True


def _migrate_adminer_sqlite_wrapper() -> bool:
    old_text = _read_text(OLD_ADMINER_SQLITE)
    if old_text is None or ADMINER_SQLITE_MARKER not in old_text:
        return False
    if NEW_ADMINER_SQLITE.exists() or NEW_ADMINER_SQLITE.is_symlink():
        new_text = _read_text(NEW_ADMINER_SQLITE)
        if new_text is None or ADMINER_SQLITE_MARKER not in new_text:
            _warn(f"left old Adminer SQLite wrapper because destination is unmanaged: {NEW_ADMINER_SQLITE}")
            return False
    else:
        _atomic_write(NEW_ADMINER_SQLITE, old_text)
    try:
        OLD_ADMINER_SQLITE.unlink()
        OLD_ADMINER_SQLITE_ROOT.rmdir()
    except OSError as exc:
        _warn(f"Adminer SQLite wrapper migrated but old package-tree path could not be fully removed: {exc}")
    return True


def migrate() -> None:
    _migrate_adminer_sqlite_wrapper()
    _migrate_legacy_tool_nginx()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["migrate"]:
        migrate()
        return 0
    if args == ["cleanup-localdev"]:
        cleanup_localdev()
        return 0
    print("usage: python -m nativedev.package_lifecycle {migrate|cleanup-localdev}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
