from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


APP_DIR = Path.home() / ".config" / "nativedev"
STATE_DIR = Path.home() / ".local" / "share" / "nativedev"
CONFIG_FILE = APP_DIR / "config.json"


@dataclass(slots=True)
class AppConfig:
    park_dir: str = str(Path.home() / "Code")
    domain: str = "test"
    # Kept only for backward compatibility with 0.1.4 config files that were
    # briefly written with a single global PHP-FPM version. Routing no longer
    # depends on this; each project resolves its own version (see
    # LocalDevManager.project_php_version).
    php_version: str = ""
    https_enabled: bool = False
    projects: dict[str, dict[str, str]] = field(default_factory=dict)

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
        return cls(**values)

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
