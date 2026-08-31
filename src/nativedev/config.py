from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


APP_DIR = Path.home() / ".config" / "nativedev"
STATE_DIR = Path.home() / ".local" / "share" / "nativedev"
CONFIG_FILE = APP_DIR / "config.json"


@dataclass(slots=True)
class AppConfig:
    park_dir: str = str(Path.home() / "Code")
    domain: str = "test"
    php_version: str = ""
    https_enabled: bool = False

    @classmethod
    def load(cls) -> "AppConfig":
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        allowed = {field for field in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
