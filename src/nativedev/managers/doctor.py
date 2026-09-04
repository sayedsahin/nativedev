from __future__ import annotations

import shutil
from dataclasses import dataclass

from ..services import COMPONENTS, ServiceManager
from ..system import AptManager, DistroInfo, SystemdManager
from .localdev import LocalDevManager
from .node import NodeManager
from .php import PhpManager


@dataclass(slots=True)
class Check:
    ok: bool
    name: str
    detail: str = ""


class Doctor:
    def __init__(
        self,
        distro: DistroInfo,
        apt: AptManager,
        systemd: SystemdManager,
        php: PhpManager,
        node: NodeManager,
        services: ServiceManager,
        localdev: LocalDevManager,
    ):
        self.distro = distro
        self.apt = apt
        self.systemd = systemd
        self.php = php
        self.node = node
        self.services = services
        self.localdev = localdev

    def run(self) -> list[Check]:
        checks: list[Check] = [
            Check(self.distro.is_debian_family, "Debian/Ubuntu-family distribution", self.distro.pretty_name),
            Check(self.apt.available, "APT/dpkg available"),
            Check(self.systemd.available, "systemd available"),
            Check(bool(shutil.which("pkexec")), "Polkit / pkexec available"),
            Check(self.php.multi_php_configured(), "Multi-PHP repository", "optional" if not self.php.multi_php_configured() else self.php.multi_php_repository_name),
            Check(bool(self.php.installed_versions()), "PHP installed", ", ".join(self.php.installed_versions())),
            Check(self.node.provider() != "none", "Node.js provider", self.node.provider()),
            Check(self.localdev.dns_ready(), f"*.{self.localdev.config.domain} DNS", self.localdev.dns_strategy()),
            Check(self.localdev.nginx_ready(), "NativeDev Nginx sites configured", f"{len(self.localdev.projects())} projects"),
        ]
        versions_in_use: set[str] = set()
        for project in self.localdev.projects():
            try:
                versions_in_use.add(self.localdev.project_php_version(project))
            except RuntimeError:
                continue
        for version in sorted(versions_in_use):
            checks.append(
                Check(
                    self.php.developer_pool_configured(version),
                    f"PHP {version} NativeDev developer pool",
                    f"*.test PHP runs as {self.php.developer_user}",
                )
            )
        for spec in COMPONENTS:
            state = self.services.state(spec)
            detail = "running" if state.running else ("installed" if state.installed else "not installed")
            checks.append(Check(state.installed, spec.title, detail))
        return checks

    @staticmethod
    def format(checks: list[Check]) -> str:
        lines = []
        for item in checks:
            symbol = "✓" if item.ok else "○"
            detail = f" — {item.detail}" if item.detail else ""
            lines.append(f"{symbol} {item.name}{detail}")
        return "\n".join(lines)
