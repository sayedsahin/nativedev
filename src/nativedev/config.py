from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


APP_DIR = Path.home() / ".config" / "nativedev"
STATE_DIR = Path.home() / ".local" / "share" / "nativedev"
CONFIG_FILE = APP_DIR / "config.json"
LOCAL_DOMAIN_RE = re.compile(r"^[a-z0-9-]{1,30}$")


def normalize_local_domain(value: object) -> str:
    """Return NativeDev's canonical local TLD or reject unsafe input.

    The value is persisted in user-writable config.json and later embedded in
    privileged DNS/Nginx configuration, so this must be treated as a security
    boundary rather than only a GUI validation rule.
    """
    if not isinstance(value, str):
        raise ValueError("Local TLD must be a string")
    normalized = value.strip().lower().lstrip(".")
    if not LOCAL_DOMAIN_RE.fullmatch(normalized):
        raise ValueError("Local TLD must contain 1-30 letters, numbers or hyphens")
    return normalized


@dataclass(slots=True)
class AppConfig:
    park_dir: str = str(Path.home() / "www")
    domain: str = "test"
    # Kept only for backward compatibility with 0.1.4 config files that were
    # briefly written with a single global PHP-FPM version. Routing no longer
    # depends on this; each project resolves its own version (see
    # LocalDevManager.project_php_version).
    php_version: str = ""
    https_enabled: bool = False
    # Empty string means "auto-detect from distro" (see
    # DEFAULT_DNS_MODEL_BY_DISTRO in managers/localdev.py). Set explicitly to
    # "networkmanager-dnsmasq" or "dedicated-link" to override, e.g. for a
    # newly-supported distro before it's added to the built-in map.
    dns_model: str = ""
    projects: dict[str, dict[str, str]] = field(default_factory=dict)
    developer_tools: dict[str, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.domain = normalize_local_domain(self.domain)

    @classmethod
    def load(cls) -> "AppConfig":
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        allowed = {name for name in cls.__dataclass_fields__}
        values = {key: value for key, value in data.items() if key in allowed}
        if not isinstance(values.get("projects", {}), dict):
            values["projects"] = {}
        if not isinstance(values.get("developer_tools", {}), dict):
            values["developer_tools"] = {}
        try:
            values["domain"] = normalize_local_domain(values.get("domain", "test"))
        except ValueError:
            # A hand-edited/corrupted config must never become privileged DNS
            # or Nginx input. Keep the rest of the user's settings and fall
            # back only the unsafe TLD to NativeDev's safe default.
            values["domain"] = "test"
        return cls(**values)

    def save(self) -> None:
        # Re-validate here as well because callers can mutate dataclass fields
        # after construction. This prevents an invalid in-memory TLD from ever
        # being persisted for a later privileged operation.
        self.domain = normalize_local_domain(self.domain)
        APP_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), indent=2) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix="config-", suffix=".json.tmp", dir=APP_DIR)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, CONFIG_FILE)
        finally:
            temp_path.unlink(missing_ok=True)
