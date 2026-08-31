from __future__ import annotations

import os
import re
import shlex
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..system import CommandRunner


NVM_INSTALL_URL = "https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.6/install.sh"
BEGIN_MARKER = "# >>> NativeDev NVM >>>"
END_MARKER = "# <<< NativeDev NVM <<<"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True, slots=True)
class LtsRelease:
    version: str
    codename: str


class NodeManager:
    def __init__(self, runner: CommandRunner):
        self.runner = runner

    @property
    def nvm_dir(self) -> Path:
        if os.environ.get("NVM_DIR"):
            return Path(os.environ["NVM_DIR"]).expanduser()
        xdg = os.environ.get("XDG_CONFIG_HOME")
        return (Path(xdg) / "nvm") if xdg else (Path.home() / ".nvm")

    def installed(self) -> bool:
        return (self.nvm_dir / "nvm.sh").is_file()

    def install_nvm(self) -> None:
        if self.installed():
            return
        with tempfile.TemporaryDirectory(prefix="nativedev-nvm-") as temp_dir:
            installer = Path(temp_dir) / "install.sh"
            urllib.request.urlretrieve(NVM_INSTALL_URL, installer)
            result = self.runner.run(
                ["/bin/bash", str(installer)],
                env={"PROFILE": "/dev/null", "NVM_DIR": str(self.nvm_dir)},
                timeout=900,
            )
            if not result.ok:
                raise RuntimeError(result.output or "NVM installation failed")
        self.configure_shell()

    def shell_rc(self) -> Path:
        shell = Path(os.environ.get("SHELL", "/bin/bash")).name
        if shell == "zsh":
            return Path.home() / ".zshrc"
        if shell == "bash":
            return Path.home() / ".bashrc"
        return Path.home() / ".profile"

    def configure_shell(self) -> Path:
        rc = self.shell_rc()
        old = rc.read_text(encoding="utf-8") if rc.exists() else ""
        if BEGIN_MARKER in old and END_MARKER in old:
            return rc
        nvm_dir = shlex.quote(str(self.nvm_dir))
        block = (
            f"\n{BEGIN_MARKER}\n"
            f"export NVM_DIR={nvm_dir}\n"
            '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"\n'
            f"{END_MARKER}\n"
        )
        rc.write_text(old.rstrip() + block, encoding="utf-8")
        return rc

    def nvm_version(self) -> str:
        if not self.installed():
            return ""
        result = self.runner.bash_nvm(self.nvm_dir, ["--version"])
        return result.stdout.strip() if result.ok else ""

    def current_node(self) -> str:
        if not self.installed():
            return ""
        result = self.runner.bash_nvm(self.nvm_dir, ["current"])
        value = result.stdout.strip()
        return "" if value in {"none", "system", "N/A"} else value

    def default_node(self) -> str:
        if not self.installed():
            return ""
        # `nvm version default` resolves aliases to the installed concrete version.
        result = self.runner.bash_nvm(self.nvm_dir, ["version", "default"])
        value = result.stdout.strip()
        return "" if value in {"none", "system", "N/A"} else value

    def installed_versions(self) -> list[str]:
        versions_dir = self.nvm_dir / "versions" / "node"
        if not versions_dir.is_dir():
            return []
        values = [
            p.name
            for p in versions_dir.iterdir()
            if p.is_dir() and re.fullmatch(r"v\d+\.\d+\.\d+", p.name)
        ]
        return sorted(values, key=self._version_key, reverse=True)

    def available_lts(self) -> list[LtsRelease]:
        if not self.installed():
            return []
        result = self.runner.bash_nvm(self.nvm_dir, ["ls-remote", "--lts"], check=False)
        if not result.ok:
            raise RuntimeError(result.output or "Could not load Node.js LTS releases")
        return self.parse_lts_output(result.stdout)

    @classmethod
    def parse_lts_output(cls, output: str) -> list[LtsRelease]:
        # nvm emits every patch release in every LTS line. The GUI needs one row
        # per LTS generation, so retain the newest patch for each codename.
        latest: dict[str, str] = {}
        cleaned = ANSI_RE.sub("", output)
        for raw in cleaned.splitlines():
            match = re.search(r"(v\d+\.\d+\.\d+).*\(LTS:\s*([^\)]+)\)", raw)
            if not match:
                continue
            version, codename = match.group(1), match.group(2).strip()
            if codename.lower() in {"n/a", "false"}:
                continue
            previous = latest.get(codename)
            if previous is None or cls._version_key(version) > cls._version_key(previous):
                latest[codename] = version
        releases = [LtsRelease(version, codename) for codename, version in latest.items()]
        return sorted(releases, key=lambda item: cls._version_key(item.version), reverse=True)

    def install_version(self, version: str) -> None:
        self._validate_version(version)
        result = self.runner.bash_nvm(self.nvm_dir, ["install", version], check=False)
        if not result.ok:
            raise RuntimeError(result.output or f"Node {version} installation failed")

    def uninstall_version(self, version: str) -> None:
        self._validate_version(version)
        if version not in self.installed_versions():
            raise RuntimeError(f"Node {version} is not installed")
        if self.default_node() == version:
            self.runner.bash_nvm(self.nvm_dir, ["unalias", "default"], check=False)
        result = self.runner.bash_nvm(self.nvm_dir, ["uninstall", version], check=False)
        if not result.ok:
            raise RuntimeError(result.output or f"Node {version} uninstall failed")

    def set_default(self, version: str) -> None:
        self._validate_version(version)
        if version not in self.installed_versions():
            raise RuntimeError(f"Install Node {version} before setting it as default")
        result = self.runner.bash_nvm(self.nvm_dir, ["alias", "default", version], check=False)
        if not result.ok:
            raise RuntimeError(result.output or f"Could not set Node {version} as default")

    def install_lts(self) -> None:
        result = self.runner.bash_nvm(self.nvm_dir, ["install", "--lts"], check=False)
        if not result.ok:
            raise RuntimeError(result.output or "Node LTS installation failed")
        self.runner.bash_nvm(self.nvm_dir, ["alias", "default", "lts/*"], check=True)

    def install_current(self) -> None:
        result = self.runner.bash_nvm(self.nvm_dir, ["install", "node"], check=False)
        if not result.ok:
            raise RuntimeError(result.output or "Current Node installation failed")
        self.runner.bash_nvm(self.nvm_dir, ["alias", "default", "node"], check=True)

    @staticmethod
    def _validate_version(version: str) -> None:
        if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
            raise RuntimeError(f"Invalid Node.js version: {version}")

    @staticmethod
    def _version_key(value: str) -> tuple[int, int, int]:
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", value)
        return tuple(map(int, match.groups())) if match else (0, 0, 0)
