from __future__ import annotations

from dataclasses import dataclass

from .config import AppConfig
from .controller import NativeDevController
from .managers import Doctor, LocalDevManager, NodeManager, PhpExtensionManager, PhpIniManager, PhpManager
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
    php_extensions: PhpExtensionManager
    php_ini: PhpIniManager
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
        php_extensions = PhpExtensionManager(runner, apt, systemd, php)
        php_ini = PhpIniManager(runner, systemd, php)
        node = NodeManager(runner, apt)
        services = ServiceManager(runner, apt, systemd)
        localdev = LocalDevManager(runner, apt, systemd, config, php)
        doctor = Doctor(distro, apt, systemd, php, node, services, localdev)
        controller = NativeDevController(php, localdev, node, php_ini)
        return cls(distro, config, runner, apt, systemd, php, php_extensions, php_ini, node, services, localdev, doctor, controller)
