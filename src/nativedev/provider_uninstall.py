from __future__ import annotations

import re
import shutil
from pathlib import Path

from .php_uninstall import PhpAwareNativeDevController
from .managers.node import BEGIN_MARKER, END_MARKER, NodeManager


class ProviderNodeManager(NodeManager):
    """NodeManager with a guarded NVM provider uninstall operation."""

    @staticmethod
    def _remove_nativedev_marker_block(text: str) -> str:
        pattern = re.compile(
            rf"\n?{re.escape(BEGIN_MARKER)}.*?{re.escape(END_MARKER)}\n?",
            re.DOTALL,
        )
        cleaned = pattern.sub("\n", text)
        # Preserve a normal text-file ending without rewriting unrelated lines.
        if cleaned and not cleaned.endswith("\n"):
            cleaned += "\n"
        return cleaned

    def _native_shell_rc_files(self) -> tuple[Path, ...]:
        # NativeDev normally writes only the active shell RC. Include both Bash
        # and Zsh explicitly so a user who changed shells later does not retain
        # a stale NativeDev-owned block. If an unsupported shell caused
        # configure_shell() to use ~/.profile, include that exact current path
        # too. Only marker-owned blocks are ever removed.
        candidates = [
            Path.home() / ".bashrc",
            Path.home() / ".zshrc",
            self.shell_rc(),
        ]
        unique: list[Path] = []
        seen: set[Path] = set()
        for path in candidates:
            expanded = path.expanduser()
            if expanded in seen:
                continue
            seen.add(expanded)
            unique.append(expanded)
        return tuple(unique)

    def uninstall_nvm(self) -> None:
        if not self.installed():
            raise RuntimeError("NVM is not installed")

        versions = self.installed_versions()
        if versions:
            readable = ", ".join(version.removeprefix("v") for version in versions)
            raise RuntimeError(
                "Uninstall all NVM-managed Node.js versions before removing NVM. "
                f"Installed versions: {readable}"
            )

        home = Path.home().resolve()
        nvm_dir = self.nvm_dir.expanduser()

        if nvm_dir.is_symlink():
            raise RuntimeError(
                f"Refusing to remove symlinked NVM directory: {nvm_dir}"
            )

        try:
            resolved_nvm = nvm_dir.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"NVM directory is unavailable: {nvm_dir}") from exc

        # NativeDev's NVM installer is intentionally per-user. Refuse a custom
        # NVM_DIR outside the user's home instead of recursively deleting an
        # arbitrary filesystem location.
        if resolved_nvm == home or home not in resolved_nvm.parents:
            raise RuntimeError(
                f"Refusing to remove NVM outside the current user's home: {resolved_nvm}"
            )

        if not (resolved_nvm / "nvm.sh").is_file():
            raise RuntimeError(
                f"Refusing to remove an unverified NVM directory: {resolved_nvm}"
            )

        originals: dict[Path, str] = {}
        changed: list[Path] = []

        try:
            for rc in self._native_shell_rc_files():
                if not rc.is_file() or rc.is_symlink():
                    continue
                old = rc.read_text(encoding="utf-8")
                new = self._remove_nativedev_marker_block(old)
                if new == old:
                    continue
                originals[rc] = old
                rc.write_text(new, encoding="utf-8")
                changed.append(rc)

            shutil.rmtree(resolved_nvm)
        except Exception as exc:
            rollback_errors: list[str] = []
            for rc in reversed(changed):
                try:
                    rc.write_text(originals[rc], encoding="utf-8")
                except Exception as rollback_exc:
                    rollback_errors.append(f"{rc}: {rollback_exc}")
            if rollback_errors:
                raise RuntimeError(
                    f"NVM uninstall failed ({exc}); shell integration rollback "
                    f"also failed: {'; '.join(rollback_errors)}"
                ) from exc
            raise RuntimeError(
                f"NVM uninstall failed; NativeDev shell integration was restored: {exc}"
            ) from exc

        if self.installed():
            raise RuntimeError(
                f"NVM removal completed incompletely; {self.nvm_dir / 'nvm.sh'} still exists"
            )


class ProviderAwareNativeDevController(PhpAwareNativeDevController):
    """Add provider-removal invariants on top of PHP-aware uninstall workflow."""

    def uninstall_multi_php_repository(self) -> None:
        with self._mutation_lock:
            versions = self.php.installed_versions()
            if versions:
                raise RuntimeError(
                    "Uninstall all PHP versions before removing the "
                    f"{self.php.multi_php_repository_name} multi-PHP repository."
                )
            if not self.php.multi_php_configured():
                raise RuntimeError("Multi-PHP repository is not configured")
            self.php.remove_multi_php_repository()
            if self.php.multi_php_configured():
                raise RuntimeError("Multi-PHP repository is still configured after removal")

    def uninstall_nvm(self) -> None:
        with self._mutation_lock:
            if self.node is None:
                raise RuntimeError("Node manager is not available")
            versions = self.node.installed_versions()
            if versions:
                readable = ", ".join(
                    version.removeprefix("v") for version in versions
                )
                raise RuntimeError(
                    "Uninstall all NVM-managed Node.js versions before removing NVM. "
                    f"Installed versions: {readable}"
                )
            uninstall = getattr(self.node, "uninstall_nvm", None)
            if uninstall is None:
                raise RuntimeError("NVM uninstall operation is not available")
            uninstall()
