from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Callable, TypeVar

from .managers.localdev import LocalDevManager
from .managers.php import PhpManager
from .managers.php_ini import PhpIniManager
from .managers.database_access import DatabaseAccessManager
from .services import ComponentSpec, ServiceManager
from .managers.node import NodeManager


T = TypeVar("T")


class NativeDevController:
    """Application-level orchestration for NativeDev mutations.

    Managers remain reusable infrastructure/domain adapters. Cross-manager
    invariants live here so GUI (and a future CLI) do not have to remember when
    a PHP or project change also requires regenerating NativeDev's Nginx state.

    All mutations pass through one re-entrant lock. The GTK worker also uses a
    single mutation executor, while this lock keeps the invariant intact for
    non-GUI callers as well.
    """

    def __init__(
        self,
        php: PhpManager,
        localdev: LocalDevManager,
        node: NodeManager | None = None,
        php_ini: PhpIniManager | None = None,
        services: ServiceManager | None = None,
        database_access: DatabaseAccessManager | None = None,
    ):
        self.php = php
        self.localdev = localdev
        self.node = node
        self.php_ini = php_ini
        self.services = services
        self.database_access = database_access
        self._mutation_lock = threading.RLock()

    def run_mutation(self, fn: Callable[..., T], *args, **kwargs) -> T:
        with self._mutation_lock:
            return fn(*args, **kwargs)

    def _reconcile_managed_nginx(self) -> None:
        """Refresh NativeDev-owned Nginx state without creating it implicitly."""
        if self.localdev.nginx_managed() and shutil.which("nginx"):
            self.localdev.configure_nginx_sites()

    def update_localdev_settings(self, park_dir: str, domain: str) -> None:
        """Persist Local Development settings and reconcile derived infrastructure.

        TLD changes update NativeDev's NetworkManager wildcard DNS when that
        integration is supported. Existing wildcard Nginx state is rebuilt for
        either TLD or park changes, including the new park ACL. When HTTPS is
        enabled, its wildcard certificate is regenerated for the new TLD.

        If any required reconciliation fails, restore both the previous config
        and the previous NativeDev-managed infrastructure as best as possible.
        """
        with self._mutation_lock:
            config = self.localdev.config
            previous_park = config.park_dir
            previous_domain = config.domain
            park_changed = previous_park != park_dir
            domain_changed = previous_domain != domain
            if not park_changed and not domain_changed:
                return

            nginx_managed = self.localdev.nginx_managed()
            https_enabled = bool(config.https_enabled)
            dns_strategy = self.localdev.dns_strategy() if domain_changed else ""

            config.park_dir = park_dir
            config.domain = domain
            try:
                config.save()
            except Exception:
                config.park_dir = previous_park
                config.domain = previous_domain
                raise

            try:
                if domain_changed and dns_strategy == "networkmanager":
                    self.localdev.configure_dns()

                # HTTPS certificates contain the TLD, so a domain change must
                # regenerate the NativeDev wildcard certificate as well.
                if domain_changed and https_enabled:
                    self.localdev.enable_https()
                elif nginx_managed and (park_changed or domain_changed):
                    self.localdev.configure_nginx_sites()
            except Exception as exc:
                config.park_dir = previous_park
                config.domain = previous_domain
                config.save()

                rollback_errors: list[str] = []
                if domain_changed and dns_strategy == "networkmanager":
                    try:
                        self.localdev.configure_dns()
                    except Exception as rollback_exc:
                        rollback_errors.append(f"DNS rollback failed: {rollback_exc}")

                try:
                    if domain_changed and https_enabled:
                        self.localdev.enable_https()
                    elif nginx_managed and (park_changed or domain_changed):
                        self.localdev.configure_nginx_sites()
                except Exception as rollback_exc:
                    rollback_errors.append(f"Nginx/HTTPS rollback failed: {rollback_exc}")

                if rollback_errors:
                    raise RuntimeError(
                        f"Local Development settings could not be applied ({exc}); "
                        + "; ".join(rollback_errors)
                    ) from exc
                raise RuntimeError(
                    f"Local Development settings could not be applied and were rolled back: {exc}"
                ) from exc


    def install_component(self, spec: ComponentSpec) -> None:
        """Install a system component and provision local DB access when applicable."""
        with self._mutation_lock:
            if self.services is None:
                raise RuntimeError("Service manager is not available")
            self.services.install(spec)
            if self.database_access is not None and self.database_access.supports(spec.key):
                try:
                    self.database_access.ensure_after_install(spec.key)
                except Exception as exc:
                    raise RuntimeError(
                        f"{spec.title} was installed, but NativeDev local database access could not be configured: {exc}"
                    ) from exc

    def uninstall_component(self, spec: ComponentSpec) -> None:
        """Uninstall runtime packages and forget NativeDev's saved DB credential."""
        with self._mutation_lock:
            if self.services is None:
                raise RuntimeError("Service manager is not available")
            self.services.uninstall(spec)
            if spec.key in {"mariadb", "postgresql"} and self.database_access is not None:
                self.database_access.forget(spec.key)

    def use_existing_database_access(self, key: str, password: str):
        with self._mutation_lock:
            if self.database_access is None:
                raise RuntimeError("Database access manager is not available")
            return self.database_access.use_existing_account(key, password)

    def create_database_access(self, key: str, admin_password: str | None = None):
        with self._mutation_lock:
            if self.database_access is None:
                raise RuntimeError("Database access manager is not available")
            return self.database_access.create_local_access(key, admin_password=admin_password)


    def change_database_password(self, key: str, password: str):
        with self._mutation_lock:
            if self.database_access is None:
                raise RuntimeError("Database access manager is not available")
            return self.database_access.change_password(key, password)

    def reset_database_password(self, key: str):
        with self._mutation_lock:
            if self.database_access is None:
                raise RuntimeError("Database access manager is not available")
            return self.database_access.reset_password(key)

    def set_default_php(self, version: str) -> None:
        with self._mutation_lock:
            previous = self.php.cli_version()
            self.php.set_cli_default(version)
            try:
                self._reconcile_managed_nginx()
            except Exception as exc:
                # update-alternatives is cheap and reversible. Keep the CLI and
                # generated *.test routing consistent when reconciliation fails.
                rollback_error = None
                if previous and previous != version:
                    try:
                        self.php.set_cli_default(previous)
                        self._reconcile_managed_nginx()
                    except Exception as rollback_exc:
                        rollback_error = rollback_exc
                if rollback_error is not None:
                    raise RuntimeError(
                        f"PHP default change failed during Nginx reconciliation ({exc}); "
                        f"rollback also failed ({rollback_error})"
                    ) from exc
                raise

    def install_system_php(self) -> str:
        with self._mutation_lock:
            version = self.php.install_system_default()
            try:
                self._reconcile_managed_nginx()
            except Exception as exc:
                raise RuntimeError(
                    f"System PHP {version} was installed, but NativeDev Nginx reconciliation failed: {exc}"
                ) from exc
            return version

    def install_php(self, version: str) -> None:
        with self._mutation_lock:
            self.php.install_version(version)
            try:
                self._reconcile_managed_nginx()
            except Exception as exc:
                raise RuntimeError(
                    f"PHP {version} was installed, but NativeDev Nginx reconciliation failed: {exc}"
                ) from exc

    def uninstall_php(self, version: str) -> None:
        with self._mutation_lock:
            detached_ini = False
            if self.php_ini is not None and self.php_ini.has_active_override(version):
                self.php_ini.detach_runtime(version)
                detached_ini = True
            try:
                self.php.uninstall_version(version)
            except Exception as exc:
                if detached_ini:
                    try:
                        self.php_ini.restore_profile(version)
                    except Exception as rollback_exc:
                        raise RuntimeError(
                            f"PHP {version} uninstall failed ({exc}); NativeDev INI rollback also failed ({rollback_exc})"
                        ) from exc
                raise
            # Project preferences automatically fall back to Default when a
            # pinned FPM version disappears; regenerate any managed site file.
            try:
                self._reconcile_managed_nginx()
            except Exception as exc:
                raise RuntimeError(
                    f"PHP {version} was uninstalled, but NativeDev Nginx reconciliation failed: {exc}"
                ) from exc

    def repair_php_fpm(self, version: str) -> None:
        with self._mutation_lock:
            self.php.repair_fpm(version)
            try:
                self._reconcile_managed_nginx()
            except Exception as exc:
                raise RuntimeError(
                    f"PHP {version} FPM was repaired, but NativeDev Nginx reconciliation failed: {exc}"
                ) from exc

    def set_project_php(self, project: Path, version: str) -> None:
        with self._mutation_lock:
            previous = self.localdev.project_preferences(project)["php"]
            self.localdev.set_project_php(project, version)
            if not shutil.which("nginx"):
                return
            try:
                # Changing a project's PHP selection is an explicit routing
                # operation, so retain the previous behaviour of generating
                # Nginx on first use when Nginx is already installed.
                self.localdev.configure_nginx_sites()
            except Exception as exc:
                rollback_error = None
                try:
                    self.localdev.set_project_php(project, previous)
                    self.localdev.configure_nginx_sites()
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
                if rollback_error is not None:
                    raise RuntimeError(
                        f"Project PHP routing failed ({exc}); rollback also failed ({rollback_error})"
                    ) from exc
                raise

    def enable_multi_php(self) -> str:
        """One-way migration from System PHP to the distro-appropriate Multi-PHP repository."""
        with self._mutation_lock:
            version = self.php.enable_multi_php()
            try:
                self._reconcile_managed_nginx()
            except Exception as exc:
                raise RuntimeError(
                    f"Multi-PHP migration completed, but NativeDev Nginx reconciliation failed: {exc}"
                ) from exc
            return version

    def enable_nvm_multi_node(self) -> str:
        """One-way migration from System Node to NVM multi-Node."""
        with self._mutation_lock:
            if self.node is None:
                raise RuntimeError("Node manager is not available")
            return self.node.enable_nvm_multi_node()

