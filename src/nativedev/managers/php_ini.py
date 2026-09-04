from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from ..config import APP_DIR
from ..system import CommandRunner, SystemdManager
from .php import PhpManager


DIRECTIVE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.]*$")
MAX_SETTINGS = 128
MAX_DIRECTIVE_LENGTH = 128
MAX_VALUE_LENGTH = 4096
BLOCKED_DIRECTIVES = frozenset({"extension", "zend_extension", "extension_dir"})

# These are suggestions only. NativeDev accepts other syntactically valid PHP
# directives too, except extension-loading directives which belong exclusively
# to the PHP Extensions page.
SUGGESTED_DIRECTIVES: tuple[str, ...] = (
    "memory_limit",
    "max_execution_time",
    "max_input_time",
    "max_input_vars",
    "upload_max_filesize",
    "post_max_size",
    "max_file_uploads",
    "display_errors",
    "display_startup_errors",
    "error_reporting",
    "log_errors",
    "date.timezone",
    "session.gc_maxlifetime",
    "realpath_cache_size",
    "realpath_cache_ttl",
    "default_socket_timeout",
    "variables_order",
    "request_order",
    "opcache.enable",
    "opcache.memory_consumption",
    "opcache.interned_strings_buffer",
    "opcache.max_accelerated_files",
    "opcache.revalidate_freq",
    "opcache.validate_timestamps",
)


class PhpIniManager:
    """Manage one NativeDev-owned INI override layer per PHP version.

    NativeDev never edits System/Multi-PHP php.ini files. The root helper owns the
    fixed /etc/php/<version>/mods-available/nativedev.ini file and the matching
    99-nativedev.ini links for CLI and FPM. PHP extension loading remains the
    exclusive responsibility of PhpExtensionManager.
    """

    def __init__(
        self,
        runner: CommandRunner,
        systemd: SystemdManager,
        php: PhpManager,
        *,
        config_root: Path = Path("/etc/php"),
        profile_root: Path = APP_DIR / "php",
    ):
        self.runner = runner
        self.systemd = systemd
        self.php = php
        self.config_root = config_root
        self.profile_root = profile_root

    def installed_versions(self) -> list[str]:
        return self.php.installed_versions()

    @staticmethod
    def suggested_directives() -> tuple[str, ...]:
        return SUGGESTED_DIRECTIVES

    def override_file(self, version: str) -> Path:
        return self.config_root / version / "mods-available" / "nativedev.ini"

    def profile_file(self, version: str) -> Path:
        return self.profile_root / f"{version}.json"

    @staticmethod
    def validate_setting(directive: str, value: str) -> None:
        if not isinstance(directive, str) or not directive:
            raise RuntimeError("PHP INI directive name is required")
        if len(directive) > MAX_DIRECTIVE_LENGTH or not DIRECTIVE_RE.fullmatch(directive):
            raise RuntimeError(
                "PHP INI directive names may contain only letters, numbers, underscores and dots, and must start with a letter"
            )
        if directive.casefold() in BLOCKED_DIRECTIVES:
            raise RuntimeError(
                f"{directive} is managed by PHP Extensions; extension loading cannot be changed from PHP Settings"
            )
        if not isinstance(value, str):
            raise RuntimeError("PHP INI value must be text")
        # This is intentionally a hard reject, never a strip/sanitize operation.
        # A newline would create a second INI directive after the helper renders
        # ``name = value`` and would therefore cross the semantic RPC boundary.
        if "\n" in value or "\r" in value or "\0" in value:
            raise RuntimeError("PHP INI value must be a single line (newline, carriage return and NUL are not allowed)")
        if len(value) > MAX_VALUE_LENGTH:
            raise RuntimeError(f"PHP INI value is too long (maximum {MAX_VALUE_LENGTH} characters)")

    @classmethod
    def validate_settings(cls, settings: dict[str, str]) -> dict[str, str]:
        if not isinstance(settings, dict):
            raise RuntimeError("PHP INI settings must be a directive/value mapping")
        if len(settings) > MAX_SETTINGS:
            raise RuntimeError(f"Too many PHP INI overrides (maximum {MAX_SETTINGS})")
        normalized: dict[str, str] = {}
        for directive, value in settings.items():
            cls.validate_setting(directive, value)
            normalized[directive] = value
        return normalized

    def has_active_override(self, version: str) -> bool:
        path = self.override_file(version)
        cli = self.config_root / version / "cli" / "conf.d" / "99-nativedev.ini"
        fpm = self.config_root / version / "fpm" / "conf.d" / "99-nativedev.ini"
        return path.is_file() or cli.is_symlink() or fpm.is_symlink()

    def detach_runtime(self, version: str) -> None:
        """Remove only the active /etc layer while retaining the saved profile.

        The controller uses this immediately before uninstalling a PHP version so
        NativeDev-owned root configuration does not keep /etc/php/<version> alive.
        """
        self._require_runtime(version)
        current = self.settings(version)
        if current and not self.saved_profile(version):
            self._write_profile(version, current)
        self.runner.privileged_operation(
            "php.ini.reset",
            version=version,
            check=True,
            timeout=180,
        )

    def settings(self, version: str) -> dict[str, str]:
        """Read only NativeDev's own override file, never distro php.ini."""
        path = self.override_file(version)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise RuntimeError(f"Could not read NativeDev PHP {version} settings: {exc}") from exc

        settings: dict[str, str] = {}
        for number, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith(";") or line.startswith("#"):
                continue
            directive, separator, value = raw.partition("=")
            if not separator:
                raise RuntimeError(f"Unexpected syntax in {path} at line {number}")
            directive = directive.strip()
            value = value.strip()
            self.validate_setting(directive, value)
            settings[directive] = value
        return settings

    def effective_settings(self, version: str, directives: list[str] | tuple[str, ...]) -> dict[str, str]:
        """Resolve selected directives with one PHP process.

        The NativeDev override is shared by CLI/FPM. We use CLI only as a cheap
        read probe; FPM configuration itself is validated by the root helper on
        every mutation.
        """
        self._require_runtime(version)
        keys = list(dict.fromkeys(directives))
        if not keys:
            return {}
        for directive in keys:
            self.validate_setting(directive, "")

        binary = Path(f"/usr/bin/php{version}")
        payload = json.dumps(keys, separators=(",", ":"))
        script = (
            '$keys=json_decode(getenv("NATIVEDEV_INI_KEYS"), true);'
            '$out=[]; foreach ($keys as $key) { $value=ini_get($key); '
            '$out[$key]=($value === false ? null : $value); } echo json_encode($out);'
        )
        result = self.runner.run(
            [str(binary), "-r", script],
            env={"NATIVEDEV_INI_KEYS": payload},
            timeout=30,
        )
        if not result.ok:
            raise RuntimeError(result.output or f"Could not read effective PHP {version} settings")
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"PHP {version} returned invalid INI state") from exc
        if not isinstance(data, dict):
            return {}
        return {key: "" if data.get(key) is None else str(data.get(key)) for key in keys}

    def apply(self, version: str, settings: dict[str, str]) -> None:
        self._require_runtime(version)
        normalized = self.validate_settings(settings)
        if not normalized:
            self.reset(version)
            return
        self.runner.privileged_operation(
            "php.ini.apply",
            version=version,
            settings=normalized,
            check=True,
            timeout=180,
        )
        self._write_profile(version, normalized)

    def reset(self, version: str) -> None:
        self._require_runtime(version)
        self.runner.privileged_operation(
            "php.ini.reset",
            version=version,
            check=True,
            timeout=180,
        )
        self.profile_file(version).unlink(missing_ok=True)

    def saved_profile(self, version: str) -> dict[str, str]:
        path = self.profile_file(version)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if isinstance(data, dict) and isinstance(data.get("settings"), dict):
            data = data["settings"]
        if not isinstance(data, dict):
            return {}
        try:
            return self.validate_settings({str(key): value for key, value in data.items()})
        except RuntimeError:
            return {}

    def restore_profile(self, version: str) -> None:
        profile = self.saved_profile(version)
        if not profile:
            raise RuntimeError(f"No saved NativeDev PHP {version} settings were found")
        self.apply(version, profile)

    def _write_profile(self, version: str, settings: dict[str, str]) -> None:
        self.profile_root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": version, "settings": settings}, indent=2, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f"php-{version}-", suffix=".json.tmp", dir=self.profile_root)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.profile_file(version))
        finally:
            temp_path.unlink(missing_ok=True)

    def _require_runtime(self, version: str) -> None:
        if version not in self.php.installed_versions():
            raise RuntimeError(f"PHP {version} is not installed")
        # The application architecture always installs CLI and FPM together;
        # Settings deliberately follows that invariant rather than introducing a
        # split-SAPI configuration mode.
        if not self.php.fpm_config_ready(version):
            raise RuntimeError(f"PHP {version} FPM configuration is not ready")
