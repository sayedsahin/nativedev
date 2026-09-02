from __future__ import annotations

from dataclasses import dataclass

from .config import AppConfig
from .controller import NativeDevController
from .managers import Doctor, LocalDevManager, NodeManager, PhpManager
from .services import ServiceManager
from .system import AptManager, CommandRunner, DistroInfo, SystemdManager, read_os_release


@dataclass(slots=True)
class AppContext:
    distro: DistroInfo
    config: AppConfig
    runner: CommandRunner
    apt: AptManager
    systemd: SystemdManager
    php: PhpManager
    node: NodeManager
    services: ServiceManager
    localdev: LocalDevManager
    doctor: Doctor
    controller: NativeDevController

    @classmethod
    def create(cls) -> "AppContext":
        distro = read_os_release()
        config = AppConfig.load()
        runner = CommandRunner()
        apt = AptManager(runner)
        systemd = SystemdManager(runner)
        php = PhpManager(runner, apt, systemd, distro)
        node = NodeManager(runner, apt)
        services = ServiceManager(runner, apt, systemd)
        localdev = LocalDevManager(runner, apt, systemd, config, php)
        doctor = Doctor(distro, apt, systemd, php, node, services, localdev)
        controller = NativeDevController(php, localdev, node)
        return cls(distro, config, runner, apt, systemd, php, node, services, localdev, doctor, controller)
