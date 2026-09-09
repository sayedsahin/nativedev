from __future__ import annotations

from dataclasses import dataclass
import re
import shutil
import time
from collections.abc import Iterable

from .controller import NativeDevController
from .managers.developer_tools import (
    DEVELOPER_TOOL_BY_KEY,
    DEVELOPER_WEB_TOOLS,
    DeveloperToolManager,
    DeveloperToolSpec,
)


@dataclass(frozen=True, slots=True)
class PhpUninstallPreview:
    """Read-only plan shown by the GUI before a PHP uninstall."""

    version: str
    tool_keys: tuple[str, ...]
    tool_titles: tuple[str, ...]
    remaining_fpm: tuple[str, ...]
    replacement: str
    removing_default: bool

    @property
    def has_tool_users(self) -> bool:
        return bool(self.tool_keys)

    @property
    def needs_tool_removal_confirmation(self) -> bool:
        return bool(self.tool_keys and not self.remaining_fpm)


_DEPENDENCY_RE = re.compile(
    r"^\s*\|?(?:PreDepends|Depends):\s*(?:<)?([^>\s]+)"
)
_VERSIONED_PHP_RE = re.compile(r"^php\d+\.\d+(?:-(.+))?$")
_CONFIRMATION_TTL_SECONDS = 600.0


def _tool_users(
    developer_tools: DeveloperToolManager | None,
    version: str,
) -> tuple[DeveloperToolSpec, ...]:
    if developer_tools is None:
        return ()

    return tuple(
        spec
        for spec in DEVELOPER_WEB_TOOLS
        if developer_tools.apt.is_installed(spec.package)
        and developer_tools.effective_php(spec.key) == version
    )


def build_php_uninstall_preview(
    controller_or_php,
    developer_tools_or_version,
    version: str | None = None,
) -> PhpUninstallPreview:
    """Build a read-only uninstall plan.

    Supports both the current controller API::

        build_php_uninstall_preview(controller, "8.4")

    and the earlier public/test API::

        build_php_uninstall_preview(php, developer_tools, "8.4")

    Keeping both signatures avoids breaking callers while the application
    itself continues to use the controller-based workflow.
    """

    if version is None:
        controller = controller_or_php
        php = controller.php
        developer_tools = controller.developer_tools
        target = str(developer_tools_or_version)
    else:
        php = controller_or_php
        developer_tools = developer_tools_or_version
        target = str(version)

    users = _tool_users(developer_tools, target)

    remaining = tuple(
        sorted(
            (
                installed
                for installed in php.installed_fpm_versions()
                if installed != target
            ),
            key=php._version_key,
            reverse=True,
        )
    )

    current_default = php.cli_version()
    replacement = ""

    if users and remaining:
        if current_default != target and current_default in remaining:
            # Removing a non-default PHP: move affected tools to the current
            # system Default PHP when that Default has an installed FPM.
            replacement = current_default
        else:
            # Removing the current Default: choose a remaining FPM only for the
            # Developer Tools. NativeDev intentionally does NOT call
            # update-alternatives here; package maintainer scripts/system policy
            # decide the new system Default after the target PHP is removed.
            replacement = remaining[0]

    return PhpUninstallPreview(
        version=target,
        tool_keys=tuple(spec.key for spec in users),
        tool_titles=tuple(spec.title for spec in users),
        remaining_fpm=remaining,
        replacement=replacement,
        removing_default=current_default == target,
    )


def _versioned_dependency_candidates(
    package: str,
    version: str,
) -> tuple[str, ...]:
    """Map generic/versioned PHP dependencies to one replacement PHP version."""

    package = package.strip().strip("<>").split(":", 1)[0]
    if not package:
        return ()

    match = _VERSIONED_PHP_RE.fullmatch(package)
    if match:
        suffix = match.group(1)
        if suffix:
            return (f"php{version}-{suffix}",)
        return (f"php{version}-fpm", f"php{version}-common")

    if package == "php":
        # These tools are served through FPM. phpX.Y-fpm pulls in the matching
        # common/runtime package and satisfies the web runtime requirement.
        return (f"php{version}-fpm",)
    if package == "php-fpm":
        return (f"php{version}-fpm",)
    if package == "php-cli":
        return (f"php{version}-cli",)
    if package == "php-common":
        return (f"php{version}-common",)
    if package == "php-json":
        # JSON is part of the runtime/common package on supported PHP versions.
        return (f"php{version}-common",)
    if package.startswith("php-"):
        return (f"php{version}-{package[4:]}",)

    return ()


def _tool_php_requirements(
    developer_tools: DeveloperToolManager,
    spec: DeveloperToolSpec,
    replacement: str,
) -> set[str]:
    """Return installable replacement-version PHP packages for one tool."""

    required = {
        f"php{replacement}-fpm",
        f"php{replacement}-common",
    }

    result = developer_tools.runner.run(
        ["apt-cache", "depends", spec.package],
        timeout=60,
    )
    if not result.ok:
        # The later APT removal simulation is the final safety gate. A failed
        # dependency probe therefore falls back to the base FPM runtime rather
        # than guessing extension names.
        return required

    for raw in result.stdout.splitlines():
        match = _DEPENDENCY_RE.match(raw)
        if not match:
            continue

        for candidate in _versioned_dependency_candidates(
            match.group(1),
            replacement,
        ):
            if (
                developer_tools.apt.is_installed(candidate)
                or developer_tools.apt.candidate(candidate)
            ):
                required.add(candidate)

    return required


def _prepare_replacement_php(
    controller: NativeDevController,
    preview: PhpUninstallPreview,
) -> None:
    developer_tools = controller.developer_tools
    if developer_tools is None:
        raise RuntimeError("Developer Tool manager is not available")

    replacement = preview.replacement
    if not replacement:
        raise RuntimeError("No replacement PHP-FPM version is available")
    if replacement not in controller.php.installed_fpm_versions():
        raise RuntimeError(f"PHP {replacement} FPM is not installed")
    if not controller.php.fpm_config_ready(replacement):
        raise RuntimeError(
            f"PHP {replacement} FPM configuration is missing. Repair FPM first."
        )

    required: set[str] = set()
    for key in preview.tool_keys:
        spec = DEVELOPER_TOOL_BY_KEY[key]
        required.update(
            _tool_php_requirements(
                developer_tools,
                spec,
                replacement,
            )
        )

    missing = sorted(
        package
        for package in required
        if not developer_tools.apt.is_installed(package)
    )
    if missing:
        developer_tools.apt.install(missing)

    # Validate/create NativeDev's per-user FPM pool and start the replacement
    # FPM when necessary. This does not set /usr/bin/php as Default.
    controller.php.ensure_developer_pool(replacement)


def _simulate_tool_impact(
    controller: NativeDevController,
    version: str,
) -> tuple[DeveloperToolSpec, ...]:
    developer_tools = controller.developer_tools
    if developer_tools is None:
        return ()

    packages = controller.php.installed_version_packages(version)
    if not packages:
        raise RuntimeError(f"PHP {version} is not installed")

    result = developer_tools.runner.run(
        ["apt-get", "-s", "remove", *packages],
        timeout=90,
    )
    if not result.ok:
        raise RuntimeError(
            result.output or f"Could not calculate PHP {version} removal impact"
        )

    removed: set[str] = set()
    for raw in result.stdout.splitlines():
        match = re.match(r"^Remv\s+(\S+)", raw.strip())
        if match:
            removed.add(match.group(1).split(":", 1)[0])

    return tuple(
        spec
        for spec in DEVELOPER_WEB_TOOLS
        if developer_tools.apt.is_installed(spec.package)
        and spec.package in removed
    )


def _restore_tool_selections(
    controller: NativeDevController,
    previous: dict[str, str],
) -> None:
    developer_tools = controller.developer_tools
    if developer_tools is None:
        return

    for key, selected in previous.items():
        if selected:
            developer_tools.set_selected_php(key, selected)
        else:
            developer_tools.clear_selected_php(key)

    if shutil.which("nginx"):
        developer_tools.configure_nginx()


class _StandalonePhpUninstallContext:
    """Minimal adapter for the legacy module-level uninstall API."""

    def __init__(self, php, developer_tools):
        self.php = php
        self.developer_tools = developer_tools


def _standalone_remove_php_version(
    *,
    php,
    php_ini,
    reconcile_nginx,
    version: str,
) -> None:
    """Legacy removal tail used only by :func:`uninstall_php_version`.

    The controller workflow owns the newer version-package postcondition check.
    This compatibility path deliberately preserves the earlier public contract
    because external callers/test doubles may not expose ``php.apt`` or the
    controller's LocalDev state.
    """

    detached_ini = False
    if (
        php_ini is not None
        and hasattr(php_ini, "has_active_override")
        and php_ini.has_active_override(version)
    ):
        php_ini.detach_runtime(version)
        detached_ini = True

    try:
        php.uninstall_version(version)
    except Exception as exc:
        if detached_ini:
            try:
                php_ini.restore_profile(version)
            except Exception as rollback_exc:
                raise RuntimeError(
                    f"PHP {version} uninstall failed ({exc}); "
                    f"NativeDev INI rollback also failed ({rollback_exc})"
                ) from exc
        raise

    if reconcile_nginx is not None:
        reconcile_nginx()


def uninstall_php_version(
    *,
    php,
    developer_tools,
    php_ini,
    reconcile_nginx,
    version: str,
    confirmed_tool_removal: Iterable[str] = (),
) -> None:
    """Backward-compatible module-level PHP uninstall workflow.

    This API predates :class:`PhpAwareNativeDevController` and remains useful
    for tests and non-GUI callers. It follows the same dependency-aware policy:

    * migrate affected Developer Tools to a remaining FPM when possible;
    * never change the system Default PHP explicitly;
    * block when APT would remove an affected Developer Tool;
    * require explicit confirmation when the last FPM is being removed.
    """

    preview = build_php_uninstall_preview(php, developer_tools, version)

    if not preview.has_tool_users:
        _standalone_remove_php_version(
            php=php,
            php_ini=php_ini,
            reconcile_nginx=reconcile_nginx,
            version=version,
        )
        return

    if not preview.remaining_fpm:
        confirmed = {str(key) for key in confirmed_tool_removal}
        if not set(preview.tool_keys).issubset(confirmed):
            titles = ", ".join(preview.tool_titles)
            raise RuntimeError(
                f"PHP {version} is the only installed PHP-FPM runtime and "
                f"is currently used by: {titles}. Removing it may also "
                "remove those Developer Tools. Explicit confirmation is "
                "required."
            )

        _standalone_remove_php_version(
            php=php,
            php_ini=php_ini,
            reconcile_nginx=reconcile_nginx,
            version=version,
        )
        if developer_tools is not None:
            for key in preview.tool_keys:
                developer_tools.clear_selected_php(key)
        return

    context = _StandalonePhpUninstallContext(php, developer_tools)
    previous = {
        key: developer_tools.selected_php(key)
        for key in preview.tool_keys
    }

    try:
        _prepare_replacement_php(context, preview)

        for key in preview.tool_keys:
            developer_tools.set_selected_php(key, preview.replacement)

        if shutil.which("nginx"):
            developer_tools.configure_nginx()

        impacted = _simulate_tool_impact(context, preview.version)
        if impacted:
            titles = ", ".join(spec.title for spec in impacted)
            raise RuntimeError(
                f"PHP {preview.version} cannot be removed safely because "
                f"APT would also remove: {titles}."
            )

        _standalone_remove_php_version(
            php=php,
            php_ini=php_ini,
            reconcile_nginx=reconcile_nginx,
            version=version,
        )

    except Exception as exc:
        try:
            _restore_tool_selections(context, previous)
        except Exception as rollback_exc:
            raise RuntimeError(
                f"PHP {preview.version} uninstall failed ({exc}); "
                "Developer Tool PHP rollback also failed "
                f"({rollback_exc})"
            ) from exc
        raise


class PhpAwareNativeDevController(NativeDevController):
    """NativeDev controller with dependency-aware PHP uninstall orchestration."""

    def _reconcile_managed_nginx(self) -> None:
        """Reconcile Nginx without treating a no-PHP machine as an error.

        LocalDev's persistent wildcard router needs a PHP-FPM backend when it is
        regenerated. After the final FPM is removed there is deliberately no
        backend to render, so leave the existing NativeDev-owned wildcard file
        untouched. A later PHP install will call this method again and rebuild
        it once an FPM exists. Developer Tool routing can still reconcile to an
        empty config, which removes stale Adminer/phpMyAdmin server blocks.
        """
        if not shutil.which("nginx"):
            return

        if self.php.installed_fpm_versions():
            if self.localdev.nginx_managed():
                self.localdev.configure_nginx_sites()

        if self.developer_tools is not None and self.developer_tools.nginx_managed():
            self.developer_tools.configure_nginx()

    def _remove_php_version_complete(self, version: str) -> None:
        """Remove one PHP version and verify no version-scoped package remains.

        APT can occasionally finish a dependency transition while leaving or
        re-introducing a version-scoped package such as phpX.Y-cli. NativeDev
        therefore verifies the dpkg state after the first remove and performs
        one bounded cleanup pass in the same user action.
        """
        detached_ini = False
        if self.php_ini is not None and self.php_ini.has_active_override(version):
            self.php_ini.detach_runtime(version)
            detached_ini = True

        try:
            self.php.uninstall_version(version)

            remaining = self.php.installed_version_packages(version)
            if remaining:
                self.php.apt.remove(remaining)

            remaining = self.php.installed_version_packages(version)
            if remaining:
                raise RuntimeError(
                    f"APT completed the PHP {version} removal, but these "
                    "version-scoped packages are still installed: "
                    + ", ".join(remaining)
                )
        except Exception as exc:
            if detached_ini and version in self.php.installed_versions():
                try:
                    self.php_ini.restore_profile(version)
                except Exception as rollback_exc:
                    raise RuntimeError(
                        f"PHP {version} uninstall failed ({exc}); "
                        f"NativeDev INI rollback also failed ({rollback_exc})"
                    ) from exc
            raise

        try:
            self._reconcile_managed_nginx()
        except Exception as exc:
            raise RuntimeError(
                f"PHP {version} was uninstalled, but NativeDev Nginx "
                f"reconciliation failed: {exc}"
            ) from exc

    def preview_php_uninstall(self, version: str) -> PhpUninstallPreview:
        return build_php_uninstall_preview(self, version)

    def authorize_php_tool_removal(
        self,
        version: str,
        tool_keys: Iterable[str],
    ) -> None:
        """Record the GUI's explicit Continue decision for the last-PHP case."""

        confirmations = getattr(self, "_php_uninstall_confirmations", None)
        if confirmations is None:
            confirmations = {}
            self._php_uninstall_confirmations = confirmations

        confirmations[version] = (
            frozenset(str(key) for key in tool_keys),
            time.monotonic() + _CONFIRMATION_TTL_SECONDS,
        )

    def _consume_php_tool_removal_confirmation(
        self,
        preview: PhpUninstallPreview,
    ) -> bool:
        confirmations = getattr(self, "_php_uninstall_confirmations", {})
        value = confirmations.pop(preview.version, None)
        if value is None:
            return False

        confirmed_keys, deadline = value
        if time.monotonic() > deadline:
            return False

        return set(preview.tool_keys).issubset(confirmed_keys)

    def _uninstall_without_tool_guard(
        self,
        version: str,
    ) -> None:
        """Run the verified uninstall tail after explicit last-PHP confirmation."""
        self._remove_php_version_complete(version)

    def _migrate_tool_users_and_uninstall(
        self,
        preview: PhpUninstallPreview,
    ) -> None:
        developer_tools = self.developer_tools
        if developer_tools is None:
            raise RuntimeError("Developer Tool manager is not available")

        previous = {
            key: developer_tools.selected_php(key)
            for key in preview.tool_keys
        }

        try:
            _prepare_replacement_php(self, preview)

            for key in preview.tool_keys:
                developer_tools.set_selected_php(
                    key,
                    preview.replacement,
                )

            if shutil.which("nginx"):
                developer_tools.configure_nginx()

            impacted = _simulate_tool_impact(self, preview.version)
            if impacted:
                titles = ", ".join(spec.title for spec in impacted)
                raise RuntimeError(
                    f"PHP {preview.version} cannot be removed safely because "
                    f"APT would also remove: {titles}. PHP "
                    f"{preview.replacement} was prepared and the affected "
                    "Developer Tools were migrated, but package dependencies "
                    "are still tied to the PHP version being removed."
                )

            # No Developer Tool uses the target anymore. Remove the PHP
            # version with a package postcondition check so a leftover CLI does
            # not require a second Uninstall click.
            self._remove_php_version_complete(preview.version)

        except Exception as exc:
            # If the target PHP still exists, restore the original tool
            # selections. If package removal already succeeded and only a later
            # reconciliation failed, never point tools back at a removed PHP.
            target_still_installed = preview.version in self.php.installed_versions()
            if target_still_installed:
                try:
                    _restore_tool_selections(self, previous)
                except Exception as rollback_exc:
                    raise RuntimeError(
                        f"PHP {preview.version} uninstall failed ({exc}); "
                        "Developer Tool PHP rollback also failed "
                        f"({rollback_exc})"
                    ) from exc
            raise

    def uninstall_php(self, version: str) -> None:
        with self._mutation_lock:
            preview = self.preview_php_uninstall(version)

            if not preview.has_tool_users:
                self._remove_php_version_complete(version)
                return

            if preview.remaining_fpm:
                self._migrate_tool_users_and_uninstall(preview)
                return

            if not self._consume_php_tool_removal_confirmation(preview):
                titles = ", ".join(preview.tool_titles)
                raise RuntimeError(
                    f"PHP {version} is the only installed PHP-FPM runtime and "
                    f"is currently used by: {titles}. Removing it may also "
                    "remove those Developer Tools. Explicit confirmation is "
                    "required."
                )

            # The GUI has explicitly warned that the affected tools may be
            # removed. Do not pre-migrate because no replacement FPM exists.
            self._uninstall_without_tool_guard(version)

            # APT may have removed Adminer/phpMyAdmin together with PHP. In all
            # cases the removed PHP is no longer a valid explicit selection, so
            # clear it and let any future PHP installation use Default.
            if self.developer_tools is not None:
                for key in preview.tool_keys:
                    self.developer_tools.clear_selected_php(key)
