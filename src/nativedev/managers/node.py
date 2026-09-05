from __future__ import annotations

import os
import re
import shlex
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..system import AptManager, CommandRunner


NVM_INSTALL_URL = "https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.6/install.sh"
BEGIN_MARKER = "# >>> NativeDev NVM >>>"
END_MARKER = "# <<< NativeDev NVM <<<"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True, slots=True)
class LtsRelease:
    version: str
    codename: str


class NodeManager:
    """Manage one active Node provider: system APT packages or per-user NVM.

    NativeDev can discover/manage an existing System Node installation without
    touching it automatically. Multi-version Node is an explicit provider
    migration to NVM; after a successful migration the system node/npm packages
    are no longer retained as a second selectable runtime.
    """

    def __init__(self, runner: CommandRunner, apt: AptManager | None = None):
        self.runner = runner
        self.apt = apt or AptManager(runner)

    @property
    def nvm_dir(self) -> Path:
        if os.environ.get("NVM_DIR"):
            return Path(os.environ["NVM_DIR"]).expanduser()
        xdg = os.environ.get("XDG_CONFIG_HOME")
        return (Path(xdg) / "nvm") if xdg else (Path.home() / ".nvm")

    def installed(self) -> bool:
        """Return whether the NVM framework itself is present."""
        return (self.nvm_dir / "nvm.sh").is_file()

    def system_node_installed(self) -> bool:
        return self.apt.is_installed("nodejs")

    def system_npm_installed(self) -> bool:
        return self.apt.is_installed("npm")

    def system_node_version(self) -> str:
        binary = Path("/usr/bin/node")
        if not binary.is_file():
            return ""
        result = self.runner.run([str(binary), "--version"], timeout=10)
        value = result.stdout.strip()
        return value if result.ok and re.fullmatch(r"v\d+\.\d+\.\d+", value) else ""

    def provider(self) -> str:
        """Return the NativeDev Node mode: ``nvm``, ``system`` or ``none``.

        Presence of the NVM framework wins deliberately. NativeDev never offers
        System Node as a second provider once NVM is active; a leftover system Node is
        treated only as an incomplete NVM migration that can be cleaned up.
        """
        if self.installed():
            return "nvm"
        if self.system_node_installed():
            return "system"
        return "none"

    def install_nvm(self) -> None:
        if self.installed():
            self.configure_shell()
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

    def shell_configured(self) -> bool:
        rc = self.shell_rc()
        if not rc.exists():
            return False
        try:
            text = rc.read_text(encoding="utf-8")
        except OSError:
            return False
        return BEGIN_MARKER in text and END_MARKER in text

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

    def remove_shell_integration(self) -> Path:
        """Remove only NativeDev's marked NVM block; never delete user NVM data."""
        rc = self.shell_rc()
        if not rc.exists():
            return rc
        old = rc.read_text(encoding="utf-8")
        pattern = re.compile(
            rf"\n?{re.escape(BEGIN_MARKER)}.*?{re.escape(END_MARKER)}\n?",
            re.DOTALL,
        )
        new = pattern.sub("\n", old).rstrip() + ("\n" if old else "")
        if new != old:
            rc.write_text(new, encoding="utf-8")
        return rc

    def nvm_version(self) -> str:
        if not self.installed():
            return ""
        result = self.runner.bash_nvm(self.nvm_dir, ["--version"])
        return result.stdout.strip() if result.ok else ""

    def current_node(self) -> str:
        provider = self.provider()
        if provider == "system":
            return self.system_node_version()
        if not self.installed():
            return ""
        result = self.runner.bash_nvm(self.nvm_dir, ["current"])
        value = result.stdout.strip()
        if value == "system":
            return self.system_node_version()
        return "" if value in {"none", "N/A"} else value

    def default_node(self) -> str:
        if not self.installed() or not self.installed_versions():
            return ""
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
        latest: dict[str, str] = {}
        cleaned = ANSI_RE.sub("", output)
        for raw in cleaned.splitlines():
            match = re.search(
                r"(v\d+\.\d+.\d+).*?\((?:Latest\s+)?LTS:\s*([^\)]+)\)",
                raw,
            )
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
        self._require_nvm_provider()
        self._validate_version(version)
        result = self.runner.bash_nvm(self.nvm_dir, ["install", version], check=False)
        if not result.ok:
            raise RuntimeError(result.output or f"Node {version} installation failed")

    def uninstall_version(self, version: str) -> None:
        self._require_nvm_provider()
        self._validate_version(version)
        if version not in self.installed_versions():
            raise RuntimeError(f"Node {version} is not installed")
        if self.default_node() == version:
            self.runner.bash_nvm(self.nvm_dir, ["unalias", "default"], check=False)
        result = self.runner.bash_nvm(self.nvm_dir, ["uninstall", version], check=False)
        if not result.ok:
            raise RuntimeError(result.output or f"Node {version} uninstall failed")

    def set_default(self, version: str) -> None:
        self._require_nvm_provider()
        self._validate_version(version)
        if version not in self.installed_versions():
            raise RuntimeError(f"Install Node {version} before setting it as default")
        result = self.runner.bash_nvm(self.nvm_dir, ["alias", "default", version], check=False)
        if not result.ok:
            raise RuntimeError(result.output or f"Could not set Node {version} as default")

    def install_lts(self) -> None:
        if not self.installed():
            raise RuntimeError("NVM is not installed")
        result = self.runner.bash_nvm(self.nvm_dir, ["install", "--lts"], check=False)
        if not result.ok:
            raise RuntimeError(result.output or "Node LTS installation failed")
        self.runner.bash_nvm(self.nvm_dir, ["alias", "default", "lts/*"], check=True)

    def install_current(self) -> None:
        self._require_nvm_provider()
        result = self.runner.bash_nvm(self.nvm_dir, ["install", "node"], check=False)
        if not result.ok:
            raise RuntimeError(result.output or "Current Node installation failed")
        self.runner.bash_nvm(self.nvm_dir, ["alias", "default", "node"], check=True)

    def system_removal_impact(self) -> list[str]:
        """Return manually-installed packages that would be removed with Node.

        The system ``npm`` package pulls in a large graph of ``node-*`` packages
        (and tools such as eslint/webpack) as automatic dependencies. Removing
        the System Node provider may legitimately remove that dependency graph;
        treating every simulated ``Remv`` row as unrelated makes a normal
        System -> NVM migration impossible.

        NativeDev therefore blocks only when APT would also remove a package
        that is marked *manual* by apt-mark. If apt-mark cannot be queried, fall
        back to the conservative behaviour and treat every extra removal as a
        blocker.
        """
        requested = [pkg for pkg in ("nodejs", "npm") if self.apt.is_installed(pkg)]
        if not requested:
            return []

        result = self.runner.run(["apt-get", "-s", "remove", *requested], timeout=60)
        if not result.ok:
            raise RuntimeError(result.output or "Could not calculate Node.js removal impact")

        removed: set[str] = set()
        for raw in result.stdout.splitlines():
            match = re.match(r"^Remv\s+(\S+)", raw.strip())
            if match:
                removed.add(match.group(1).split(":", 1)[0])

        extras = removed.difference(requested)
        if not extras:
            return []

        manual_result = self.runner.run(["apt-mark", "showmanual"], timeout=30)
        if not manual_result.ok:
            # Safety-first fallback: if package ownership cannot be classified,
            # do not silently remove anything beyond nodejs/npm.
            return sorted(extras)

        manual = {
            line.strip().split(":", 1)[0]
            for line in manual_result.stdout.splitlines()
            if line.strip()
        }
        return sorted(extras.intersection(manual))

    def install_system_node(self) -> str:
        packages = ["nodejs"]
        if self.apt.candidate("npm"):
            packages.append("npm")
        self.apt.install(packages)
        version = self.system_node_version()
        if not version:
            raise RuntimeError("System Node.js was installed but /usr/bin/node is not usable")
        return version

    def uninstall_system_node(self) -> None:
        extras = self.system_removal_impact()
        if extras:
            raise RuntimeError(
                "NativeDev will not remove System Node because APT would also remove manually installed package(s): "
                + ", ".join(extras)
            )
        packages = [pkg for pkg in ("nodejs", "npm") if self.apt.is_installed(pkg)]
        if not packages:
            raise RuntimeError("System Node.js is not installed")
        self.apt.remove(packages)

    def enable_nvm_multi_node(self) -> str:
        """Migrate System Node to exclusive NVM management and install LTS."""
        extras = self.system_removal_impact()
        if extras:
            raise RuntimeError(
                "NativeDev will not remove System Node because APT would also remove manually installed package(s): "
                + ", ".join(extras)
            )

        # NativeDev's provider migration is explicit: retire System Node first,
        # then activate NVM. If NVM bootstrap or LTS installation fails, restore
        # the system packages as rollback so the user is not left without Node.
        had_shell = self.shell_configured()
        system_packages = [pkg for pkg in ("nodejs", "npm") if self.apt.is_installed(pkg)]

        try:
            if system_packages:
                self.apt.remove(system_packages)
            self.install_nvm()
            versions = self.installed_versions()
            if not versions:
                self.install_lts()
            elif not self.default_node():
                self.set_default(versions[0])
            self.configure_shell()
        except Exception as exc:
            rollback_errors: list[str] = []
            if system_packages and not self.system_node_installed():
                try:
                    self.install_system_node()
                except Exception as rollback_exc:
                    rollback_errors.append(f"System Node restore failed: {rollback_exc}")
            if not had_shell:
                try:
                    self.remove_shell_integration()
                except Exception as rollback_exc:
                    rollback_errors.append(f"shell rollback failed: {rollback_exc}")
            if rollback_errors:
                raise RuntimeError(
                    f"NVM migration failed ({exc}); " + "; ".join(rollback_errors)
                ) from exc
            restored = "System Node was restored" if system_packages else "provider state was rolled back"
            raise RuntimeError(f"NVM migration failed; {restored}: {exc}") from exc

        return self.default_node() or (self.installed_versions()[0] if self.installed_versions() else "")

    def _require_nvm_provider(self) -> None:
        provider = self.provider()
        if provider == "system":
            raise RuntimeError("Switch Node.js provider to NVM before managing multiple Node versions")
        if not self.installed():
            raise RuntimeError("NVM is not installed")

    @staticmethod
    def _validate_version(version: str) -> None:
        if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
            raise RuntimeError(f"Invalid Node.js version: {version}")

    @staticmethod
    def _version_key(value: str) -> tuple[int, int, int]:
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", value)
        return tuple(map(int, match.groups())) if match else (0, 0, 0)
