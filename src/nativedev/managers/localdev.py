from __future__ import annotations

import os
import pwd
import re
import shutil
import socket
import tempfile
import time
from pathlib import Path

from ..config import AppConfig, STATE_DIR
from ..system import AptManager, CommandRunner, SystemdManager
from .php import PhpManager


NGINX_SITE = Path("/etc/nginx/sites-available/nativedev-sites.conf")
NGINX_ENABLED = Path("/etc/nginx/sites-enabled/nativedev-sites.conf")
NM_CONF = Path("/etc/NetworkManager/conf.d/nativedev-dns.conf")
NM_DNSMASQ = Path("/etc/NetworkManager/dnsmasq.d/nativedev-test.conf")
NGINX_CERT_DIR = Path("/etc/nginx/nativedev")
NGINX_CERT = NGINX_CERT_DIR / "nativedev.pem"
NGINX_KEY = NGINX_CERT_DIR / "nativedev-key.pem"
NGINX_WILDCARD_MARKER = "# NativeDev wildcard router v1"
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
        """Persist only the project's desired PHP selection.

        Cross-manager side effects (Nginx regeneration/rollback) belong to the
        application controller so this manager remains independently reusable.
        """
        project = self._validate_project(project)
        if version != PHP_DEFAULT and version not in self.php.installed_fpm_versions():
            raise RuntimeError(f"PHP {version} FPM is not installed")
        prefs = self._project_record(project)
        prefs["php"] = version
        self.config.save()

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
    # PHP-FPM workers run as the developer. Nginx (www-data) still needs read
    # access for static assets. Wildcard routing means projects may appear after
    # the Nginx config was generated, so NativeDev prepares the park directory
    # once with an inheritable ACL. A newly-created direct child then inherits
    # the www-data read/traverse entry without a watcher or Nginx regeneration.

    def ensure_park_readable(self) -> None:
        root = self.park_dir
        if not root.is_dir():
            raise RuntimeError(f"Park directory does not exist: {root}")
        self._ensure_acl_tool()
        self._ensure_web_user()
        self._grant_parent_traverse(root)

        # The park directory itself needs an access ACL for traversal plus a
        # default ACL. New project directories inherit both the access entry and
        # the default entry, and that default entry keeps propagating to their
        # descendants. This is what makes a new project work while NativeDev is
        # closed, without a watcher or background service.
        self._setfacl(["-m", f"u:{WEB_USER}:r-x", "--", str(root)])
        self._setfacl(["-m", f"d:u:{WEB_USER}:r-x", "--", str(root)])

        # Existing projects pre-date the park default ACL, so prepare their
        # current document roots once. When /public exists, existing source files
        # outside /public keep the narrower previous ACL behaviour.
        for project in self.projects():
            self.ensure_project_readable(project)

    def ensure_project_readable(self, project: Path) -> None:
        project = self._validate_project(project)
        self._ensure_acl_tool()
        self._ensure_web_user()
        docroot = self.document_root(project)
        self._grant_parent_traverse(docroot)

        # A parked project may contain Docker/container-created files owned by
        # root or another UID. NativeDev does not own those files and must not
        # try to rewrite their ACLs.
        self._set_owned_tree_acl(docroot, f"u:{WEB_USER}:rX")

        # Default ACL so files created later by the developer remain servable.
        self._set_default_acl_on_owned_dirs(docroot, f"u:{WEB_USER}:r-x")

    def _grant_parent_traverse(self, target: Path) -> None:
        uid = os.getuid()
        for ancestor in reversed(list(target.parents)):
            if ancestor == Path("/"):
                continue
            try:
                if ancestor.stat().st_uid != uid:
                    continue
            except OSError:
                continue
            self._setfacl(["-m", f"u:{WEB_USER}:x", "--", str(ancestor)])

    def _owned_find_prefix(self, root: Path) -> list[str]:
        uid = str(os.getuid())
        return [
            "find", str(root), "-xdev",
            "(",
            "-type", "l",
            "-o",
            "(", "-type", "d", "!", "-uid", uid, ")",
            ")",
            "-prune", "-o",
            "-uid", uid,
        ]

    def _set_owned_tree_acl(self, root: Path, acl: str) -> None:
        result = self.runner.run(
            [*self._owned_find_prefix(root), "-exec", "setfacl", "-m", acl, "--", "{}", "+"],
            timeout=900,
        )
        if not result.ok:
            raise RuntimeError(result.output or f"Could not grant Nginx read ACLs in {root}")

    def _set_default_acl_on_owned_dirs(self, root: Path, acl: str) -> None:
        result = self.runner.run(
            [
                *self._owned_find_prefix(root),
                "-type", "d",
                "-exec", "setfacl", "-m", f"d:{acl}", "--", "{}", "+",
            ],
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

    def _wait_for_dns_ready(self, timeout: float = 8.0, interval: float = 0.25) -> bool:
        # `nmcli general reload dns-full` restarts NetworkManager's DNS plugin.
        # NetworkManager documents that this shortly interrupts name
        # resolution, so an immediate one-shot lookup is racy even when the
        # resulting configuration is correct. Retry for a small bounded window
        # instead of rolling back a healthy configuration on the first lookup.
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self.dns_ready():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(0.01, interval))

    def _reload_networkmanager_dns(self) -> None:
        # Do not restart NetworkManager: that can bounce active connections,
        # delay connectivity after boot, and interfere with VPN/Wi-Fi state.
        # Reload only NetworkManager.conf and its DNS plugin.
        self.runner.run(["nmcli", "general", "reload", "conf"], privileged=True, check=True, timeout=30)
        self.runner.run(["nmcli", "general", "reload", "dns-full"], privileged=True, check=True, timeout=30)

    def configure_dns(self) -> None:
        if self.dns_strategy() != "networkmanager":
            raise RuntimeError(
                "Automatic wildcard DNS currently supports NetworkManager-based Debian-family desktops only. "
                "NativeDev will not overwrite /etc/resolv.conf as a fallback."
            )
        if not self.apt.is_installed("dnsmasq-base"):
            self.apt.install(["dnsmasq-base"])

        previous_conf = NM_CONF.read_text(encoding="utf-8") if NM_CONF.exists() else None
        previous_dnsmasq = NM_DNSMASQ.read_text(encoding="utf-8") if NM_DNSMASQ.exists() else None

        with tempfile.TemporaryDirectory(prefix="nativedev-dns-", dir="/tmp") as temp_dir:
            temp = Path(temp_dir)
            nm_conf = temp / "nativedev-dns.conf"
            nm_dnsmasq = temp / "nativedev-test.conf"
            nm_conf.write_text("[main]\ndns=dnsmasq\n", encoding="utf-8")
            nm_dnsmasq.write_text(
                f"address=/.{self.config.domain}/127.0.0.1\n", encoding="utf-8"
            )
            self.runner.run(["mkdir", "-p", str(NM_CONF.parent), str(NM_DNSMASQ.parent)], privileged=True, check=True)

            try:
                self.runner.run(["install", "-m", "0644", str(nm_conf), str(NM_CONF)], privileged=True, check=True)
                self.runner.run(["install", "-m", "0644", str(nm_dnsmasq), str(NM_DNSMASQ)], privileged=True, check=True)
                self._reload_networkmanager_dns()
                if not self._wait_for_dns_ready():
                    raise RuntimeError(
                        f"*.{self.config.domain} did not resolve to 127.0.0.1 within 8 seconds after DNS reload"
                    )
            except Exception as exc:
                # Restore only NativeDev-owned files. Never rewrite resolv.conf
                # or connection profiles while recovering from a failed setup.
                for old, dest, name in (
                    (previous_conf, NM_CONF, "rollback-nm.conf"),
                    (previous_dnsmasq, NM_DNSMASQ, "rollback-dnsmasq.conf"),
                ):
                    if old is None:
                        self.runner.run(["rm", "-f", str(dest)], privileged=True, check=True)
                    else:
                        rollback = temp / name
                        rollback.write_text(old, encoding="utf-8")
                        self.runner.run(["install", "-m", "0644", str(rollback), str(dest)], privileged=True, check=True)
                try:
                    self._reload_networkmanager_dns()
                except Exception as rollback_exc:
                    raise RuntimeError(f"DNS setup failed and DNS reload rollback also failed: {rollback_exc}") from exc
                raise

    # ---- Nginx ---------------------------------------------------------------

    def nginx_ready(self) -> bool:
        if not (NGINX_SITE.exists() and NGINX_ENABLED.exists()):
            return False
        try:
            return NGINX_WILDCARD_MARKER in NGINX_SITE.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False

    def nginx_managed(self) -> bool:
        """Return whether NativeDev has already created any Nginx site state."""
        return NGINX_SITE.exists() or NGINX_ENABLED.exists()

    @staticmethod
    def _nginx_quote(value: str) -> str:
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise RuntimeError("Nginx paths may not contain control characters")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
        return f'"{escaped}"'

    @staticmethod
    def _nginx_template_path(prefix: str, variable: str) -> str:
        """Quote a literal path prefix while preserving one Nginx variable."""
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in prefix):
            raise RuntimeError("Nginx paths may not contain control characters")
        escaped = prefix.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
        return f'"{escaped}${variable}"'

    def _project_hostname(self, project: Path) -> str | None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project.name):
            return None
        return f"{project.name}.{self.config.domain}".lower()

    def _known_project_routes(self) -> dict[str, Path]:
        routes: dict[str, Path] = {}
        for project in self.projects():
            host = self._project_hostname(project)
            if not host:
                continue
            previous = routes.get(host)
            if previous is not None and previous != project:
                raise RuntimeError(
                    f"Project hostname collision: {previous.name} and {project.name} both map to {host}"
                )
            routes[host] = project
        return routes

    def https_ready(self) -> bool:
        """Return True only when HTTPS is enabled *and* both TLS files exist."""
        return bool(self.config.https_enabled and NGINX_CERT.is_file() and NGINX_KEY.is_file())

    def _reconcile_https_state(self) -> None:
        """Disable stale HTTPS state when its NativeDev-owned TLS files vanished."""
        if self.config.https_enabled and not (NGINX_CERT.is_file() and NGINX_KEY.is_file()):
            self.config.https_enabled = False
            self.config.save()

    def render_nginx(self) -> str:
        """Render one persistent wildcard router instead of one server per project."""
        domain = self.config.domain
        default_version = self.default_php_version()
        if not default_version:
            return (
                f"{NGINX_WILDCARD_MARKER}\n"
                "# Managed by NativeDev. Manual edits may be replaced.\n"
                "# No PHP-FPM runtime is currently available.\n"
            )

        default_socket = f"unix:{self.php.developer_socket_path(default_version)}"
        routes = self._known_project_routes()
        dynamic_path = self._nginx_template_path(str(self.park_dir) + os.sep, "nativedev_auto_project")
        domain_re = re.escape(domain)

        project_map = [
            "map $host $nativedev_project_dir {",
            '    default "";',
        ]
        for host, project in sorted(routes.items()):
            # Normal lowercase DNS-safe projects are intentionally *not* baked
            # into the file. Their host always resolves through the live park
            # fallback, so delete/recreate/rename workflows stay zero-reload.
            # Exact entries exist only to preserve already-known legacy names
            # such as Shop, foo_bar or foo.bar on case-sensitive filesystems.
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", project.name):
                project_map.append(f"    {host} {self._nginx_quote(str(project))};")
        project_map.append(
            f"    ~^(?<nativedev_auto_project>[a-z0-9][a-z0-9-]*)\\.{domain_re}$ {dynamic_path};"
        )
        project_map.append("}")

        php_map = [
            "map $host $nativedev_php_backend {",
            f"    default {self._nginx_quote(default_socket)};",
        ]
        installed = set(self.php.installed_fpm_versions())
        for host, project in sorted(routes.items()):
            version = self.project_preferences(project)["php"]
            if version == PHP_DEFAULT or version not in installed:
                continue
            socket_value = f"unix:{self.php.developer_socket_path(version)}"
            php_map.append(f"    {host} {self._nginx_quote(socket_value)};")
        php_map.append("}")

        ssl = ""
        if self.https_ready():
            ssl = (
                "    listen 443 ssl;\n"
                "    listen [::]:443 ssl;\n"
                f"    ssl_certificate {NGINX_CERT};\n"
                f"    ssl_certificate_key {NGINX_KEY};\n"
            )

        server = f"""server {{
    listen 80;
    listen [::]:80;
{ssl}    server_name ~^.+\\.{domain_re}$;

    if ($nativedev_project_dir = "") {{ return 404; }}
    if (!-d $nativedev_project_dir) {{ return 404; }}

    set $nativedev_document_root $nativedev_project_dir;
    if (-d "$nativedev_project_dir/public") {{
        set $nativedev_document_root "$nativedev_project_dir/public";
    }}

    root $nativedev_document_root;
    index index.php index.html index.htm;

    location / {{
        try_files $uri $uri/ /index.php?$query_string;
    }}

    location ~ \\.php$ {{
        try_files $uri =404;
        include fastcgi_params;
        fastcgi_pass $nativedev_php_backend;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_param HTTPS $https if_not_empty;
    }}

    location ~ /\\. {{
        deny all;
    }}
}}
"""
        return "\n".join(
            [
                NGINX_WILDCARD_MARKER,
                "# Managed by NativeDev. Manual edits may be replaced.",
                f"# PHP-FPM workers run as local developer: {self.php.developer_user}",
                "# New lowercase project directories under the park are routable without regeneration.",
                "",
                *project_map,
                "",
                *php_map,
                "",
                server,
            ]
        )

    def configure_nginx_sites(self) -> None:
        if not shutil.which("nginx"):
            raise RuntimeError("Nginx is not installed")

        self._reconcile_https_state()
        default_version = self.default_php_version()
        if not default_version:
            raise RuntimeError("Install and start a PHP-FPM version before configuring wildcard *.test routing")

        versions_needed: set[str] = {default_version}
        installed = set(self.php.installed_fpm_versions())
        for project in self.projects():
            version = self.project_preferences(project)["php"]
            if version != PHP_DEFAULT and version in installed:
                versions_needed.add(version)

        for version in versions_needed:
            self.php.ensure_developer_pool(version)

        # Existing roots are fixed now; a default ACL on the park makes future
        # projects inherit Nginx read/traverse access automatically.
        self.ensure_park_readable()

        content = self.render_nginx()
        previous = NGINX_SITE.read_text(encoding="utf-8") if NGINX_SITE.exists() else None
        enabled_before = NGINX_ENABLED.is_symlink()
        if NGINX_ENABLED.exists() and not enabled_before:
            raise RuntimeError(f"Refusing to replace non-symlink path: {NGINX_ENABLED}")
        if enabled_before and NGINX_ENABLED.resolve() != NGINX_SITE.resolve():
            raise RuntimeError(f"Refusing to replace unexpected symlink target: {NGINX_ENABLED}")

        with tempfile.TemporaryDirectory(prefix="nativedev-nginx-", dir="/tmp") as temp_dir:
            source = Path(temp_dir) / "nativedev-sites.conf"
            source.write_text(content, encoding="utf-8")
            self.runner.run(["install", "-m", "0644", str(source), str(NGINX_SITE)], privileged=True, check=True)
            self.runner.run(["ln", "-sfn", str(NGINX_SITE), str(NGINX_ENABLED)], privileged=True, check=True)

            check = self.runner.run(["nginx", "-t"], privileged=True, timeout=30)
            if not check.ok:
                if previous is None:
                    self.runner.run(["rm", "-f", str(NGINX_SITE)], privileged=True, check=True)
                else:
                    rollback = Path(temp_dir) / "rollback.conf"
                    rollback.write_text(previous, encoding="utf-8")
                    self.runner.run(["install", "-m", "0644", str(rollback), str(NGINX_SITE)], privileged=True, check=True)
                if not enabled_before:
                    self.runner.run(["rm", "-f", str(NGINX_ENABLED)], privileged=True, check=True)
                rollback_check = self.runner.run(["nginx", "-t"], privileged=True, timeout=30)
                if not rollback_check.ok:
                    raise RuntimeError(
                        (check.output or "nginx -t failed")
                        + "; rollback was attempted but the restored Nginx configuration is also invalid: "
                        + (rollback_check.output or "nginx -t failed after rollback")
                    )
                raise RuntimeError(check.output or "nginx -t failed; configuration rolled back")
        if self.systemd.is_active("nginx"):
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
            # Nginx's privileged master process loads the private key before
            # workers handle requests, so the leaf key does not need to be
            # world-readable. Keep the NativeDev-owned key root-only.
            self.runner.run(["install", "-m", "0600", str(key), str(NGINX_CERT_DIR / "nativedev-key.pem")], privileged=True, check=True)
        self.config.https_enabled = True
        self.config.save()
        self.configure_nginx_sites()
