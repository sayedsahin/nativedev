from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Callable, TypeVar

from .managers.localdev import LocalDevManager
from .managers.php import PhpManager
from .managers.php_ini import PhpIniManager
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

    def __init__(self, php: PhpManager, localdev: LocalDevManager, node: NodeManager | None = None, php_ini: PhpIniManager | None = None):
        self.php = php
        self.localdev = localdev
        self.node = node
        self.php_ini = php_ini
        self._mutation_lock = threading.RLock()

    def run_mutation(self, fn: Callable[..., T], *args, **kwargs) -> T:
        with self._mutation_lock:
            return fn(*args, **kwargs)

    def _reconcile_managed_nginx(self) -> None:
        """Refresh NativeDev-owned Nginx state without creating it implicitly."""
        if self.localdev.nginx_managed() and shutil.which("nginx"):
            self.localdev.configure_nginx_sites()

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

    def install_debian_php(self) -> str:
        with self._mutation_lock:
            version = self.php.install_debian_default()
            try:
                self._reconcile_managed_nginx()
            except Exception as exc:
                raise RuntimeError(
                    f"Debian PHP {version} was installed, but NativeDev Nginx reconciliation failed: {exc}"
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

    def enable_sury_multi_php(self) -> str:
        """One-way migration from Debian system PHP to Sury multi-PHP."""
        with self._mutation_lock:
            version = self.php.enable_sury_multi_php()
            try:
                self._reconcile_managed_nginx()
            except Exception as exc:
                raise RuntimeError(
                    f"Sury PHP migration completed, but NativeDev Nginx reconciliation failed: {exc}"
                ) from exc
            return version

    def enable_nvm_multi_node(self) -> str:
        """One-way migration from Debian system Node to NVM multi-Node."""
        with self._mutation_lock:
            if self.node is None:
                raise RuntimeError("Node manager is not available")
            return self.node.enable_nvm_multi_node()

