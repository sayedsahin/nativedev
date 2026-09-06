from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
import tempfile

from ..config import AppConfig
from ..system import AptManager, CommandRunner, SystemdManager
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
ADMINER_SQLITE_KEY = "adminer_sqlite"
ADMINER_SQLITE_TITLE = "Adminer SQLite"
ADMINER_SQLITE_HOSTNAME = "adminer-sqlite.localhost"
ADMINER_SQLITE_ENTRYPOINT = Path("/var/lib/nativedev/adminer-sqlite/index.php")
ADMINER_SQLITE_MARKER = "// Managed by NativeDev: Adminer SQLite"
ADMINER_SQLITE_PASSWORD = "nativedev"
DEVELOPER_TOOLS_NGINX = Path("/etc/nginx/conf.d/nativedev-tools.conf")
DEVELOPER_TOOLS_NGINX_MARKER = "# Managed by NativeDev Developer Tools v1"


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


@dataclass(slots=True)
class AdminerSqliteState:
    installed: bool
    installable: bool
    adminer_installed: bool
    adminer_version: str | None
    php_version: str
    url: str
    password: str
    runtime_ready: bool
    runtime_note: str


class DeveloperToolManager:
    """APT-backed developer web applications served by NativeDev Nginx.

    Package ownership and updates remain with Debian/Ubuntu. NativeDev owns only
    the per-tool PHP-FPM selection and the exact local hostname integration.
    """

    def __init__(
        self,
        runner: CommandRunner,
        apt: AptManager,
        php: PhpManager,
        config: AppConfig,
        systemd: SystemdManager | None = None,
    ):
        self.runner = runner
        self.apt = apt
        self.php = php
        self.config = config
        self.systemd = systemd

    @staticmethod
    def supports(key: str) -> bool:
        return key in DEVELOPER_TOOL_BY_KEY

    def adminer_sqlite_state(self) -> AdminerSqliteState:
        adminer = DEVELOPER_TOOL_BY_KEY["adminer"]
        adminer_installed = self.apt.is_installed(adminer.package)
        adminer_version = self._installed_version(adminer.package) if adminer_installed else None
        php_version = self.effective_php("adminer") if adminer_installed else ""
        adminer_major = self._adminer_major(adminer_version)
        installed = self._adminer_sqlite_managed()
        runtime_ready = False
        runtime_note = ""
        if installed:
            expected_major = adminer_major
            wrapper_major = self._adminer_sqlite_wrapper_major()
            if not adminer_installed:
                runtime_note = "Adminer is not installed."
            elif expected_major not in {4, 5}:
                runtime_note = "Installed Adminer version is not supported by this NativeDev SQLite integration."
            elif wrapper_major != expected_major:
                runtime_note = "Adminer SQLite integration needs repair after an Adminer version change."
            elif not self._nginx_tool_ready(ADMINER_SQLITE_HOSTNAME, php_version):
                runtime_note = "Adminer SQLite localhost Nginx integration needs repair."
            else:
                runtime_ready = True
        return AdminerSqliteState(
            installed=installed,
            installable=adminer_installed and adminer_major in {4, 5} and bool(php_version),
            adminer_installed=adminer_installed,
            adminer_version=adminer_version,
            php_version=php_version,
            url=f"http://{ADMINER_SQLITE_HOSTNAME}",
            password=ADMINER_SQLITE_PASSWORD,
            runtime_ready=runtime_ready,
            runtime_note=runtime_note,
        )

    def install_adminer_sqlite(self) -> None:
        state = self.adminer_sqlite_state()
        if not state.adminer_installed:
            raise RuntimeError("Install Adminer before installing Adminer SQLite")
        if not state.php_version:
            raise RuntimeError("Adminer does not currently have an available PHP-FPM runtime")
        self.runner.privileged_operation(
            "developer_tool.adminer_sqlite.install",
            check=True,
            timeout=120,
        )

    def uninstall_adminer_sqlite(self) -> None:
        if not self._adminer_sqlite_managed():
            raise RuntimeError("Adminer SQLite is not installed")
        self.runner.privileged_operation(
            "developer_tool.adminer_sqlite.uninstall",
            check=True,
            timeout=120,
        )

    def reconcile_adminer_sqlite(self) -> None:
        if not self.apt.is_installed("adminer"):
            raise RuntimeError("Adminer is not installed")
        self.runner.privileged_operation(
            "developer_tool.adminer_sqlite.install",
            check=True,
            timeout=120,
        )

    def _adminer_sqlite_managed(self) -> bool:
        path = ADMINER_SQLITE_ENTRYPOINT
        if not path.is_file() or path.is_symlink():
            return False
        try:
            return ADMINER_SQLITE_MARKER in path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            return False

    def _adminer_sqlite_wrapper_major(self) -> int | None:
        path = ADMINER_SQLITE_ENTRYPOINT
        if not path.is_file() or path.is_symlink():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            return None
        match = re.search(r"NativeDev Adminer major: (\d+)", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _adminer_major(version: str | None) -> int | None:
        if not version:
            return None
        match = re.search(r"(?:^|:)(\d+)\.", version)
        return int(match.group(1)) if match else None

    @staticmethod
    def _nginx_quote(value: str) -> str:
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise RuntimeError("Nginx values may not contain control characters")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
        return f'"{escaped}"'

    def _render_adminer_single_file_server(self, host: str, script_path: Path, version: str) -> str:
        socket_value = f"unix:{self.php.developer_socket_path(version)}"
        backend = self._nginx_quote(socket_value)
        script = self._nginx_quote(str(script_path))
        return f"""server {{
    listen 80;
    listen [::]:80;
    server_name {host};

    allow 127.0.0.1;
    allow ::1;
    deny all;

    location = / {{
        include fastcgi_params;
        fastcgi_pass {backend};
        fastcgi_param SCRIPT_FILENAME {script};
        fastcgi_param SCRIPT_NAME /adminer.php;
        fastcgi_param HTTPS off;
    }}

    location = /adminer.php {{
        include fastcgi_params;
        fastcgi_pass {backend};
        fastcgi_param SCRIPT_FILENAME {script};
        fastcgi_param SCRIPT_NAME /adminer.php;
        fastcgi_param HTTPS off;
    }}

    location / {{
        return 404;
    }}
}}
"""

    def render_nginx(self) -> str:
        """Render persistent localhost-only Developer Tool routing.

        This configuration is intentionally independent from Local Development
        wildcard routing so removing the NativeDev application does not break
        installed standalone database tools.
        """
        installed_versions = set(self.php.installed_fpm_versions())
        default_version = self.php.default_fpm_version() or (sorted(installed_versions)[0] if installed_versions else "")
        blocks: list[str] = []
        for spec in DEVELOPER_WEB_TOOLS:
            if not self.apt.is_installed(spec.package):
                continue
            version = self.effective_php(spec.key) or default_version
            if not version or version not in installed_versions:
                continue
            host = developer_tool_hostname(spec.key)
            socket_value = f"unix:{self.php.developer_socket_path(version)}"
            backend = self._nginx_quote(socket_value)

            if spec.key == "adminer":
                blocks.append(
                    self._render_adminer_single_file_server(
                        host, spec.document_root / spec.entrypoint, version
                    )
                )
                continue

            root = self._nginx_quote(str(spec.document_root))
            blocks.append(
                f"""server {{
    listen 80;
    listen [::]:80;
    server_name {host};

    allow 127.0.0.1;
    allow ::1;
    deny all;

    root {root};
    index index.php index.html;

    location / {{
        try_files $uri $uri/ /index.php?$query_string;
    }}

    location ~* \\.(?:css|js|map|png|gif|jpe?g|svg|ico|webp|woff2?|ttf)$ {{
        try_files $uri =404;
        access_log off;
    }}

    location ~ \\.php$ {{
        try_files $uri =404;
        include fastcgi_params;
        fastcgi_pass {backend};
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_param HTTPS off;
        fastcgi_param PHP_ADMIN_VALUE "display_errors=Off\\nerror_reporting=E_ALL & ~E_DEPRECATED & ~E_USER_DEPRECATED";
    }}

    location ~ /\\. {{
        deny all;
    }}
}}
"""
            )

        if self.apt.is_installed("adminer") and self._adminer_sqlite_managed():
            version = self.effective_php("adminer") or default_version
            if version and version in installed_versions:
                blocks.append(
                    self._render_adminer_single_file_server(
                        ADMINER_SQLITE_HOSTNAME, ADMINER_SQLITE_ENTRYPOINT, version
                    )
                )

        if not blocks:
            return ""
        return "\n".join(
            [
                DEVELOPER_TOOLS_NGINX_MARKER,
                "# Persistent service/tool integration; not part of NativeDev Local Development routing.",
                "# Manual edits may be replaced while NativeDev manages these tools.",
                "",
                *blocks,
            ]
        )

    def nginx_managed(self) -> bool:
        if not DEVELOPER_TOOLS_NGINX.is_file() or DEVELOPER_TOOLS_NGINX.is_symlink():
            return False
        try:
            return DEVELOPER_TOOLS_NGINX_MARKER in DEVELOPER_TOOLS_NGINX.read_text(
                encoding="utf-8", errors="strict"
            )
        except (OSError, UnicodeError):
            return False

    def _nginx_tool_ready(self, host: str, version: str) -> bool:
        if not version or not self.nginx_managed():
            return False
        try:
            text = DEVELOPER_TOOLS_NGINX.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            return False
        socket_value = f"unix:{self.php.developer_socket_path(version)}"
        return f"server_name {host};" in text and socket_value in text

    def configure_nginx(self) -> None:
        if not shutil.which("nginx"):
            raise RuntimeError("Nginx is not installed")

        content = self.render_nginx()
        previous = None
        if DEVELOPER_TOOLS_NGINX.exists() or DEVELOPER_TOOLS_NGINX.is_symlink():
            if not self.nginx_managed():
                raise RuntimeError(f"Refusing to replace unmanaged Nginx path: {DEVELOPER_TOOLS_NGINX}")
            previous = DEVELOPER_TOOLS_NGINX.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory(prefix="nativedev-tools-nginx-", dir="/tmp") as temp_dir:
            temp = Path(temp_dir)
            if content:
                source = temp / "nativedev-tools.conf"
                source.write_text(content, encoding="utf-8")
                self.runner.run(
                    ["install", "-m", "0644", str(source), str(DEVELOPER_TOOLS_NGINX)],
                    privileged=True,
                    check=True,
                )
            elif previous is not None:
                self.runner.run(["rm", "-f", str(DEVELOPER_TOOLS_NGINX)], privileged=True, check=True)
            else:
                return

            check = self.runner.run(["nginx", "-t"], privileged=True, timeout=30)
            if not check.ok:
                if previous is None:
                    self.runner.run(["rm", "-f", str(DEVELOPER_TOOLS_NGINX)], privileged=True, check=True)
                else:
                    rollback = temp / "rollback-tools.conf"
                    rollback.write_text(previous, encoding="utf-8")
                    self.runner.run(
                        ["install", "-m", "0644", str(rollback), str(DEVELOPER_TOOLS_NGINX)],
                        privileged=True,
                        check=True,
                    )
                raise RuntimeError(check.output or "nginx -t failed; Developer Tool configuration rolled back")

        if self.systemd is not None and self.systemd.is_active("nginx"):
            self.systemd.reload("nginx")

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
        version = self.effective_php(spec.key)
        problems: list[str] = []
        if spec.key == "phpmyadmin":
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
            if not (config_ready and temp_ready):
                problems.append("NativeDev phpMyAdmin runtime integration needs repair")
        if self.apt.is_installed(spec.package) and not self._nginx_tool_ready(developer_tool_hostname(spec.key), version):
            problems.append("localhost Nginx integration needs repair")
        if problems:
            return False, "; ".join(problems) + "."
        return True, ""

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
