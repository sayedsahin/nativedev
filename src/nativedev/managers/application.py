from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..system import CommandRunner, DistroInfo


APPLICATION_PACKAGE = "nativedev"
UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
UPDATE_CACHE_DIR = Path.home() / ".cache" / "nativedev"
UPDATE_STATE_FILE = UPDATE_CACHE_DIR / "update-state.json"


@dataclass(slots=True, frozen=True)
class ApplicationUpdate:
    installed_version: str
    candidate_version: str
    backend: str


class ApplicationPackageBackend(Protocol):
    name: str

    def installed_version(self) -> str | None: ...

    def candidate_version(self) -> str | None: ...

    def is_newer(self, candidate: str, installed: str) -> bool: ...

    def update(self) -> None: ...


class AptApplicationBackend:
    """NativeDev application package backend for Debian/Ubuntu-family systems."""

    name = "apt"

    def __init__(self, runner: CommandRunner):
        self.runner = runner

    def installed_version(self) -> str | None:
        result = self.runner.run(
            ["dpkg-query", "-W", "-f=${db:Status-Abbrev}\t${Version}", APPLICATION_PACKAGE],
            timeout=15,
        )
        if not result.ok:
            return None
        status, separator, version = result.stdout.partition("\t")
        if not separator or not status.startswith("ii "):
            return None
        return version.strip() or None

    def candidate_version(self) -> str | None:
        result = self.runner.run(["apt-cache", "policy", APPLICATION_PACKAGE], timeout=30)
        if not result.ok:
            return None
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if line.startswith("Candidate:"):
                value = line.partition(":")[2].strip()
                return None if value in {"", "(none)"} else value
        return None

    def is_newer(self, candidate: str, installed: str) -> bool:
        if not candidate or not installed or candidate == installed:
            return False
        result = self.runner.run(
            ["dpkg", "--compare-versions", candidate, "gt", installed], timeout=15
        )
        return result.ok

    def update(self) -> None:
        self.runner.privileged_operation(
            "application.update",
            check=True,
            # Package-manager work must not be killed by an arbitrary short
            # application timeout. Root-side APT lock handling still fails
            # immediately if another dpkg frontend already owns the lock.
            timeout=None,
        )


class ApplicationManager:
    """Application install/update state, independent from feature managers.

    NativeDev currently ships an APT backend, while the GUI and 24-hour update
    policy depend only on this interface. Future RPM/DNF or Arch/Pacman support
    can add another backend without changing the application-level UX.
    """

    def __init__(
        self,
        runner: CommandRunner,
        distro: DistroInfo,
        *,
        state_file: Path = UPDATE_STATE_FILE,
        clock: Callable[[], float] = time.time,
    ):
        self.runner = runner
        self.distro = distro
        self.state_file = state_file
        self.clock = clock
        self.backend: ApplicationPackageBackend | None = self._select_backend()

    def _select_backend(self) -> ApplicationPackageBackend | None:
        if self.distro.is_debian_family:
            return AptApplicationBackend(self.runner)
        return None

    @property
    def update_supported(self) -> bool:
        return self.backend is not None

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix="update-state-", suffix=".json.tmp", dir=self.state_file.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.state_file)
        finally:
            temp_path.unlink(missing_ok=True)

    def check_due(self) -> bool:
        if self.backend is None:
            return False
        state = self._load_state()
        try:
            last_check = float(state.get("last_check", 0))
        except (TypeError, ValueError):
            last_check = 0
        return self.clock() - last_check >= UPDATE_CHECK_INTERVAL_SECONDS

    def check_for_update(self, *, force: bool = False) -> ApplicationUpdate | None:
        """Check native package metadata without asking for privilege.

        APT repositories are normally refreshed by the distro's own periodic
        package metadata jobs. NativeDev intentionally does not invoke a root
        `apt update` in the background because that would cause an unexpected
        Polkit prompt. The explicit Update action refreshes APT immediately
        before upgrading NativeDev.
        """
        backend = self.backend
        if backend is None:
            return None
        if not force and not self.check_due():
            return None

        now = self.clock()
        state: dict[str, object] = {"last_check": now, "backend": backend.name}
        try:
            installed = backend.installed_version()
            candidate = backend.candidate_version() if installed else None
            state["installed_version"] = installed or ""
            state["candidate_version"] = candidate or ""
            if installed and candidate and backend.is_newer(candidate, installed):
                return ApplicationUpdate(installed, candidate, backend.name)
            return None
        finally:
            # Record failed/no-op attempts too, otherwise an offline workstation
            # would retry on every launch instead of respecting the 24-hour rule.
            self._save_state(state)

    def update(self) -> None:
        backend = self.backend
        if backend is None:
            raise RuntimeError(
                "NativeDev application updates are not implemented for this Linux package backend yet"
            )
        if backend.installed_version() is None:
            raise RuntimeError(
                "NativeDev is not installed as a native system package. Install the packaged release before using self-update."
            )
        backend.update()
        # The running Python process still contains the previous release until
        # restart. Avoid another background update prompt in that same period.
        self._save_state({
            "last_check": self.clock(),
            "backend": backend.name,
            "installed_version": backend.installed_version() or "",
            "candidate_version": backend.candidate_version() or "",
        })
