from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .. import __version__
from ..system import CommandRunner, DistroInfo


APPLICATION_PACKAGE = "nativedev"
UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
UPDATE_CACHE_DIR = Path.home() / ".cache" / "nativedev"
UPDATE_STATE_FILE = UPDATE_CACHE_DIR / "update-state.json"
GITHUB_OWNER = "sayedsahin"
GITHUB_REPOSITORY = "nativedev"
GITHUB_LATEST_RELEASE_API = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/latest"
)
GITHUB_RELEASE_DOWNLOAD_PREFIX = (
    f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/download/"
)
GITHUB_ACCEPT = "application/vnd.github+json"
RELEASE_VERSION_RE = re.compile(r"^v?(?P<version>\d+\.\d+\.\d+)$")
SHA256_DIGEST_RE = re.compile(r"^sha256:(?P<digest>[0-9a-fA-F]{64})$")


@dataclass(slots=True, frozen=True)
class ApplicationUpdate:
    installed_version: str
    candidate_version: str
    backend: str


@dataclass(slots=True, frozen=True)
class GitHubRelease:
    version: str
    tag_name: str
    asset_name: str
    asset_url: str
    sha256: str


class ApplicationPackageBackend(Protocol):
    name: str

    def installed_version(self) -> str | None: ...

    def release_asset_name(self, version: str) -> str: ...

    def is_newer(self, candidate: str, installed: str) -> bool: ...

    def update(self) -> None: ...


class ApplicationReleaseSource(Protocol):
    name: str

    def latest(self, asset_name: str) -> GitHubRelease | None: ...


class GitHubReleaseSource:
    """Read NativeDev's public stable release metadata from GitHub Releases.

    The latest-release endpoint excludes draft/prerelease releases. NativeDev
    still validates those flags defensively and accepts only the fixed release
    asset name supplied by the active Linux package backend.
    """

    name = "github"

    def __init__(self, *, opener=urllib.request.urlopen, timeout: int = 20):
        self.opener = opener
        self.timeout = timeout

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "Accept": GITHUB_ACCEPT,
            "User-Agent": f"NativeDev/{__version__}",
        }

    def _read_json(self) -> dict | None:
        request = urllib.request.Request(GITHUB_LATEST_RELEASE_API, headers=self._headers())
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # The project may legitimately have no published release yet.
                return None
            raise RuntimeError(f"GitHub release check failed: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"GitHub release check failed: {exc}") from exc

        if len(payload) > 2 * 1024 * 1024:
            raise RuntimeError("GitHub release metadata is unexpectedly large")
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("GitHub returned invalid NativeDev release metadata") from exc
        if not isinstance(data, dict):
            raise RuntimeError("GitHub returned invalid NativeDev release metadata")
        return data

    def latest(self, asset_name: str) -> GitHubRelease | None:
        data = self._read_json()
        if data is None:
            return None
        if data.get("draft") is True or data.get("prerelease") is True:
            return None

        tag_name = data.get("tag_name")
        if not isinstance(tag_name, str):
            raise RuntimeError("Latest NativeDev GitHub release has no valid tag")
        match = RELEASE_VERSION_RE.fullmatch(tag_name.strip())
        if not match:
            raise RuntimeError("Latest NativeDev GitHub release tag must be vX.Y.Z or X.Y.Z")
        version = match.group("version")

        expected_name = asset_name.format(version=version)
        assets = data.get("assets")
        if not isinstance(assets, list):
            raise RuntimeError("Latest NativeDev GitHub release has no asset list")

        selected: dict | None = None
        for raw in assets:
            if isinstance(raw, dict) and raw.get("name") == expected_name and raw.get("state") == "uploaded":
                selected = raw
                break
        if selected is None:
            raise RuntimeError(
                f"NativeDev {version} is published without the required release asset {expected_name}"
            )

        url = selected.get("browser_download_url")
        if not isinstance(url, str) or not url.startswith(GITHUB_RELEASE_DOWNLOAD_PREFIX):
            raise RuntimeError("NativeDev release asset URL is outside the official GitHub repository")
        if not url.endswith("/" + expected_name):
            raise RuntimeError("NativeDev release asset URL does not match the expected package name")

        digest = selected.get("digest")
        digest_match = SHA256_DIGEST_RE.fullmatch(digest) if isinstance(digest, str) else None
        if digest_match is None:
            raise RuntimeError("NativeDev release asset is missing GitHub's SHA-256 digest")

        return GitHubRelease(
            version=version,
            tag_name=tag_name,
            asset_name=expected_name,
            asset_url=url,
            sha256=digest_match.group("digest").lower(),
        )


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

    def release_asset_name(self, version: str) -> str:
        # NativeDev itself is pure Python/GTK and ships as Architecture: all.
        return "nativedev_{version}_all.deb"

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
            # The helper downloads/validates a fixed GitHub release asset and
            # installs it with APT. Do not impose an arbitrary package timeout.
            timeout=None,
        )


class ApplicationManager:
    """NativeDev application release/update state.

    Release discovery is distro-independent and comes from the fixed public
    GitHub repository. The package backend remains distro-specific so future
    RPM/DNF or Pacman implementations can select/install their own release
    artifact without changing the 24-hour GUI policy.
    """

    def __init__(
        self,
        runner: CommandRunner,
        distro: DistroInfo,
        *,
        state_file: Path = UPDATE_STATE_FILE,
        clock: Callable[[], float] = time.time,
        release_source: ApplicationReleaseSource | None = None,
    ):
        self.runner = runner
        self.distro = distro
        self.state_file = state_file
        self.clock = clock
        self.release_source = release_source or GitHubReleaseSource()
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
        """Check GitHub Releases without privilege or blocking the GTK thread."""
        backend = self.backend
        if backend is None:
            return None
        if not force and not self.check_due():
            return None

        now = self.clock()
        state: dict[str, object] = {
            "last_check": now,
            "backend": backend.name,
            "source": self.release_source.name,
        }
        try:
            installed = backend.installed_version()
            state["installed_version"] = installed or ""
            if not installed:
                return None

            release = self.release_source.latest(backend.release_asset_name("{version}"))
            candidate = release.version if release else None
            state["candidate_version"] = candidate or ""
            if release is not None:
                state["tag_name"] = release.tag_name
                state["asset_name"] = release.asset_name
                state["asset_sha256"] = release.sha256
            if candidate and backend.is_newer(candidate, installed):
                return ApplicationUpdate(installed, candidate, backend.name)
            return None
        finally:
            # Failed/offline checks are also throttled. Otherwise every app
            # launch would retry GitHub instead of respecting the 24-hour rule.
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
        # The running Python process remains the previous release until restart.
        # The helper independently revalidates GitHub's latest asset before it
        # installs anything, so no cached URL/path crosses the root boundary.
        self._save_state({
            "last_check": self.clock(),
            "backend": backend.name,
            "source": self.release_source.name,
            "installed_version": backend.installed_version() or "",
            "candidate_version": "",
        })
