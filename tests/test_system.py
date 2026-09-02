import tempfile
import unittest
from pathlib import Path

from nativedev.system import read_os_release
from nativedev.managers.php import PhpManager
from nativedev.managers.node import NodeManager


class DistroTests(unittest.TestCase):
    def parse(self, content: str):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "os-release"
            path.write_text(content, encoding="utf-8")
            return read_os_release(path)

    def test_debian(self):
        info = self.parse('ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\nPRETTY_NAME="Debian 13"\n')
        self.assertTrue(info.is_debian_family)
        self.assertEqual(info.codename, "trixie")

    def test_ubuntu_derivative_uses_base_codename(self):
        info = self.parse('ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_CODENAME=wilma\nUBUNTU_CODENAME=noble\n')
        self.assertTrue(info.is_debian_family)
        self.assertEqual(info.codename, "noble")


class VersionTests(unittest.TestCase):
    def test_php_sort_key(self):
        self.assertGreater(PhpManager._version_key("8.4"), PhpManager._version_key("8.3"))

    def test_node_sort_key(self):
        self.assertGreater(NodeManager._version_key("v22.1.0"), NodeManager._version_key("v20.9.0"))

class StubPhp:
    developer_user = "developer"

    def __init__(self, default="8.4", installed=None):
        self.default = default
        self.installed = installed or ["8.4", "8.3"]

    def default_fpm_version(self):
        return self.default

    def installed_fpm_versions(self):
        return list(self.installed)

    @staticmethod
    def developer_socket_path(version):
        return Path(f"/run/php/php{version}-fpm-nativedev-1000.sock")


class NginxRenderTests(unittest.TestCase):
    def test_renders_one_persistent_wildcard_router(self):
        from nativedev.config import AppConfig
        from nativedev.managers.localdev import LocalDevManager, NGINX_WILDCARD_MARKER

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "shop" / "public").mkdir(parents=True)
            manager = LocalDevManager(None, None, None, AppConfig(park_dir=str(root), domain="test"), StubPhp())
            rendered = manager.render_nginx()
            self.assertIn(NGINX_WILDCARD_MARKER, rendered)
            self.assertIn("map $host $nativedev_project_dir", rendered)
            self.assertIn("$nativedev_auto_project", rendered)
            self.assertIn('if (-d "$nativedev_project_dir/public")', rendered)
            self.assertIn("root $nativedev_document_root;", rendered)
            self.assertIn("fastcgi_pass $nativedev_php_backend;", rendered)
            self.assertIn("php8.4-fpm-nativedev-1000.sock", rendered)
            self.assertNotIn(f'shop.test "{root / "shop"}";', rendered)
            self.assertNotIn("server_name shop.test;", rendered)

    def test_existing_project_gets_exact_path_but_future_projects_use_regex_fallback(self):
        from nativedev.config import AppConfig
        from nativedev.managers.localdev import LocalDevManager

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Shop").mkdir()
            manager = LocalDevManager(None, None, None, AppConfig(park_dir=str(root), domain="test"), StubPhp())
            rendered = manager.render_nginx()
            self.assertIn(f'shop.test "{root / "Shop"}";', rendered)
            self.assertIn(r"(?<nativedev_auto_project>[a-z0-9][a-z0-9-]*)\.test$", rendered)
            self.assertIn(f'"{root}/$nativedev_auto_project"', rendered)

    def test_project_can_pin_an_installed_php_fpm_version(self):
        from nativedev.config import AppConfig
        from nativedev.managers.localdev import LocalDevManager

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "legacy"
            project.mkdir()
            config = AppConfig(
                park_dir=str(root),
                domain="test",
                projects={str(project.resolve()): {"php": "8.3"}},
            )
            manager = LocalDevManager(None, None, None, config, StubPhp())
            rendered = manager.render_nginx()
            self.assertIn('legacy.test "unix:/run/php/php8.3-fpm-nativedev-1000.sock";', rendered)
            self.assertIn('default "unix:/run/php/php8.4-fpm-nativedev-1000.sock";', rendered)

    def test_park_path_with_spaces_is_quoted_for_dynamic_projects(self):
        from nativedev.config import AppConfig
        from nativedev.managers.localdev import LocalDevManager

        with tempfile.TemporaryDirectory(prefix="Native Dev ") as td:
            root = Path(td)
            manager = LocalDevManager(None, None, None, AppConfig(park_dir=str(root)), StubPhp())
            rendered = manager.render_nginx()
            self.assertIn(f'"{root}/$nativedev_auto_project"', rendered)

    def test_new_project_defaults_to_default_php(self):
        from nativedev.config import AppConfig
        from nativedev.managers.localdev import LocalDevManager

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "app"
            project.mkdir()
            config = AppConfig(park_dir=str(root), domain="test")
            manager = LocalDevManager(None, None, None, config, StubPhp())
            self.assertEqual(manager.project_preferences(project)["php"], "default")

    def test_no_php_runtime_does_not_emit_broken_server(self):
        from nativedev.config import AppConfig
        from nativedev.managers.localdev import LocalDevManager, NGINX_WILDCARD_MARKER

        with tempfile.TemporaryDirectory() as td:
            manager = LocalDevManager(None, None, None, AppConfig(park_dir=td, domain="test"), StubPhp(default="", installed=[]))
            rendered = manager.render_nginx()
            self.assertIn(NGINX_WILDCARD_MARKER, rendered)
            self.assertNotIn("fastcgi_pass", rendered)
            self.assertNotIn("server {", rendered)

    def test_wildcard_setup_adds_inheritable_park_acl(self):
        localdev = (
            Path(__file__).resolve().parents[1] / "src" / "nativedev" / "managers" / "localdev.py"
        ).read_text()
        self.assertIn('self._setfacl(["-m", f"d:u:{WEB_USER}:r-x", "--", str(root)])', localdev)
        self.assertIn("self.ensure_park_readable()", localdev)


class DnsRegressionTests(unittest.TestCase):
    def test_dns_config_does_not_restart_networkmanager(self):
        localdev = (
            Path(__file__).resolve().parents[1] / "src" / "nativedev" / "managers" / "localdev.py"
        ).read_text()
        self.assertIn('["nmcli", "general", "reload", "conf"]', localdev)
        self.assertIn('["nmcli", "general", "reload", "dns-full"]', localdev)
        self.assertNotIn('self.systemd.restart("NetworkManager")', localdev)


class HttpsKeyPermissionTests(unittest.TestCase):
    def test_https_key_is_root_only(self):
        # The privileged Nginx master process loads certificate keys before
        # workers handle traffic, so the NativeDev leaf key need not be
        # readable by www-data or other local users.
        localdev = (
            Path(__file__).resolve().parents[1] / "src" / "nativedev" / "managers" / "localdev.py"
        ).read_text()
        key_install_line = next(
            line for line in localdev.splitlines() if "nativedev-key.pem" in line and "install" in line
        )
        self.assertIn('"-m", "0600"', key_install_line)
        self.assertNotIn('"-m", "0644"', key_install_line)


class PhpDeveloperPoolTests(unittest.TestCase):
    def test_pool_runs_workers_as_current_developer(self):
        from nativedev.system import DistroInfo

        distro = DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13")
        manager = PhpManager(None, None, None, distro)
        rendered = manager.render_developer_pool("8.4")
        self.assertIn(f"[{manager.developer_pool_name()}]", rendered)
        self.assertIn(f"user = {manager.developer_user}", rendered)
        self.assertIn(f"group = {manager.developer_group}", rendered)
        self.assertIn(f"listen = {manager.developer_socket_path('8.4')}", rendered)
        self.assertIn("listen.owner = www-data", rendered)
        self.assertIn("listen.group = www-data", rendered)



class GtkSourceRegressionTests(unittest.TestCase):
    def test_gtk4_dialog_uses_gobject_property(self):
        from pathlib import Path
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        self.assertIn('gi.require_version("Gdk", "4.0")', gui)
        self.assertIn("secondary_text=message", gui)
        self.assertNotIn("dialog.format_secondary_text(", gui)

class NodeLtsTests(unittest.TestCase):
    def test_parse_lts_keeps_latest_patch_per_codename(self):
        sample = '''
       v18.20.7   (LTS: Hydrogen)
       v18.20.8   (LTS: Hydrogen)
       v20.19.4   (LTS: Iron)
       v20.19.5   (LTS: Iron)
       v22.20.0   (LTS: Jod)
        v23.0.0
'''
        releases = NodeManager.parse_lts_output(sample)
        self.assertEqual(
            [(r.version, r.codename) for r in releases],
            [("v22.20.0", "Jod"), ("v20.19.5", "Iron"), ("v18.20.8", "Hydrogen")],
        )


class PrivilegedHelperTests(unittest.TestCase):
    def operation_ok(self, request, uid=1000):
        from unittest.mock import patch
        from nativedev.privileged_helper import validate_operation
        with patch("nativedev.privileged_helper._binary", side_effect=lambda name: f"/usr/bin/{name}"):
            return validate_operation(request, uid=uid)[0]

    def test_allows_structured_native_operations(self):
        protocol = 6
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "systemd.service", "verb": "restart", "now": False, "service": "nginx"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "systemd.service", "verb": "disable", "now": True, "service": "php8.4-fpm"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "apt.install", "packages": ["redis-tools"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "apt.reinstall_confmiss", "packages": ["php8.4-fpm"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.install_packages", "packages": ["php8.4-cli", "php8.4-fpm", "php8.4-gd", "php8.4-opcache"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.install_packages", "packages": ["php-cli", "php-fpm", "php-gd"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.install_packages", "packages": ["php-cli", "php-fpm"], "allow_downgrades": True}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "apt.remove", "packages": ["nodejs", "npm"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.enable_modules", "version": "8.4", "sapi": "cli", "modules": ["gd", "mysqli", "pdo_mysql", "opcache"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "networkmanager.reload", "scope": "conf"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "file.install", "mode": "0644", "source": "/tmp/nativedev-fpm-test/pool.conf", "destination": "/etc/php/8.4/fpm/pool.d/nativedev-1000.conf"}, uid=1000))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "file.remove", "paths": ["/etc/php/8.4/fpm/pool.d/nativedev-1000.conf"]}, uid=1000))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "file.remove", "paths": ["/etc/php/8.4/fpm/pool.d/nativedev-1001.conf"]}, uid=1000))

    def test_rejects_raw_commands_and_outside_packages(self):
        protocol = 6
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "run", "argv": ["bash", "-c", "id"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "apt.install", "packages": ["openssh-server"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "apt.install", "packages": ["/tmp/nativedev-test/debsuryorg-archive-keyring.deb"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "sury.configure", "codename": "trixie"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "sury.configure", "codename": "evil-suite"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "systemd.service", "verb": "restart", "now": False, "service": "ssh"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "apt.reinstall_confmiss", "packages": ["nginx"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.install_packages", "packages": ["nginx"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.install_packages", "packages": ["php-arbitrary-root-tool"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.enable_modules", "version": "8.4", "sapi": "apache2", "modules": ["gd"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.enable_modules", "version": "8.4", "sapi": "cli", "modules": ["xdebug"]}))

    def test_client_translates_to_semantic_rpc_without_argv(self):
        from nativedev.system import privileged_operation_for_command
        request = privileged_operation_for_command(["systemctl", "enable", "--now", "nginx"])
        self.assertEqual(request["action"], "systemd.service")
        self.assertEqual(request["service"], "nginx")
        self.assertTrue(request["now"])
        self.assertNotIn("argv", request)

    def test_php_install_restores_missing_ucf_files_without_overwriting_existing_config(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import execute_operation

        request = {
            "protocol": 6,
            "action": "php.install_packages",
            "packages": ["php8.4-cli", "php8.4-gd", "php8.4-opcache"],
        }
        with patch("nativedev.privileged_helper._binary", side_effect=lambda name: f"/usr/bin/{name}"), \
             patch("nativedev.privileged_helper.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            execute_operation(request, uid=1000, timeout=1200)

        args, kwargs = run.call_args
        self.assertEqual(args[0][:4], ["/usr/bin/apt-get", "install", "--reinstall", "-y"])
        self.assertEqual(kwargs["env"]["UCF_FORCE_CONFFMISS"], "1")
        self.assertNotIn("UCF_FORCE_CONFFNEW", kwargs["env"])




class ServiceCleanupTests(unittest.TestCase):
    def _manager(self, dpkg_output: str):
        from nativedev.services import ServiceManager
        from nativedev.system import CommandResult

        class Runner:
            def run(self, argv, **kwargs):
                if argv[:2] == ["dpkg-query", "-W"]:
                    return CommandResult(list(argv), 0, dpkg_output, "")
                return CommandResult(list(argv), 0, "", "")

        class Apt:
            def __init__(self):
                self.removed = []
            def candidate(self, package):
                return "1"
            def is_installed(self, package):
                return any(
                    line.startswith("ii ") and line.split("\t", 1)[-1].strip().split(":", 1)[0] == package
                    for line in dpkg_output.splitlines()
                )
            def remove(self, packages):
                self.removed.append(list(packages))
            def install(self, packages):
                pass

        class Systemd:
            def enabled_state(self, service):
                return "not-found"
            def is_active(self, service):
                return False
            def disable_now(self, service):
                pass
            def enable_now(self, service):
                pass

        apt = Apt()
        return ServiceManager(Runner(), apt, Systemd()), apt

    def test_redis_server_and_cli_are_one_component(self):
        from nativedev.services import COMPONENTS
        redis = next(spec for spec in COMPONENTS if spec.key == "redis")
        self.assertEqual(redis.packages, ("redis-server", "redis-tools"))
        self.assertFalse(any(spec.key == "redis-cli" for spec in COMPONENTS))

    def test_postgresql_residual_versioned_runtime_remains_uninstallable(self):
        from nativedev.services import COMPONENTS

        output = (
            "rc \tpostgresql\n"
            "ii \tpostgresql-17\n"
            "un \tpostgresql-client\n"
            "ii \tpostgresql-client-17\n"
            "ii \tpostgresql-client-common\n"
            "ii \tpostgresql-common\n"
            "ii \tpostgresql-common-dev\n"
        )
        manager, apt = self._manager(output)
        spec = next(item for item in COMPONENTS if item.key == "postgresql")
        state = manager.state(spec)
        self.assertTrue(state.installed)
        self.assertTrue(state.uninstallable)
        self.assertEqual(
            manager.installed_component_packages(spec),
            ["postgresql-17", "postgresql-client-17"],
        )
        manager.uninstall(spec)
        self.assertEqual(apt.removed[-1], ["postgresql-17", "postgresql-client-17"])
        self.assertNotIn("postgresql-common", apt.removed[-1])
        self.assertNotIn("postgresql-common-dev", apt.removed[-1])

    def test_mariadb_cleanup_includes_server_and_client_core_but_not_common(self):
        from nativedev.services import COMPONENTS

        output = (
            "ii \tmariadb-server\n"
            "ii \tmariadb-client\n"
            "ii \tmariadb-server-core\n"
            "ii \tmariadb-client-core\n"
            "ii \tmariadb-common\n"
            "ii \tlibmariadb3\n"
        )
        manager, apt = self._manager(output)
        spec = next(item for item in COMPONENTS if item.key == "mariadb")
        manager.uninstall(spec)
        removed = apt.removed[-1]
        self.assertIn("mariadb-server", removed)
        self.assertIn("mariadb-client", removed)
        self.assertIn("mariadb-server-core", removed)
        self.assertIn("mariadb-client-core", removed)
        self.assertNotIn("mariadb-common", removed)
        self.assertNotIn("libmariadb3", removed)

    def test_redis_uninstall_removes_server_and_cli_package_together(self):
        from nativedev.services import COMPONENTS

        output = "ii \tredis-server\nii \tredis-tools\n"
        manager, apt = self._manager(output)
        spec = next(item for item in COMPONENTS if item.key == "redis")
        manager.uninstall(spec)
        self.assertEqual(apt.removed[-1], ["redis-server", "redis-tools"])

    def test_helper_allows_runtime_cleanup_only_for_remove(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import validate_operation

        with patch("nativedev.privileged_helper._binary", side_effect=lambda name: f"/usr/bin/{name}"):
            for package in ("postgresql-17", "postgresql-client-17", "mariadb-server-core", "mariadb-client-core"):
                self.assertTrue(validate_operation({"protocol": 6, "action": "apt.remove", "packages": [package]})[0])
                self.assertFalse(validate_operation({"protocol": 6, "action": "apt.install", "packages": [package]})[0])


class ControllerTests(unittest.TestCase):
    def test_default_php_change_reconciles_managed_nginx(self):
        from nativedev.controller import NativeDevController

        class Php:
            def __init__(self):
                self.current = "8.4"
                self.calls = []
            def cli_version(self):
                return self.current
            def set_cli_default(self, version):
                self.calls.append(version)
                self.current = version

        class LocalDev:
            def __init__(self):
                self.reconciles = 0
            def nginx_managed(self):
                return True
            def configure_nginx_sites(self):
                self.reconciles += 1

        php = Php()
        localdev = LocalDev()
        controller = NativeDevController(php, localdev)
        from unittest.mock import patch
        with patch("nativedev.controller.shutil.which", return_value="/usr/sbin/nginx"):
            controller.set_default_php("8.3")
        self.assertEqual(php.current, "8.3")
        self.assertEqual(localdev.reconciles, 1)

    def test_mutations_are_globally_serialized(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        from nativedev.controller import NativeDevController

        controller = NativeDevController(object(), object())
        gate = threading.Lock()
        active = 0
        max_active = 0

        def mutation():
            nonlocal active, max_active
            with gate:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with gate:
                active -= 1

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(controller.run_mutation, mutation) for _ in range(3)]
            for future in futures:
                future.result()
        self.assertEqual(max_active, 1)


class PhpDevelopmentPackageTests(unittest.TestCase):
    def _manager(self, runner=None, apt=None, systemd=None):
        from nativedev.system import DistroInfo

        class Runner:
            def run(self, argv, **kwargs):
                from nativedev.system import CommandResult
                return CommandResult(list(argv), 0, "", "")

        class Apt:
            def candidate(self, package): return "1"

        class Systemd:
            def disable_now(self, service): self.disabled_now = service

        distro = DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13")
        return PhpManager(runner or Runner(), apt or Apt(), systemd or Systemd(), distro)

    def test_framework_extensions_include_databases_and_opcache_before_php_85(self):
        manager = self._manager()
        packages = manager.development_extension_packages("8.4")
        for suffix in (
            "bcmath", "curl", "gd", "intl", "mbstring", "mysql", "pgsql",
            "readline", "sqlite3", "xml", "zip", "opcache",
        ):
            self.assertIn(f"php8.4-{suffix}", packages)

    def test_php_85_does_not_request_separate_opcache_package(self):
        manager = self._manager()
        packages = manager.development_extension_packages("8.5")
        self.assertNotIn("php8.5-opcache", packages)
        self.assertIn("php8.5-mbstring", packages)
        self.assertIn("php8.5-pgsql", packages)

    def test_install_time_module_baseline_maps_database_packages_to_all_modules(self):
        manager = self._manager()
        modules = manager.development_modules("8.4")
        for module in (
            "gd", "mysqlnd", "mysqli", "pdo_mysql", "pgsql", "pdo_pgsql",
            "sqlite3", "pdo_sqlite", "opcache",
        ):
            self.assertIn(module, modules)

    def test_php_85_module_baseline_does_not_use_opcache_ini(self):
        manager = self._manager()
        self.assertNotIn("opcache", manager.development_modules("8.5"))

    def test_enable_development_modules_targets_only_cli_and_fpm(self):
        class Runner:
            def __init__(self): self.operations = []
            def run(self, argv, **kwargs):
                from nativedev.system import CommandResult
                return CommandResult(list(argv), 0, "", "")
            def privileged_operation(self, action, **fields):
                self.operations.append((action, fields))
                from nativedev.system import CommandResult
                return CommandResult([f"nativedev:{action}"], 0, "", "")

        runner = Runner()
        manager = self._manager(runner=runner)
        manager.enable_development_modules("8.4")
        self.assertEqual([fields["sapi"] for _, fields in runner.operations], ["cli", "fpm"])
        self.assertTrue(all(action == "php.enable_modules" for action, _ in runner.operations))
        self.assertTrue(all("opcache" in fields["modules"] for _, fields in runner.operations))

    def test_php_install_uses_ucf_aware_package_path_before_enabling_modules(self):
        calls = []

        class Runner:
            def run(self, argv, **kwargs):
                from nativedev.system import CommandResult
                return CommandResult(list(argv), 0, "", "")
            def privileged_operation(self, action, **fields):
                calls.append(("rpc", action, fields))
                from nativedev.system import CommandResult
                return CommandResult([f"nativedev:{action}"], 0, "", "")

        class Apt:
            def candidate(self, package): return "1"
            def install_php(self, packages): calls.append(("install_php", list(packages)))
            def is_installed(self, package): return True

        class Systemd:
            def enable_now(self, service): calls.append(("enable_now", service))
            def is_active(self, service): return True
            def restart(self, service): calls.append(("restart", service))

        manager = self._manager(runner=Runner(), apt=Apt(), systemd=Systemd())
        manager.available_versions = lambda: ["8.4"]
        manager.fpm_config_ready = lambda version: True
        manager.ensure_developer_pool = lambda version: calls.append(("pool", version))
        manager.install_version("8.4")

        install_index = next(i for i, call in enumerate(calls) if call[0] == "install_php")
        module_index = next(i for i, call in enumerate(calls) if call[:2] == ("rpc", "php.enable_modules"))
        self.assertLess(install_index, module_index)
        installed = calls[install_index][1]
        self.assertIn("php8.4-gd", installed)
        self.assertIn("php8.4-opcache", installed)

    def test_uninstall_package_discovery_includes_all_version_scoped_extensions(self):
        from nativedev.system import CommandResult

        output = (
            "ii \tphp8.4-cli\n"
            "ii \tphp8.4-fpm\n"
            "ii \tphp8.4-mbstring\n"
            "ii \tphp8.4-redis\n"
            "ii \tphp8.4\n"
            "ii \tphp8.3-cli\n"
            "rc \tphp8.4-xdebug\n"
            "ii \tlibapache2-mod-php8.4\n"
        )

        class Runner:
            def run(self, argv, **kwargs):
                return CommandResult(list(argv), 0, output, "")

        manager = self._manager(runner=Runner())
        self.assertEqual(
            manager.installed_version_packages("8.4"),
            ["php8.4", "php8.4-cli", "php8.4-fpm", "php8.4-mbstring", "php8.4-redis"],
        )

    def test_disable_fpm_disables_and_stops_immediately(self):
        class Systemd:
            def __init__(self): self.calls = []
            def disable_now(self, service): self.calls.append(service)

        systemd = Systemd()
        manager = self._manager(systemd=systemd)
        manager.disable_fpm("8.4")
        self.assertEqual(systemd.calls, ["php8.4-fpm"])


class PhpRepairTests(unittest.TestCase):
    def test_repair_restores_only_missing_conffiles_without_purge(self):
        from nativedev.system import CommandResult, DistroInfo

        with tempfile.TemporaryDirectory() as td:
            master = Path(td) / "php-fpm.conf"

            class Runner:
                def __init__(self):
                    self.commands = []
                def run(self, argv, **kwargs):
                    self.commands.append(list(argv))
                    if argv[:2] == ["apt-get", "install"]:
                        master.write_text("[global]\n", encoding="utf-8")
                    return CommandResult(list(argv), 0, "", "")

            class Apt:
                def candidate(self, package): return "1"
                def is_installed(self, package): return True
                def install(self, packages): raise AssertionError("installed repair must use reinstall/force-confmiss")

            class Systemd:
                def enabled_state(self, service): return "disabled"
                def is_active(self, service): return False
                def disable(self, service): pass
                def enable(self, service): pass
                def stop(self, service): pass
                def start(self, service): pass

            runner = Runner()
            distro = DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13")
            manager = PhpManager(runner, Apt(), Systemd(), distro)
            manager.fpm_master_config_file = lambda version: master
            manager.repair_fpm("8.4")

            repair = runner.commands[0]
            self.assertIn("--reinstall", repair)
            self.assertIn("Dpkg::Options::=--force-confmiss", repair)
            self.assertNotIn("purge", repair)


class GuiFeatureRegressionTests(unittest.TestCase):
    def test_requested_controls_are_present(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        self.assertIn('Gtk.Button(label="Default")', gui)
        self.assertIn('Gtk.Button(label="Uninstall")', gui)
        self.assertIn('Gtk.Button(label="Start")', gui)
        self.assertIn('Gtk.Button(label="Stop")', gui)
        self.assertIn('Gtk.Button(label="Enable")', gui)
        self.assertIn('Gtk.Button(label="Disable")', gui)
        self.assertNotIn('Gtk.Button(label="Use CLI")', gui)
        # Per-project PHP-FPM routing must stay on a dedicated Projects page.
        # A prior release quietly regressed this back to a single free-text
        # global field; these two assertions guard against that regression
        # slipping through unnoticed again.
        self.assertIn('("projects", "Projects", ProjectsPage)', gui)
        self.assertIn('Gtk.DropDown.new_from_strings(php_labels)', gui)
        self.assertNotIn('grid.attach(label("PHP-FPM version")', gui)
        # Per-project file permission (Safe/Full write) ACLs were removed on
        # purpose once PHP-FPM started running as the developer user; do not
        # let that complexity come back silently either.
        self.assertNotIn('PERMISSION_OPTIONS', gui)
        # Page refreshes must remain observational; configuring Nginx is the
        # explicit action that grants www-data read/traverse ACLs.
        projects_refresh = gui[gui.index("class ProjectsPage"):gui.index("class LocalDevPage")]
        self.assertNotIn("ensure_project_readable(project)", projects_refresh)

    def test_php_and_node_lists_render_installed_versions_first(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        php_page = gui[gui.index("class PhpPage"):gui.index("class NodePage")]
        self.assertIn("versions = installed_sorted + available_sorted", php_page)
        self.assertIn('Gtk.Button(label="Disable & Stop")', php_page)

        node_page = gui[gui.index("class NodePage"):gui.index("class ServicesPage")]
        installed_pos = node_page.index('for version in data["versions"]:')
        available_pos = node_page.index('label("Available LTS releases", "section-title")')
        self.assertLess(installed_pos, available_pos)


class ProviderMigrationTests(unittest.TestCase):
    def test_php_provider_uses_sury_mode_whenever_sury_source_is_active(self):
        from nativedev.system import DistroInfo

        class Apt:
            def is_installed(self, package): return package == "php8.4-cli"

        manager = PhpManager(None, Apt(), None, DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13"))
        manager.sury_configured = lambda: True
        manager.installed_versions = lambda: ["8.4"]
        self.assertEqual(manager.provider(), "sury")

    def test_php_provider_uses_debian_only_without_sury(self):
        from nativedev.system import DistroInfo

        manager = PhpManager(None, object(), None, DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13"))
        manager.sury_configured = lambda: False
        manager.installed_versions = lambda: ["8.4"]
        self.assertEqual(manager.provider(), "debian")

    def test_php_debian_to_sury_migrates_in_place_after_repository_preflight(self):
        from nativedev.system import DistroInfo

        events = []

        manager = PhpManager(None, object(), None, DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13"))
        manager.default_fpm_version = lambda: "8.4"
        manager.cli_version = lambda: "8.4"
        manager.sury_configured = lambda: False
        manager.configure_sury = lambda explicit=False: events.append(("configure_sury", explicit))
        manager.available_versions = lambda: ["8.5", "8.4"]
        manager.installed_versions = lambda: ["8.4"]
        manager.install_version = lambda version: events.append(("install_sury", version))
        manager.set_cli_default = lambda version: events.append(("default", version))

        self.assertEqual(manager.enable_sury_multi_php(), "8.4")
        self.assertEqual(events[0], ("configure_sury", True))
        self.assertIn(("install_sury", "8.4"), events)
        self.assertIn(("default", "8.4"), events)

    def test_sury_migration_needed_reports_old_package_origin_without_changing_provider(self):
        from nativedev.system import DistroInfo

        class Apt:
            def is_installed(self, package): return package == "php8.4-cli"

        manager = PhpManager(None, Apt(), None, DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13"))
        manager.sury_configured = lambda: True
        manager.installed_versions = lambda: ["8.4"]
        manager.installed_package_provider = lambda package: "debian"
        self.assertEqual(manager.provider(), "sury")
        self.assertTrue(manager.sury_migration_needed())

    def test_node_provider_nvm_wins_when_system_node_is_also_present(self):
        class Apt:
            def is_installed(self, package): return package == "nodejs"

        manager = NodeManager(None, Apt())
        manager.installed = lambda: True
        self.assertEqual(manager.provider(), "nvm")

    def test_node_migration_blocks_manually_installed_apt_removals(self):
        from nativedev.system import CommandResult

        class Apt:
            def is_installed(self, package): return package in {"nodejs", "npm"}

        class Runner:
            def run(self, argv, **kwargs):
                if argv[:2] == ["apt-mark", "showmanual"]:
                    return CommandResult(list(argv), 0, "nodejs\nnpm\nmy-app\n", "")
                return CommandResult(list(argv), 0, "Remv nodejs [20]\nRemv npm [9]\nRemv node-acorn [8]\nRemv my-app [1]\n", "")

        manager = NodeManager(Runner(), Apt())
        manager.install_nvm = lambda: self.fail("NVM bootstrap must not run when APT removal is unsafe")
        with self.assertRaisesRegex(RuntimeError, "my-app"):
            manager.enable_nvm_multi_node()

    def test_node_migration_ignores_automatic_debian_node_dependency_removals(self):
        from nativedev.system import CommandResult

        class Apt:
            def is_installed(self, package): return package in {"nodejs", "npm"}

        class Runner:
            def run(self, argv, **kwargs):
                if argv[:2] == ["apt-mark", "showmanual"]:
                    return CommandResult(list(argv), 0, "nodejs\nnpm\n", "")
                return CommandResult(
                    list(argv), 0,
                    "Remv nodejs [20]\nRemv npm [9]\nRemv eslint [6]\nRemv webpack [5]\nRemv node-acorn [8]\n",
                    "",
                )

        manager = NodeManager(Runner(), Apt())
        self.assertEqual(manager.system_removal_impact(), [])

    def test_node_debian_to_nvm_removes_system_before_installing_nvm(self):
        from nativedev.system import CommandResult

        events = []

        class Apt:
            def is_installed(self, package): return package in {"nodejs", "npm"}
            def remove(self, packages): events.append(("remove_system", tuple(packages)))

        class Runner:
            def run(self, argv, **kwargs):
                if argv[:2] == ["apt-mark", "showmanual"]:
                    return CommandResult(list(argv), 0, "nodejs\nnpm\n", "")
                return CommandResult(list(argv), 0, "Remv nodejs [20]\nRemv npm [9]\nRemv node-acorn [8]\n", "")

        manager = NodeManager(Runner(), Apt())
        manager.shell_configured = lambda: False
        manager.install_nvm = lambda: events.append(("install_nvm",))
        manager.installed_versions = lambda: []
        manager.install_lts = lambda: events.append(("install_lts",))
        manager.default_node = lambda: "v22.0.0"
        manager.configure_shell = lambda: events.append(("shell",))

        self.assertEqual(manager.enable_nvm_multi_node(), "v22.0.0")
        self.assertLess(events.index(("remove_system", ("nodejs", "npm"))), events.index(("install_nvm",)))
        self.assertLess(events.index(("install_nvm",)), events.index(("install_lts",)))

    def test_provider_ui_is_one_way_and_has_no_switch_back_to_debian(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        controller = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "controller.py").read_text()
        php = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "managers" / "php.py").read_text()
        node = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "managers" / "node.py").read_text()

        self.assertIn('Gtk.Button(label="Enable Sury Multi-PHP")', gui)
        self.assertIn('Gtk.Button(label="Enable NVM Multi-Node")', gui)
        self.assertNotIn('Gtk.Button(label="Switch to Debian PHP")', gui)
        self.assertNotIn('Gtk.Button(label="Switch to Debian Node")', gui)
        self.assertNotIn('Gtk.Button(label="Normalize to Debian")', gui)
        self.assertIn("def enable_sury_multi_php", controller)
        self.assertIn("def enable_nvm_multi_node", controller)
        self.assertNotIn("def switch_php_provider", controller)
        self.assertNotIn("def switch_node_provider", controller)
        self.assertNotIn("def switch_to_debian", php)
        self.assertNotIn("def switch_to_debian", node)


class MissingExecutableRegressionTests(unittest.TestCase):
    def test_command_runner_missing_executable_returns_127(self):
        from nativedev.system import CommandRunner

        runner = CommandRunner()
        try:
            result = runner.run(["nativedev-command-that-does-not-exist"], timeout=1)
            self.assertEqual(result.returncode, 127)
            self.assertFalse(result.ok)
        finally:
            runner.close()

    def test_php_cli_version_is_empty_when_php_binary_is_missing(self):
        from nativedev.system import CommandResult, DistroInfo

        class Runner:
            def run(self, argv, **kwargs):
                self.argv = list(argv)
                return CommandResult(list(argv), 127, "", "No such file or directory")

        distro = DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13")
        manager = PhpManager(Runner(), None, None, distro)
        self.assertEqual(manager.cli_version(), "")


    def test_php_cli_version_does_not_invoke_runner_when_php_is_absent(self):
        from unittest.mock import patch
        from nativedev.system import DistroInfo

        class Runner:
            def run(self, argv, **kwargs):
                raise AssertionError("runner must not be called when php is absent from PATH")

        distro = DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13")
        manager = PhpManager(Runner(), None, None, distro)
        with patch("nativedev.managers.php.shutil.which", return_value=None):
            self.assertEqual(manager.cli_version(), "")

    def test_php_refresh_source_detects_sury_before_parallel_catalog(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        php_page = gui[gui.index("class PhpPage"):gui.index("class NodePage")]
        self.assertIn("sury = self.context.php.sury_configured()", php_page)
        self.assertIn("available = self.context.php.available_versions() if provider == \"sury\" else []", php_page)

    def test_php_available_versions_require_sury(self):
        from nativedev.system import DistroInfo

        class Runner:
            def run(self, argv, **kwargs):
                raise AssertionError("APT metadata should not be queried before Sury is configured")

        distro = DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13")
        manager = PhpManager(Runner(), None, None, distro)
        manager.sury_configured = lambda: False
        self.assertEqual(manager.available_versions(), [])

if __name__ == "__main__":
    unittest.main()
