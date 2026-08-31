from __future__ import annotations

import os
import re
import shutil
import socket
import tempfile
from pathlib import Path

from ..config import AppConfig, STATE_DIR
from ..system import AptManager, CommandRunner, SystemdManager


NGINX_SITE = Path("/etc/nginx/sites-available/nativedev-sites.conf")
NGINX_ENABLED = Path("/etc/nginx/sites-enabled/nativedev-sites.conf")
NM_CONF = Path("/etc/NetworkManager/conf.d/nativedev-dns.conf")
NM_DNSMASQ = Path("/etc/NetworkManager/dnsmasq.d/nativedev-test.conf")
NGINX_CERT_DIR = Path("/etc/nginx/nativedev")


class LocalDevManager:
    def __init__(
        self,
        runner: CommandRunner,
        apt: AptManager,
        systemd: SystemdManager,
        config: AppConfig,
    ):
        self.runner = runner
        self.apt = apt
        self.systemd = systemd
        self.config = config

    @property
    def park_dir(self) -> Path:
        return Path(self.config.park_dir).expanduser().resolve()

    def projects(self) -> list[Path]:
        root = self.park_dir
        if not root.is_dir():
            return []
        return sorted(
            [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")],
            key=lambda p: p.name.lower(),
        )

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

    def nginx_ready(self) -> bool:
        return NGINX_SITE.exists() and NGINX_ENABLED.exists()

    def render_nginx(self) -> str:
        php_version = self.config.php_version.strip()
        if not php_version:
            raise RuntimeError("Choose a PHP version for local sites first")
        socket_path = f"/run/php/php{php_version}-fpm.sock"
        domain = self.config.domain
        blocks: list[str] = [
            "# Managed by NativeDev. Manual edits may be replaced.\n",
        ]
        for project in self.projects():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project.name):
                continue
            docroot = project / "public" if (project / "public").is_dir() else project
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
            self.runner.run(["install", "-m", "0600", str(key), str(NGINX_CERT_DIR / "nativedev-key.pem")], privileged=True, check=True)
        self.config.https_enabled = True
        self.config.save()
        self.configure_nginx_sites()
