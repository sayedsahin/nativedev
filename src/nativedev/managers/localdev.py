from __future__ import annotations

import os
import pwd
import re
import shutil
import socket
import tempfile
from pathlib import Path

from ..config import AppConfig, STATE_DIR
from ..system import AptManager, CommandRunner, SystemdManager
from .php import PhpManager


NGINX_SITE = Path("/etc/nginx/sites-available/nativedev-sites.conf")
NGINX_ENABLED = Path("/etc/nginx/sites-enabled/nativedev-sites.conf")
NM_CONF = Path("/etc/NetworkManager/conf.d/nativedev-dns.conf")
NM_DNSMASQ = Path("/etc/NetworkManager/dnsmasq.d/nativedev-test.conf")
NGINX_CERT_DIR = Path("/etc/nginx/nativedev")
WEB_USER = "www-data"
PHP_DEFAULT = "default"


class LocalDevManager:
    def __init__(
        self,
        runner: CommandRunner,
        apt: AptManager,
        systemd: SystemdManager,
        config: AppConfig,
        php: PhpManager,
    ):
        self.runner = runner
        self.apt = apt
        self.systemd = systemd
        self.config = config
        self.php = php

    @property
    def park_dir(self) -> Path:
        return Path(self.config.park_dir).expanduser().resolve()

    def projects(self) -> list[Path]:
        root = self.park_dir
        if not root.is_dir():
            return []
        return sorted(
            [p.resolve() for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")],
            key=lambda p: p.name.lower(),
        )

    def document_root(self, project: Path) -> Path:
        project = self._validate_project(project)
        public = project / "public"
        return public if public.is_dir() else project

    # ---- Per-project PHP-FPM version -------------------------------------
    # PHP itself now always runs as the logged-in developer (see PhpManager's
    # developer pool). This section only decides *which installed version*
    # handles a given project; it has nothing to do with file ownership.

    def default_php_version(self) -> str:
        return self.php.default_fpm_version()

    def project_preferences(self, project: Path) -> dict[str, str]:
        project = self._validate_project(project)
        key = str(project)
        raw = self.config.projects.get(key, {})
        php = str(raw.get("php", PHP_DEFAULT))
        installed = set(self.php.installed_fpm_versions())
        if php != PHP_DEFAULT and php not in installed:
            php = PHP_DEFAULT
        return {"php": php}

    def set_project_php(self, project: Path, version: str) -> None:
        project = self._validate_project(project)
        if version != PHP_DEFAULT and version not in self.php.installed_fpm_versions():
            raise RuntimeError(f"PHP {version} FPM is not installed")
        prefs = self._project_record(project)
        previous = prefs.get("php", PHP_DEFAULT)
        prefs["php"] = version
        self.config.save()
        if shutil.which("nginx"):
            try:
                self.configure_nginx_sites()
            except Exception:
                prefs["php"] = previous
                self.config.save()
                raise

    def project_php_version(self, project: Path) -> str:
        prefs = self.project_preferences(project)
        if prefs["php"] != PHP_DEFAULT:
            return prefs["php"]
        version = self.default_php_version()
        if not version:
            raise RuntimeError("No installed PHP-FPM version is available for Default")
        return version

    def _project_record(self, project: Path) -> dict[str, str]:
        project = self._validate_project(project)
        key = str(project)
        record = self.config.projects.setdefault(key, {})
        if not isinstance(record, dict):
            record = {}
            self.config.projects[key] = record
        record.setdefault("php", PHP_DEFAULT)
        return record

    def _validate_project(self, project: Path) -> Path:
        candidate = Path(project).expanduser().resolve()
        if not candidate.is_dir() or candidate.parent != self.park_dir:
            raise RuntimeError("Project must be a direct directory inside the configured park directory")
        return candidate

    # ---- Nginx read access -------------------------------------------------
    # PHP-FPM workers run as the developer, so they need no special grant to
    # read or write project files. Nginx (www-data) still serves static files
    # under the document root directly and must stat its way there, so it
    # needs traverse (x) on ancestor directories and read (rX) on the
    # document root only. Nothing outside the document root is touched.

    def ensure_project_readable(self, project: Path) -> None:
        project = self._validate_project(project)
        self._ensure_acl_tool()
        self._ensure_web_user()
        docroot = self.document_root(project)
        self._grant_parent_traverse(docroot)
        self._setfacl(["-R", "-P", "-m", f"u:{WEB_USER}:rX", "--", str(docroot)])
        # Default ACL so files created later (e.g. an uploaded avatar written
        # by the app itself, as the developer user) stay servable by Nginx
        # without a manual re-scan.
        self._set_default_acl_on_dirs(docroot, f"u:{WEB_USER}:r-x")

    def _grant_parent_traverse(self, target: Path) -> None:
        uid = os.getuid()
        # Only modify ancestors owned by the desktop user. System-owned
        # ancestors such as / and /home are left untouched; they are normally
        # traversable already.
        for ancestor in reversed(list(target.parents)):
            if ancestor == Path("/"):
                continue
            try:
                if ancestor.stat().st_uid != uid:
                    continue
            except OSError:
                continue
            self._setfacl(["-m", f"u:{WEB_USER}:x", "--", str(ancestor)])

    def _set_default_acl_on_dirs(self, root: Path, acl: str) -> None:
        result = self.runner.run(
            ["find", str(root), "-type", "d", "-exec", "setfacl", "-m", f"d:{acl}", "--", "{}", "+"],
            timeout=900,
        )
        if not result.ok:
            raise RuntimeError(result.output or f"Could not set default ACLs in {root}")

    def _setfacl(self, args: list[str]) -> None:
        result = self.runner.run(["setfacl", *args], timeout=900)
        if not result.ok:
            raise RuntimeError(result.output or "setfacl failed")

    def _ensure_acl_tool(self) -> None:
        if shutil.which("setfacl"):
            return
        if not self.apt.candidate("acl"):
            raise RuntimeError("The Debian 'acl' package is required to let Nginx read project document roots")
        self.apt.install(["acl"])
        if not shutil.which("setfacl"):
            raise RuntimeError("setfacl is still unavailable after installing the acl package")

    @staticmethod
    def _ensure_web_user() -> None:
        try:
            pwd.getpwnam(WEB_USER)
        except KeyError as exc:
            raise RuntimeError(f"Web-server user '{WEB_USER}' does not exist") from exc

    # ---- DNS ----------------------------------------------------------------

    def dns_strategy(self) -> str:
        if self.systemd.is_active("NetworkManager") or self.systemd.is_active("NetworkManager.service"):
            return "networkmanager"
        return "unsupported"

    def dns_ready(self) -> bool:
        try:
            return socket.gethostbyname(f"nativedev-check.{self.config.domain}") == "127.0.0.1"
        except OSError:
            return False

    def configure_dns(self) -> None:
        if self.dns_strategy() != "networkmanager":
            raise RuntimeError(
                "Automatic wildcard DNS currently supports NetworkManager-based Debian-family desktops only. "
                "NativeDev will not overwrite /etc/resolv.conf as a fallback."
            )
        if not self.apt.is_installed("dnsmasq-base"):
            self.apt.install(["dnsmasq-base"])

        with tempfile.TemporaryDirectory(prefix="nativedev-dns-", dir="/tmp") as temp_dir:
            temp = Path(temp_dir)
            nm_conf = temp / "nativedev-dns.conf"
            nm_dnsmasq = temp / "nativedev-test.conf"
            nm_conf.write_text("[main]\ndns=dnsmasq\n", encoding="utf-8")
            nm_dnsmasq.write_text(
                f"address=/.{self.config.domain}/127.0.0.1\n", encoding="utf-8"
            )
            self.runner.run(["mkdir", "-p", str(NM_CONF.parent), str(NM_DNSMASQ.parent)], privileged=True, check=True)
            self.runner.run(["install", "-m", "0644", str(nm_conf), str(NM_CONF)], privileged=True, check=True)
            self.runner.run(["install", "-m", "0644", str(nm_dnsmasq), str(NM_DNSMASQ)], privileged=True, check=True)
        self.systemd.restart("NetworkManager")

    # ---- Nginx ---------------------------------------------------------------

    def nginx_ready(self) -> bool:
        return NGINX_SITE.exists() and NGINX_ENABLED.exists()

    def render_nginx(self) -> str:
        domain = self.config.domain
        blocks: list[str] = [
            "# Managed by NativeDev. Manual edits may be replaced.\n",
            f"# PHP-FPM workers run as local developer: {self.php.developer_user}\n",
        ]
        for project in self.projects():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project.name):
                continue
            try:
                php_version = self.project_php_version(project)
            except RuntimeError:
                # No PHP-FPM installed yet for this project's selection; skip
                # it rather than failing the whole site file.
                continue
            socket_path = self.php.developer_socket_path(php_version)
            docroot = self.document_root(project)
            escaped_root = str(docroot).replace("$", "\\$")
            ssl = ""
            if self.config.https_enabled:
                ssl = (
                    "    listen 443 ssl;\n"
                    "    listen [::]:443 ssl;\n"
                    f"    ssl_certificate {NGINX_CERT_DIR}/nativedev.pem;\n"
                    f"    ssl_certificate_key {NGINX_CERT_DIR}/nativedev-key.pem;\n"
                )
            blocks.append(
                f"""server {{
    listen 80;
    listen [::]:80;
{ssl}    server_name {project.name}.{domain};
    root {escaped_root};
    index index.php index.html index.htm;

    location / {{
        try_files $uri $uri/ /index.php?$query_string;
    }}

    location ~ \\.php$ {{
        try_files $uri =404;
        include fastcgi_params;
        fastcgi_pass unix:{socket_path};
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_param HTTPS $https if_not_empty;
    }}

    location ~ /\\. {{
        deny all;
    }}
}}
"""
            )
        return "\n".join(blocks)

    def configure_nginx_sites(self) -> None:
        if not shutil.which("nginx"):
            raise RuntimeError("Nginx is not installed")

        projects = self.projects()
        versions_needed: set[str] = set()
        for project in projects:
            try:
                versions_needed.add(self.project_php_version(project))
            except RuntimeError:
                continue

        # Critical ownership rule: *.test never targets Debian/Sury's default
        # www-data pool. Ensure NativeDev's developer-user pool exists for
        # every PHP version actually selected by a project.
        for version in versions_needed:
            self.php.ensure_developer_pool(version)

        for project in projects:
            self.ensure_project_readable(project)

        content = self.render_nginx()
        previous = NGINX_SITE.read_text(encoding="utf-8") if NGINX_SITE.exists() else None

        with tempfile.TemporaryDirectory(prefix="nativedev-nginx-", dir="/tmp") as temp_dir:
            source = Path(temp_dir) / "nativedev-sites.conf"
            source.write_text(content, encoding="utf-8")
            self.runner.run(["install", "-m", "0644", str(source), str(NGINX_SITE)], privileged=True, check=True)
            self.runner.run(["ln", "-sfn", str(NGINX_SITE), str(NGINX_ENABLED)], privileged=True, check=True)

            check = self.runner.run(["nginx", "-t"], privileged=True, timeout=30)
            if not check.ok:
                if previous is None:
                    self.runner.run(["rm", "-f", str(NGINX_SITE), str(NGINX_ENABLED)], privileged=True)
                else:
                    rollback = Path(temp_dir) / "rollback.conf"
                    rollback.write_text(previous, encoding="utf-8")
                    self.runner.run(["install", "-m", "0644", str(rollback), str(NGINX_SITE)], privileged=True)
                raise RuntimeError(check.output or "nginx -t failed; configuration rolled back")
        self.systemd.reload("nginx")

    def mkcert_installed(self) -> bool:
        return bool(shutil.which("mkcert"))

    def trust_mkcert_ca(self) -> None:
        if not self.mkcert_installed():
            raise RuntimeError("mkcert is not installed")
        result = self.runner.run(["mkcert", "-install"], timeout=180)
        if not result.ok:
            raise RuntimeError(result.output or "mkcert CA installation failed")

    def enable_https(self) -> None:
        if not self.mkcert_installed():
            raise RuntimeError("mkcert is not installed")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="nativedev-cert-", dir="/tmp") as temp_dir:
            cert = Path(temp_dir) / "nativedev.pem"
            key = Path(temp_dir) / "nativedev-key.pem"
            domain = self.config.domain
            result = self.runner.run(
                [
                    "mkcert",
                    "-cert-file",
                    str(cert),
                    "-key-file",
                    str(key),
                    f"*.{domain}",
                    domain,
                    "localhost",
                    "127.0.0.1",
                ],
                timeout=180,
            )
            if not result.ok:
                raise RuntimeError(result.output or "Certificate generation failed")
            self.runner.run(["mkdir", "-p", str(NGINX_CERT_DIR)], privileged=True, check=True)
            self.runner.run(["install", "-m", "0644", str(cert), str(NGINX_CERT_DIR / "nativedev.pem")], privileged=True, check=True)
            # Mode 0644, not 0600: the file is written by the privileged
            # helper as root:root, and Nginx's worker process (www-data) must
            # be able to read the key to terminate TLS. This is a locally
            # generated mkcert leaf key for *.test only (mkcert's private root
            # CA key, which is what actually matters for trust, stays under
            # the developer's own mkcert state directory and is never touched
            # here) -- world-readable is an accepted, standard trade-off for
            # local-only development certificates.
            self.runner.run(["install", "-m", "0644", str(key), str(NGINX_CERT_DIR / "nativedev-key.pem")], privileged=True, check=True)
        self.config.https_enabled = True
        self.config.save()
        self.configure_nginx_sites()
