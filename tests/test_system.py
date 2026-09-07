import json
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
        self.assertTrue(info.is_ubuntu_family)
        self.assertEqual(info.ubuntu_codename, "noble")
        self.assertEqual(info.codename, "noble")

    def test_ubuntu_codename_marks_derivative_even_without_ubuntu_id_like(self):
        info = self.parse('ID=example\nID_LIKE=debian\nVERSION_CODENAME=custom\nUBUNTU_CODENAME=jammy\n')
        self.assertTrue(info.is_ubuntu_family)
        self.assertTrue(info.is_debian_family)
        self.assertEqual(info.codename, "jammy")


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

    def test_nginx_ready_requires_current_domain_and_park_signature(self):
        from unittest.mock import patch
        from nativedev.config import AppConfig
        from nativedev.managers.localdev import LocalDevManager

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            park = base / "Code"
            park.mkdir()
            site = base / "nativedev-sites.conf"
            enabled = base / "nativedev-enabled.conf"
            config = AppConfig(park_dir=str(park), domain="test")
            manager = LocalDevManager(None, None, None, config, StubPhp())
            site.write_text(manager.render_nginx(), encoding="utf-8")
            enabled.touch()
            with patch("nativedev.managers.localdev.NGINX_SITE", site), patch(
                "nativedev.managers.localdev.NGINX_ENABLED", enabled
            ):
                self.assertTrue(manager.nginx_ready())
                config.domain = "tests"
                self.assertFalse(manager.nginx_ready())
                config.domain = "test"
                new_park = base / "Work"
                new_park.mkdir()
                config.park_dir = str(new_park)
                self.assertFalse(manager.nginx_ready())

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

    def test_error_status_wraps_without_expanding_window_and_can_be_copied(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        main = gui[gui.index("class MainWindow"):gui.index("class NativeDevApplication")]
        self.assertIn("self.status.set_wrap(True)", main)
        self.assertIn("self.status.set_wrap_mode(Pango.WrapMode.WORD_CHAR)", main)
        self.assertIn("self.status.set_max_width_chars(72)", main)
        self.assertIn('self.status_copy = Gtk.Button(label="Copy")', main)
        self.assertIn('self.status_copy.set_tooltip_text("Copy error message")', main)
        self.assertIn("display.get_clipboard().set(self._status_message)", main)
        self.assertIn("self.status.set_selectable(True)", main)

    def test_database_password_dialogs_are_compact(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        start = gui.index("def prompt_existing_database_password")
        end = gui.index("def prompt_database_admin_password", start)
        existing = gui[start:end]
        self.assertIn("dialog.set_default_size(440, -1)", existing)
        self.assertIn("_constrain_dialog_text", existing)
        self.assertIn("password.set_hexpand(True)", existing)
        helper = gui[gui.index("def _constrain_dialog_text"):gui.index("def page_header")]
        self.assertIn("widget.set_max_width_chars(max_width_chars)", helper)
        self.assertIn("Pango.WrapMode.WORD_CHAR", helper)

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
        protocol = 23
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "systemd.service", "verb": "restart", "now": False, "service": "nginx"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "systemd.service", "verb": "disable", "now": True, "service": "php8.4-fpm"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "apt.install", "packages": ["redis-tools"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "apt.reinstall_confmiss", "packages": ["php8.4-fpm"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.install_packages", "packages": ["php8.4-cli", "php8.4-fpm", "php8.4-gd", "php8.4-opcache"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.install_packages", "packages": ["php-cli", "php-fpm", "php-gd"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.install_packages", "packages": ["php-cli", "php-fpm"], "allow_downgrades": True}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "apt.remove", "packages": ["nodejs", "npm"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.enable_modules", "version": "8.4", "sapi": "cli", "modules": ["gd", "mysqli", "pdo_mysql", "opcache"]}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.extension_install", "version": "8.4", "extension": "xdebug"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.extension_enable", "version": "8.4", "extension": "redis"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.extension_disable", "version": "8.4", "extension": "xdebug"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.extension_remove", "version": "8.4", "extension": "imagick"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.ini.apply", "version": "8.4", "settings": {"memory_limit": "512M", "opcache.enable": "1"}}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.ini.reset", "version": "8.4"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "networkmanager.reload", "scope": "conf"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "file.install", "mode": "0644", "source": "/tmp/nativedev-fpm-test/pool.conf", "destination": "/etc/php/8.4/fpm/pool.d/nativedev-1000.conf"}, uid=1000))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "file.remove", "paths": ["/etc/php/8.4/fpm/pool.d/nativedev-1000.conf"]}, uid=1000))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "file.remove", "paths": ["/etc/php/8.4/fpm/pool.d/nativedev-1001.conf"]}, uid=1000))

    def test_rejects_raw_commands_and_outside_packages(self):
        protocol = 23
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "run", "argv": ["bash", "-c", "id"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "apt.install", "packages": ["openssh-server"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "apt.install", "packages": ["/tmp/nativedev-test/debsuryorg-archive-keyring.deb"]}))
        from unittest.mock import patch
        with patch("nativedev.privileged_helper._php_multi_repo_target", return_value=("sury", "trixie")):
            self.assertTrue(self.operation_ok({"protocol": protocol, "action": "php.multi_repo.configure", "backend": "sury", "codename": "trixie"}))
            self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.multi_repo.configure", "backend": "sury", "codename": "evil-suite"}))
            self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.multi_repo.configure", "backend": "ondrej", "codename": "trixie"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "systemd.service", "verb": "restart", "now": False, "service": "ssh"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "apt.reinstall_confmiss", "packages": ["nginx"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.install_packages", "packages": ["nginx"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.install_packages", "packages": ["php-arbitrary-root-tool"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.enable_modules", "version": "8.4", "sapi": "apache2", "modules": ["gd"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.enable_modules", "version": "8.4", "sapi": "cli", "modules": ["xdebug"]}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.extension_enable", "version": "8.4", "extension": "evil"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.extension_install", "version": "8.5", "extension": "opcache"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.extension_enable", "version": "8.4", "extension": "gd", "sapi": "cli"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.extension_install", "version": "8.4", "extension": "gd", "package": "php8.4-xdebug"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.ini.apply", "version": "8.4", "settings": {"memory_limit": "512M\nauto_prepend_file=/tmp/x.php"}}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.ini.apply", "version": "8.4", "settings": {"memory_limit": "512M\rdisplay_errors=1"}}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.ini.apply", "version": "8.4", "settings": {"memory_limit": "512M\0x"}}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.ini.apply", "version": "8.4", "settings": {"bad name": "1"}}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.ini.apply", "version": "8.4", "settings": {"[section]": "1"}}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.ini.apply", "version": "8.4", "settings": {"foo=bar": "1"}}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.ini.apply", "version": "8.4", "settings": {"extension": "redis.so"}}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.ini.apply", "version": "8.4", "settings": {"zend_extension": "xdebug.so"}}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.ini.apply", "version": "8.4", "settings": {"auto_prepend_file": "/tmp/bootstrap.php"}}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.ini.apply", "version": "8.4", "settings": {"auto_append_file": "/tmp/footer.php"}}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.ini.apply", "version": "8.4", "settings": {"memory_limit": "512M"}, "path": "/etc/php/8.4/php.ini"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "php.ini.reset", "version": "8.4", "settings": {"memory_limit": "512M"}}))

    def test_php_multi_repo_root_detection_uses_ubuntu_codename_for_derivatives(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import _php_multi_repo_target

        release = {
            "ID": "linuxmint",
            "ID_LIKE": "ubuntu debian",
            "VERSION_CODENAME": "wilma",
            "UBUNTU_CODENAME": "noble",
        }
        with patch("nativedev.privileged_helper._read_os_release", return_value=release):
            self.assertEqual(_php_multi_repo_target(), ("ondrej", "noble"))

    def test_php_multi_repo_ubuntu_configure_uses_fixed_ondrej_ppa(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import execute_operation
        import subprocess

        request = {
            "protocol": 23,
            "action": "php.multi_repo.configure",
            "backend": "ondrej",
            "codename": "noble",
        }
        completed = subprocess.CompletedProcess(["add-apt-repository"], 0, "", "")
        with patch("nativedev.privileged_helper._validate_php_multi_repo_request", return_value=("ondrej", "noble")), \
             patch("nativedev.privileged_helper.shutil.which", return_value="/usr/bin/add-apt-repository"), \
             patch("nativedev.privileged_helper.subprocess.run", return_value=completed) as run:
            result = execute_operation(request, uid=1000, timeout=120)

        self.assertEqual(result.returncode, 0)
        argv = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(argv, ["/usr/bin/add-apt-repository", "-y", "--sourceslist", "deb https://ppa.launchpadcontent.net/ondrej/php/ubuntu noble main"])
        self.assertEqual(kwargs["env"]["LC_ALL"], "C.UTF-8")
        self.assertEqual(kwargs["env"]["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")

    def test_apt_remove_fails_immediately_when_dpkg_is_busy(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import command_for_operation

        with patch("nativedev.privileged_helper._binary", side_effect=lambda name: f"/usr/bin/{name}"):
            argv = command_for_operation({
                "protocol": 23,
                "action": "apt.remove",
                "packages": ["mariadb-server"],
            }, uid=1000)
        self.assertEqual(argv, [
            "/usr/bin/apt-get", "-o", "DPkg::Lock::Timeout=0",
            "remove", "-y", "mariadb-server",
        ])

    def test_apt_remove_execution_is_noninteractive_and_can_run_to_completion(self):
        import subprocess
        from unittest.mock import patch
        from nativedev.privileged_helper import execute_operation

        request = {
            "protocol": 23,
            "action": "apt.remove",
            "packages": ["mariadb-server"],
            "timeout": None,
        }
        completed = subprocess.CompletedProcess(["apt-get"], 0, "", "")
        with patch("nativedev.privileged_helper._binary", side_effect=lambda name: f"/usr/bin/{name}"), \
             patch("nativedev.privileged_helper.subprocess.run", return_value=completed) as run:
            result = execute_operation(request, uid=1000, timeout=None)

        self.assertEqual(result.returncode, 0)
        argv = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(argv, [
            "/usr/bin/apt-get", "-o", "DPkg::Lock::Timeout=0",
            "remove", "-y", "mariadb-server",
        ])
        self.assertIsNone(kwargs["timeout"])
        self.assertEqual(kwargs["env"]["DEBIAN_FRONTEND"], "noninteractive")
        self.assertEqual(kwargs["env"]["APT_LISTCHANGES_FRONTEND"], "none")
        self.assertEqual(kwargs["env"]["NEEDRESTART_MODE"], "a")

    def test_client_and_helper_protocol_versions_match(self):
        from nativedev.system import PRIVILEGE_PROTOCOL_VERSION
        from nativedev.privileged_helper import PROTOCOL_VERSION
        self.assertEqual(PRIVILEGE_PROTOCOL_VERSION, 23)
        self.assertEqual(PROTOCOL_VERSION, PRIVILEGE_PROTOCOL_VERSION)

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
            "protocol": 23,
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

    def test_curated_extension_install_restores_ucf_and_enables_both_sapis(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import execute_operation
        from nativedev.system import CommandResult

        request = {"protocol": 23, "action": "php.extension_install", "version": "8.4", "extension": "redis"}
        with patch("nativedev.privileged_helper._binary", side_effect=lambda name: f"/usr/bin/{name}"), \
             patch("nativedev.privileged_helper.subprocess.run") as run, \
             patch("nativedev.privileged_helper._run_extension_module_pair") as modules:
            run.return_value.returncode = 0
            run.return_value.stdout = "installed"
            run.return_value.stderr = ""
            modules.return_value = __import__("subprocess").CompletedProcess(["modules"], 0, "enabled", "")
            result = execute_operation(request, uid=1000, timeout=1200)

        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/apt-get", "install", "--reinstall", "-y", "php8.4-redis"])
        self.assertEqual(kwargs["env"]["UCF_FORCE_CONFFMISS"], "1")
        modules.assert_called_once_with("8.4", ("redis",), True, 1200)
        self.assertEqual(result.returncode, 0)



class ServiceCleanupTests(unittest.TestCase):
    def _manager(self, dpkg_output: str):
        from nativedev.services import ServiceManager
        from nativedev.system import CommandResult

        class Runner:
            def __init__(self):
                self.operations = []
            def run(self, argv, **kwargs):
                if argv[:2] == ["dpkg-query", "-W"]:
                    return CommandResult(list(argv), 0, dpkg_output, "")
                if argv[-1:] == ["--version"] and "mariadb" in argv[0]:
                    return CommandResult(list(argv), 0, "mariadb  Ver 15.1 Distrib 11.8.3-MariaDB, for debian-linux-gnu", "")
                return CommandResult(list(argv), 0, "", "")
            def privileged_operation(self, action, **fields):
                self.operations.append((action, fields))
                return CommandResult([f"nativedev:{action}"], 0, "", "")

        class Apt:
            def __init__(self):
                self.removed = []
                self.remove_kwargs = []
            def candidate(self, package):
                return "1"
            def is_installed(self, package):
                return any(
                    line.startswith("ii ") and line.split("\t", 1)[-1].strip().split(":", 1)[0] == package
                    for line in dpkg_output.splitlines()
                )
            def remove(self, packages, **kwargs):
                self.removed.append(list(packages))
                self.remove_kwargs.append(dict(kwargs))
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
        runner = Runner()
        return ServiceManager(runner, apt, Systemd()), apt, runner

    def test_redis_server_and_cli_are_one_component(self):
        from nativedev.services import COMPONENTS
        redis = next(spec for spec in COMPONENTS if spec.key == "redis")
        self.assertEqual(redis.packages, ("redis-server", "redis-tools"))
        self.assertFalse(any(spec.key == "redis-cli" for spec in COMPONENTS))

    def test_database_components_use_original_apt_remove_behavior(self):
        from nativedev.services import COMPONENTS

        output = (
            "ii \tmariadb-server\n"
            "ii \tmariadb-client\n"
            "ii \tmariadb-server-core\n"
            "ii \tmariadb-common\n"
            "ii \tmysql-common\n"
            "ii \tpostgresql\n"
            "ii \tpostgresql-client\n"
            "ii \tpostgresql-17\n"
            "ii \tpostgresql-client-17\n"
            "ii \tpostgresql-common\n"
        )
        manager, apt, runner = self._manager(output)
        mariadb = next(item for item in COMPONENTS if item.key == "mariadb")
        postgresql = next(item for item in COMPONENTS if item.key == "postgresql")

        manager.uninstall(mariadb)
        manager.uninstall(postgresql)

        self.assertEqual(
            apt.removed[0],
            ["mariadb-client", "mariadb-server", "mariadb-server-core"],
        )
        self.assertEqual(
            apt.removed[1],
            ["postgresql", "postgresql-17", "postgresql-client", "postgresql-client-17"],
        )
        self.assertEqual(apt.remove_kwargs, [{"timeout": None}, {"timeout": None}])
        self.assertEqual(runner.operations, [])

    def test_database_data_reset_is_separate_semantic_operation(self):
        from nativedev.services import COMPONENTS

        manager, _apt, runner = self._manager("ii \tmariadb-server\nii \tmariadb-client\n")
        mariadb = next(item for item in COMPONENTS if item.key == "mariadb")
        manager.delete_database_data(mariadb)
        self.assertEqual(runner.operations[0][0], "database.delete_all_data")
        self.assertEqual(runner.operations[0][1]["key"], "mariadb")
        self.assertIsNone(runner.operations[0][1]["timeout"])

    def test_mariadb_mysql_is_one_debian_mariadb_component_in_requested_order(self):
        from nativedev.services import COMPONENTS

        self.assertEqual(
            [spec.key for spec in COMPONENTS],
            ["nginx", "mariadb", "postgresql", "redis", "memcached", "rabbitmq", "composer", "mkcert", "mailpit"],
        )
        mariadb = next(spec for spec in COMPONENTS if spec.key == "mariadb")
        self.assertEqual(mariadb.title, "MariaDB / MySQL")
        self.assertEqual(mariadb.packages, ("mariadb-server", "mariadb-client"))
        self.assertFalse(any(spec.key == "mysql" for spec in COMPONENTS))

    def test_rabbitmq_and_mailpit_are_exposed_as_native_services(self):
        from nativedev.services import COMPONENTS

        rabbitmq = next(spec for spec in COMPONENTS if spec.key == "rabbitmq")
        self.assertEqual(rabbitmq.title, "RabbitMQ")
        self.assertEqual(rabbitmq.packages, ("rabbitmq-server",))
        self.assertEqual(rabbitmq.service, "rabbitmq-server")
        self.assertEqual(rabbitmq.binary, "rabbitmqctl")

        mailpit = next(spec for spec in COMPONENTS if spec.key == "mailpit")
        self.assertEqual(mailpit.title, "Mailpit")
        self.assertEqual(mailpit.packages, ())
        self.assertEqual(mailpit.service, "mailpit")
        self.assertIn("localhost:8025", mailpit.note)
        self.assertIn("localhost:1025", mailpit.note)

    def test_mailpit_install_and_uninstall_use_fixed_semantic_helper_actions(self):
        from unittest.mock import patch
        from nativedev.services import COMPONENTS

        manager, _apt, runner = self._manager("")
        mailpit = next(spec for spec in COMPONENTS if spec.key == "mailpit")
        manager.install(mailpit)
        self.assertEqual(runner.operations[-1][0], "mailpit.install")
        self.assertEqual(runner.operations[-1][1]["timeout"], 600)

        with patch.object(manager, "_mailpit_managed", return_value=True):
            manager.uninstall(mailpit)
        self.assertEqual(runner.operations[-1][0], "mailpit.uninstall")
        self.assertEqual(runner.operations[-1][1]["timeout"], 300)

    def test_helper_allows_rabbitmq_and_restricts_mailpit_to_semantic_actions(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import command_for_operation, validate_operation

        with patch("nativedev.privileged_helper._binary", side_effect=lambda name: f"/usr/bin/{name}"):
            self.assertEqual(
                command_for_operation({
                    "protocol": 23,
                    "action": "apt.install",
                    "packages": ["rabbitmq-server"],
                }, uid=1000),
                ["/usr/bin/apt-get", "install", "-y", "rabbitmq-server"],
            )
            self.assertEqual(
                command_for_operation({
                    "protocol": 23,
                    "action": "systemd.service",
                    "verb": "start",
                    "now": False,
                    "service": "rabbitmq-server",
                }, uid=1000),
                ["/usr/bin/systemctl", "start", "rabbitmq-server"],
            )
            self.assertEqual(
                command_for_operation({
                    "protocol": 23,
                    "action": "systemd.service",
                    "verb": "restart",
                    "now": False,
                    "service": "mailpit",
                }, uid=1000),
                ["/usr/bin/systemctl", "restart", "mailpit"],
            )

        self.assertTrue(validate_operation({"protocol": 23, "action": "mailpit.install"})[0])
        self.assertTrue(validate_operation({"protocol": 23, "action": "mailpit.uninstall"})[0])
        self.assertFalse(validate_operation({
            "protocol": 23,
            "action": "mailpit.install",
            "url": "https://example.invalid/mailpit",
        })[0])
        self.assertFalse(validate_operation({
            "protocol": 23,
            "action": "mailpit.uninstall",
            "path": "/tmp/mailpit",
        })[0])

    def test_mailpit_service_is_localhost_only_and_non_root(self):
        from nativedev.privileged_helper import MAILPIT_SERVICE_CONTENT

        self.assertIn("--listen 127.0.0.1:8025", MAILPIT_SERVICE_CONTENT)
        self.assertIn("--smtp 127.0.0.1:1025", MAILPIT_SERVICE_CONTENT)
        self.assertIn("DynamicUser=yes", MAILPIT_SERVICE_CONTENT)
        self.assertIn("StateDirectory=mailpit", MAILPIT_SERVICE_CONTENT)
        self.assertIn("# Managed by NativeDev", MAILPIT_SERVICE_CONTENT)

    def test_mariadb_version_is_detected_for_ui(self):
        from unittest.mock import patch
        from nativedev.services import COMPONENTS

        output = "ii \tmariadb-server\nii \tmariadb-client\n"
        manager, _apt, _runner = self._manager(output)
        mariadb = next(spec for spec in COMPONENTS if spec.key == "mariadb")
        with patch("nativedev.services.shutil.which", return_value="/usr/bin/mariadb"):
            state = manager.state(mariadb)
        self.assertEqual(state.version, "11.8.3-MariaDB")

    def test_redis_uninstall_removes_server_and_cli_package_together(self):
        from nativedev.services import COMPONENTS

        output = "ii \tredis-server\nii \tredis-tools\n"
        manager, apt, _runner = self._manager(output)
        spec = next(item for item in COMPONENTS if item.key == "redis")
        manager.uninstall(spec)
        self.assertEqual(apt.removed[-1], ["redis-server", "redis-tools"])

    def test_helper_allows_only_fixed_database_data_reset_targets(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import validate_operation

        with patch("nativedev.privileged_helper._database_username_for_uid", return_value="developer"):
            self.assertTrue(validate_operation({"protocol": 23, "action": "database.delete_all_data", "key": "mariadb"})[0])
            self.assertTrue(validate_operation({"protocol": 23, "action": "database.delete_all_data", "key": "postgresql"})[0])
            self.assertFalse(validate_operation({"protocol": 23, "action": "database.delete_all_data", "key": "redis"})[0])
            self.assertFalse(validate_operation({"protocol": 23, "action": "database.delete_all_data", "key": "mariadb", "path": "/tmp/evil"})[0])
            self.assertFalse(validate_operation({"protocol": 23, "action": "database.cleanup_component", "key": "mariadb"})[0])


class ManagerPackageExportTests(unittest.TestCase):
    def test_database_access_manager_is_exported_from_managers_package(self):
        from nativedev.managers import DatabaseAccessManager
        from nativedev.managers.database_access import DatabaseAccessManager as DirectDatabaseAccessManager

        self.assertIs(DatabaseAccessManager, DirectDatabaseAccessManager)


class DatabaseAccessManagerTests(unittest.TestCase):
    def _manager(self, root: Path, status="0"):
        from nativedev.managers.database_access import DatabaseAccessManager
        from nativedev.system import CommandResult

        class Runner:
            def __init__(self):
                self.operations = []
                self.runs = []
                self.status = status
            def privileged_operation(self, action, **fields):
                self.operations.append((action, dict(fields)))
                stdout = self.status if action.endswith("account_status") else ""
                return CommandResult([f"nativedev:{action}"], 0, stdout, "")
            def run(self, argv, **fields):
                self.runs.append((list(argv), dict(fields)))
                sql = fields.get("input_text") or ""
                if sql.startswith("SELECT CURRENT_USER"):
                    stdout = "developer@localhost\n"
                elif sql.startswith("SELECT current_user"):
                    stdout = "developer\n"
                else:
                    stdout = ""
                return CommandResult(list(argv), 0, stdout, "")

        runner = Runner()
        manager = DatabaseAccessManager(runner, root / "database-credentials.json", developer_username="developer")
        return manager, runner

    def test_fresh_database_install_creates_default_managed_account_and_0600_store(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            manager, runner = self._manager(Path(td), status="0")
            with patch("nativedev.managers.database_access.shutil.which", side_effect=lambda name: "/usr/bin/mariadb" if name == "mariadb" else None):
                state = manager.ensure_after_install("mariadb")
            self.assertTrue(state.managed)
            self.assertFalse(state.conflict)
            self.assertEqual(state.username, "developer")
            self.assertEqual(state.password, "nativedev")
            self.assertEqual(state.host, "localhost")
            self.assertEqual(state.port, 3306)
            self.assertEqual(runner.operations[0][0], "database.mysql.account_status")
            self.assertEqual(runner.operations[1][0], "database.mysql.ensure_dev_account")
            self.assertEqual(runner.operations[1][1]["password"], "nativedev")
            self.assertEqual(manager.credential_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(manager.credential_file.parent.stat().st_mode & 0o777, 0o700)

    def test_forget_removes_saved_database_credential_config(self):
        with tempfile.TemporaryDirectory() as td:
            manager, _runner = self._manager(Path(td), status="0")
            manager._save_managed("mariadb", "Existing123!", transport="tcp")
            self.assertTrue(manager.credential_file.exists())
            manager.forget("mariadb")
            self.assertFalse(manager.credential_file.exists())
            self.assertFalse(manager.state("mariadb").managed)

    def test_legacy_fixed_username_metadata_is_not_reused_for_current_user(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            credential = root / "database-credentials.json"
            credential.write_text(json.dumps({
                "version": 1,
                "databases": {
                    "postgresql": {
                        "managed": True,
                        "conflict": False,
                        "username": "nativedev",
                        "password": "OldPass123!",
                    }
                },
            }))
            manager, runner = self._manager(root, status="0")
            with patch("nativedev.managers.database_access.shutil.which", return_value="/usr/bin/psql"):
                state = manager.ensure_after_install("postgresql")
            self.assertEqual(state.username, "developer")
            self.assertEqual(state.password, "nativedev")
            self.assertEqual([action for action, _ in runner.operations], [
                "database.postgresql.ensure_cluster",
                "database.postgresql.account_status",
                "database.postgresql.ensure_dev_account",
            ])

    def test_postgresql_uses_tcp_loopback_for_password_authentication(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            manager, _runner = self._manager(Path(td), status="0")
            with patch("nativedev.managers.database_access.shutil.which", return_value="/usr/bin/psql"):
                state = manager.ensure_after_install("postgresql")
            self.assertEqual(state.host, "localhost")
            self.assertEqual(state.port, 5432)
            self.assertEqual(state.database, "postgres")

    def test_existing_unmanaged_current_user_account_is_not_overwritten_automatically(self):
        with tempfile.TemporaryDirectory() as td:
            manager, runner = self._manager(Path(td), status="1")
            state = manager.ensure_after_install("postgresql")
            self.assertFalse(state.managed)
            self.assertTrue(state.conflict)
            self.assertEqual([action for action, _ in runner.operations], [
                "database.postgresql.ensure_cluster",
                "database.postgresql.account_status",
            ])

    def test_use_existing_mariadb_user_verifies_then_stores_without_changing_password(self):
        from unittest.mock import patch
        from nativedev.system import CommandResult

        with tempfile.TemporaryDirectory() as td:
            manager, runner = self._manager(Path(td), status="0")
            sql_seen = []

            def mysql_sql(password, sql, *, transport="tcp"):
                sql_seen.append((password, sql, transport))
                self.assertEqual(transport, "tcp")
                if sql.startswith("SELECT CURRENT_USER") and password == "Existing123!":
                    return CommandResult(["mariadb"], 0, "developer@localhost\n", "")
                return CommandResult(["mariadb"], 1, "", "ERROR 1045 access denied")

            with patch.object(manager, "_mysql_user_sql", side_effect=mysql_sql):
                state = manager.use_existing_account("mariadb", "Existing123!")
            self.assertTrue(state.managed)
            self.assertEqual(state.username, "developer")
            self.assertEqual(state.password, "Existing123!")
            self.assertEqual(state.host, "localhost")
            self.assertEqual(runner.operations, [])
            self.assertEqual(len(sql_seen), 1)
            self.assertTrue(sql_seen[0][1].startswith("SELECT CURRENT_USER"))

    def test_use_existing_mariadb_user_wrong_password_changes_nothing(self):
        from unittest.mock import patch
        from nativedev.system import CommandResult

        with tempfile.TemporaryDirectory() as td:
            manager, runner = self._manager(Path(td), status="0")

            def mysql_sql(password, sql, *, transport="tcp"):
                self.assertEqual(transport, "tcp")
                return CommandResult(["mariadb"], 1, "", "ERROR 1045 access denied")

            with patch.object(manager, "_mysql_user_sql", side_effect=mysql_sql):
                with self.assertRaisesRegex(RuntimeError, "1045|authenticate"):
                    manager.use_existing_account("mariadb", "Wrong123!")
            self.assertFalse(manager.state("mariadb").managed)
            self.assertEqual(runner.operations, [])


    def test_change_and_reset_password_are_self_service_after_credential_is_known(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            manager, runner = self._manager(Path(td), status="0")
            with patch("nativedev.managers.database_access.shutil.which", return_value="/usr/bin/psql"):
                manager.ensure_after_install("postgresql")
                runner.operations.clear()
                changed = manager.change_password("postgresql", "LocalDev123!")
                self.assertEqual(changed.password, "LocalDev123!")
                reset = manager.reset_password("postgresql")
            self.assertEqual(reset.password, "nativedev")
            self.assertEqual(runner.operations, [])
            sqls = [fields.get("input_text", "") for _argv, fields in runner.runs]
            self.assertTrue(any("ALTER ROLE CURRENT_USER PASSWORD 'LocalDev123!'" in sql for sql in sqls))
            self.assertTrue(any("ALTER ROLE CURRENT_USER PASSWORD 'nativedev'" in sql for sql in sqls))

    def test_password_validation_rejects_sql_and_control_characters(self):
        from nativedev.managers.database_access import DatabaseAccessManager
        for value in ("", "has space", "quote'", 'double"', "slash\\", "line\nbreak", "nul\0byte"):
            with self.assertRaises(RuntimeError, msg=repr(value)):
                DatabaseAccessManager.validate_password(value)
        self.assertEqual(DatabaseAccessManager.validate_password("Dev-123!"), "Dev-123!")

    def test_mariadb_default_account_uses_privileged_admin_path_and_verifies_default_password(self):
        from unittest.mock import patch
        from nativedev.system import CommandResult

        with tempfile.TemporaryDirectory() as td:
            manager, runner = self._manager(Path(td), status="0")

            def mysql_sql(password, sql, *, transport="tcp"):
                self.assertEqual(transport, "tcp")
                if sql.startswith("SELECT CURRENT_USER") and password == "nativedev":
                    return CommandResult(["mariadb"], 0, "developer@localhost\n", "")
                return CommandResult(["mariadb"], 1, "", "ERROR 1045 access denied")

            with patch.object(manager, "_mysql_user_sql", side_effect=mysql_sql):
                state = manager.create_local_access("mariadb")
            self.assertTrue(state.managed)
            self.assertEqual(state.password, "nativedev")
            self.assertEqual(state.host, "localhost")
            self.assertEqual([action for action, _ in runner.operations], ["database.mysql.ensure_dev_account"])
            self.assertEqual(runner.operations[0][1]["password"], "nativedev")
            self.assertNotIn("admin_password", runner.operations[0][1])

    def test_mariadb_default_account_prompts_for_root_password_then_retries(self):
        from unittest.mock import patch
        from nativedev.managers.database_access import DatabaseAdminPasswordRequired
        from nativedev.system import CommandResult

        with tempfile.TemporaryDirectory() as td:
            manager, runner = self._manager(Path(td), status="0")

            def privileged_operation(action, **fields):
                runner.operations.append((action, dict(fields)))
                if action == "database.mysql.ensure_dev_account" and "admin_password" not in fields:
                    return CommandResult(
                        ["nativedev:database.mysql.ensure_dev_account"],
                        77,
                        "",
                        "NATIVEDEV_MYSQL_ROOT_PASSWORD_REQUIRED",
                    )
                return CommandResult([f"nativedev:{action}"], 0, "", "")

            runner.privileged_operation = privileged_operation
            with self.assertRaises(DatabaseAdminPasswordRequired):
                manager.create_local_access("mariadb")
            self.assertFalse(manager.state("mariadb").managed)

            def mysql_sql(password, sql, *, transport="tcp"):
                if sql.startswith("SELECT CURRENT_USER") and password == "nativedev":
                    return CommandResult(["mariadb"], 0, "developer@localhost\n", "")
                return CommandResult(["mariadb"], 1, "", "ERROR 1045 access denied")

            with patch.object(manager, "_mysql_user_sql", side_effect=mysql_sql):
                state = manager.create_local_access("mariadb", admin_password="Root secret! with spaces")
            self.assertTrue(state.managed)
            self.assertEqual(state.password, "nativedev")
            self.assertEqual(runner.operations[-1][1]["admin_password"], "Root secret! with spaces")

    def test_mysql_password_verification_never_falls_back_to_socket_auth(self):
        from unittest.mock import patch
        from nativedev.system import CommandResult

        with tempfile.TemporaryDirectory() as td:
            manager, _runner = self._manager(Path(td), status="0")
            transports = []

            def mysql_sql(password, sql, *, transport="tcp"):
                transports.append(transport)
                if transport == "socket":
                    return CommandResult(["mariadb"], 0, "developer@localhost\n", "")
                return CommandResult(["mariadb"], 1, "", "ERROR 1045 access denied")

            with patch.object(manager, "_mysql_user_sql", side_effect=mysql_sql):
                with self.assertRaisesRegex(RuntimeError, "1045|authenticate"):
                    manager._verify_mysql_login("Wrong123!", preferred_transport="socket")
            self.assertEqual(transports, ["tcp"])


    def test_mariadb_password_change_is_not_saved_until_new_login_is_verified(self):
        from unittest.mock import patch
        from nativedev.system import CommandResult

        with tempfile.TemporaryDirectory() as td:
            manager, _runner = self._manager(Path(td), status="0")
            manager._save_managed("mariadb", "Existing123!", transport="tcp")

            def mysql_sql(password, sql, *, transport="tcp"):
                if sql.startswith("SELECT CURRENT_USER"):
                    if password == "Existing123!":
                        return CommandResult(["mariadb"], 0, "developer@localhost\n", "")
                    return CommandResult(["mariadb"], 1, "", "ERROR 1045 access denied")
                if sql.startswith("SELECT VERSION"):
                    return CommandResult(["mariadb"], 0, "11.8.3-MariaDB\n", "")
                if sql.startswith("SET PASSWORD"):
                    # Simulate the exact reported failure mode: SQL exits zero but
                    # the requested password is not actually usable afterward.
                    return CommandResult(["mariadb"], 0, "", "")
                return CommandResult(["mariadb"], 1, "", "unexpected SQL")

            with patch.object(manager, "_mysql_user_sql", side_effect=mysql_sql):
                with self.assertRaisesRegex(RuntimeError, "1045|authenticate"):
                    manager.reset_password("mariadb")
            # NativeDev must retain the last verified credential and must not
            # report/save the unverified default password.
            self.assertEqual(manager.state("mariadb").password, "Existing123!")

    def test_mariadb_self_service_password_change_uses_mariadb_set_password_and_updates_store(self):
        from unittest.mock import patch
        from nativedev.system import CommandResult

        with tempfile.TemporaryDirectory() as td:
            manager, _runner = self._manager(Path(td), status="0")
            manager._save_managed("mariadb", "Existing123!", transport="tcp")
            valid_passwords = {"Existing123!"}
            sql_seen = []

            def mysql_sql(password, sql, *, transport="tcp"):
                sql_seen.append(sql)
                if sql.startswith("SELECT CURRENT_USER"):
                    if password not in valid_passwords:
                        return CommandResult(["mariadb"], 1, "", "ERROR 1045 access denied")
                    return CommandResult(["mariadb"], 0, "developer@localhost\n", "")
                if sql.startswith("SELECT VERSION"):
                    return CommandResult(["mariadb"], 0, "11.8.3-MariaDB\n", "")
                if sql.startswith("SET PASSWORD"):
                    valid_passwords.clear()
                    valid_passwords.add("NewPass123!")
                    return CommandResult(["mariadb"], 0, "", "")
                return CommandResult(["mariadb"], 1, "", "unexpected SQL")

            with patch.object(manager, "_mysql_user_sql", side_effect=mysql_sql):
                state = manager.change_password("mariadb", "NewPass123!")
            self.assertEqual(state.password, "NewPass123!")
            self.assertTrue(any(sql.startswith("SET PASSWORD = PASSWORD('NewPass123!')") for sql in sql_seen))



class DatabasePrivilegedHelperTests(unittest.TestCase):
    def operation_ok(self, request):
        from unittest.mock import patch
        from nativedev.privileged_helper import validate_operation
        with patch("nativedev.privileged_helper._binary", side_effect=lambda name: f"/usr/bin/{name}"):
            return validate_operation(request, uid=1000)[0]

    def test_database_rpc_derives_username_from_peer_and_rejects_client_user_or_sql_selectors(self):
        protocol = 23
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "database.mysql.account_status"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "database.mysql.ensure_dev_account", "password": "nativedev"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "database.mysql.ensure_dev_account", "password": "nativedev", "admin_password": "root secret !@#"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "database.postgresql.account_status"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "database.postgresql.ensure_dev_account", "password": "Dev-123!"}))
        self.assertTrue(self.operation_ok({"protocol": protocol, "action": "database.postgresql.ensure_cluster"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "database.postgresql.ensure_cluster", "version": "17"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "database.mysql.ensure_dev_account", "password": "x", "user": "root"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "database.mysql.ensure_dev_account", "password": "x", "sql": "GRANT ALL"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "database.mysql.ensure_dev_account", "password": "bad'quote"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "database.postgresql.ensure_dev_account", "password": "nativedev", "admin_password": "rootpass"}))
        self.assertFalse(self.operation_ok({"protocol": protocol, "action": "database.mysql.ensure_dev_account", "password": "nativedev", "admin_password": "line\nbreak"}))

    def test_mysql_helper_grants_dev_capabilities_without_grant_option(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import execute_operation
        import subprocess

        request = {"protocol": 23, "action": "database.mysql.ensure_dev_account", "password": "nativedev"}
        calls = []

        def run_admin(sql, admin_password, timeout, env):
            calls.append((sql, admin_password))
            return subprocess.CompletedProcess(["/usr/bin/mariadb"], 0, "", "")

        with patch("nativedev.privileged_helper._database_username_for_uid", return_value="developer"), \
             patch("nativedev.privileged_helper._run_mysql_admin", side_effect=run_admin):
            execute_operation(request, uid=1000, timeout=30)

        self.assertEqual(calls[0], ("SELECT 1;\n", None))
        sql = calls[1][0]
        self.assertIn("'developer'@'localhost'", sql)
        grant_line = next(line for line in sql.splitlines() if line.startswith("GRANT "))
        self.assertIn("CREATE", grant_line)
        self.assertIn("CREATE ROUTINE", grant_line)
        self.assertNotIn("GRANT OPTION", grant_line)
        self.assertNotIn("CREATE USER", grant_line)

    def test_mysql_helper_requests_root_password_only_after_no_password_login_fails(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import execute_operation
        import subprocess

        request = {"protocol": 23, "action": "database.mysql.ensure_dev_account", "password": "nativedev"}
        with patch("nativedev.privileged_helper._database_username_for_uid", return_value="developer"), \
             patch("nativedev.privileged_helper._run_mysql_admin") as run_admin:
            run_admin.return_value = subprocess.CompletedProcess(["mariadb"], 1, "", "ERROR 1045")
            result = execute_operation(request, uid=1000, timeout=30)
        self.assertEqual(result.returncode, 77)
        self.assertEqual(result.stderr, "NATIVEDEV_MYSQL_ROOT_PASSWORD_REQUIRED")
        run_admin.assert_called_once()

    def test_mysql_helper_retries_with_one_shot_root_password(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import execute_operation
        import subprocess

        request = {
            "protocol": 23,
            "action": "database.mysql.ensure_dev_account",
            "password": "nativedev",
            "admin_password": "Root secret !@#",
        }
        calls = []

        def run_admin(sql, admin_password, timeout, env):
            calls.append((sql, admin_password))
            return subprocess.CompletedProcess(["mariadb"], 0, "", "")

        with patch("nativedev.privileged_helper._database_username_for_uid", return_value="developer"), \
             patch("nativedev.privileged_helper._run_mysql_admin", side_effect=run_admin):
            result = execute_operation(request, uid=1000, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls[0], ("SELECT 1;\n", "Root secret !@#"))
        self.assertEqual(calls[1][1], "Root secret !@#")

    def test_mysql_root_password_stays_out_of_process_argv_and_temp_file_is_removed(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import _run_mysql_admin
        import subprocess

        seen = {}

        def fake_run(argv, **kwargs):
            config_arg = next(item for item in argv if item.startswith("--defaults-extra-file="))
            config_path = Path(config_arg.split("=", 1)[1])
            seen["path"] = config_path
            seen["mode"] = config_path.stat().st_mode & 0o777
            seen["contents"] = config_path.read_text()
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(list(argv), 0, "1\n", "")

        with patch("nativedev.privileged_helper._binary", return_value="/usr/bin/mariadb"), \
             patch("nativedev.privileged_helper.subprocess.run", side_effect=fake_run):
            result = _run_mysql_admin("SELECT 1;\n", 'Root "secret" \\ value', 30, {"PATH": "/usr/bin"})

        self.assertEqual(result.returncode, 0)
        self.assertEqual(seen["mode"], 0o600)
        self.assertFalse(seen["path"].exists())
        self.assertFalse(any("Root" in item for item in seen["argv"]))
        self.assertIn('password="Root \\"secret\\" \\\\ value"', seen["contents"])

    def test_postgresql_cluster_helper_creates_main_cluster_when_reset_left_none(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import execute_operation
        import subprocess

        request = {"protocol": 23, "action": "database.postgresql.ensure_cluster"}
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if "pg_lsclusters" in argv[0]:
                return subprocess.CompletedProcess(list(argv), 0, "", "")
            if "pg_createcluster" in argv[0]:
                return subprocess.CompletedProcess(list(argv), 0, "", "")
            return subprocess.CompletedProcess(list(argv), 1, "", "unexpected command")

        binaries = {
            "pg_lsclusters": "/usr/bin/pg_lsclusters",
            "pg_createcluster": "/usr/bin/pg_createcluster",
        }
        with patch("nativedev.privileged_helper._database_username_for_uid", return_value="developer"), \
             patch("nativedev.privileged_helper._binary", side_effect=lambda name: binaries[name]), \
             patch("nativedev.privileged_helper._installed_postgresql_versions", return_value=["17"]), \
             patch("nativedev.privileged_helper.subprocess.run", side_effect=fake_run):
            result = execute_operation(request, uid=1000, timeout=120)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls[0], ["/usr/bin/pg_lsclusters", "--no-header"])
        self.assertEqual(calls[1], ["/usr/bin/pg_createcluster", "--start", "17", "main"])

    def test_postgresql_cluster_helper_starts_existing_default_cluster(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import execute_operation
        import subprocess

        request = {"protocol": 23, "action": "database.postgresql.ensure_cluster"}
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if "pg_lsclusters" in argv[0]:
                return subprocess.CompletedProcess(
                    list(argv), 0,
                    "17 main 5432 down postgres /var/lib/postgresql/17/main /var/log/postgresql/postgresql-17-main.log\n",
                    "",
                )
            if "pg_ctlcluster" in argv[0]:
                return subprocess.CompletedProcess(list(argv), 0, "", "")
            return subprocess.CompletedProcess(list(argv), 1, "", "unexpected command")

        binaries = {
            "pg_lsclusters": "/usr/bin/pg_lsclusters",
            "pg_ctlcluster": "/usr/bin/pg_ctlcluster",
        }
        with patch("nativedev.privileged_helper._database_username_for_uid", return_value="developer"), \
             patch("nativedev.privileged_helper._binary", side_effect=lambda name: binaries[name]), \
             patch("nativedev.privileged_helper.subprocess.run", side_effect=fake_run):
            result = execute_operation(request, uid=1000, timeout=120)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls[-1], ["/usr/bin/pg_ctlcluster", "17", "main", "start"])

    def test_postgresql_helper_role_is_createdb_but_not_superuser_or_createrole(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import execute_operation
        import subprocess

        request = {"protocol": 23, "action": "database.postgresql.ensure_dev_account", "password": "nativedev"}
        with patch("nativedev.privileged_helper._database_username_for_uid", return_value="developer"), \
             patch("nativedev.privileged_helper._postgres_admin_argv", return_value=["/usr/bin/runuser", "psql"]), \
             patch("nativedev.privileged_helper.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["psql"], 0, "", "")
            execute_operation(request, uid=1000, timeout=30)
        sql = run.call_args.kwargs["input"]
        self.assertIn('ROLE "developer"', sql)
        self.assertIn("CREATEDB", sql)
        self.assertIn("NOSUPERUSER", sql)
        self.assertIn("NOCREATEROLE", sql)
        self.assertIn("NOREPLICATION", sql)
        self.assertIn("NOBYPASSRLS", sql)


class DatabaseDataResetHelperTests(unittest.TestCase):
    def test_mariadb_reset_removes_only_fixed_default_datadir(self):
        from unittest.mock import patch, call
        from nativedev.privileged_helper import execute_operation

        request = {"protocol": 23, "action": "database.delete_all_data", "key": "mariadb"}
        with patch("nativedev.privileged_helper._database_username_for_uid", return_value="developer"), \
             patch("nativedev.privileged_helper._remove_fixed_tree") as remove:
            result = execute_operation(request, uid=1000, timeout=90)
        self.assertEqual(result.returncode, 0)
        remove.assert_called_once_with(Path("/var/lib/mysql"))

    def test_postgresql_reset_removes_cluster_data_and_cluster_config(self):
        from unittest.mock import patch, call
        from nativedev.privileged_helper import execute_operation

        request = {"protocol": 23, "action": "database.delete_all_data", "key": "postgresql"}
        with patch("nativedev.privileged_helper._database_username_for_uid", return_value="developer"), \
             patch("nativedev.privileged_helper._remove_fixed_tree") as remove:
            result = execute_operation(request, uid=1000, timeout=90)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(remove.call_args_list, [
            call(Path("/var/lib/postgresql")),
            call(Path("/etc/postgresql")),
        ])


class DatabaseAccessUiTests(unittest.TestCase):
    def test_page_action_refreshes_after_partial_mutation_failure(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        page = gui[gui.index("class Page(Gtk.ScrolledWindow):"):gui.index("class DashboardPage(Page):")]
        self.assertEqual(page.count("if after:"), 2)
        self.assertGreaterEqual(page.count("after()"), 2)

    def test_services_page_uses_controller_install_and_shows_database_credentials_controls(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        services = gui[gui.index("class ServicesPage"):gui.index("class ProjectsPage")]
        self.assertIn("self.context.controller.install_component(s)", services)
        self.assertIn('label("Local database access", "row-title")', services)
        self.assertIn('Gtk.Button(label="Reveal")', services)
        self.assertIn('Gtk.Button(label="Change password")', services)
        self.assertIn('Gtk.Button(label="Reset to default")', services)
        self.assertIn('Gtk.Button(label="Use existing user")', services)
        self.assertIn('Gtk.Button(label="Use NativeDev default user")', services)
        self.assertNotIn('Gtk.Button(label="Take over account")', services)
        self.assertNotIn("prompt_default_database_account", gui)
        self.assertIn("create_database_access(", services)
        self.assertIn("admin_password=admin_password", services)
        self.assertIn("use_existing_database_access", services)
        self.assertIn('label("Current database password", "row-title")', gui)
        self.assertIn('label("MariaDB/MySQL root password", "row-title")', gui)
        self.assertIn("DatabaseAdminPasswordRequired", gui)
        self.assertIn('Gtk.CheckButton(label="Delete all database data and accounts")', gui)
        self.assertIn("delete_database_data=delete_data", services)
        self.assertIn("Packages and NativeDev configuration will be removed.", gui)
        self.assertIn("Existing database data and accounts will be preserved.", gui)
        self.assertIn('label(f"Version {state.version}", "muted")', services)
        self.assertNotIn('password.set_placeholder_text', gui)



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

    def test_localdev_domain_change_reconciles_dns_and_existing_nginx(self):
        from nativedev.controller import NativeDevController

        events = []

        class Config:
            park_dir = "/tmp/nativedev-test-user/Code"
            domain = "test"
            https_enabled = False
            def save(self): events.append(("save", self.park_dir, self.domain))

        class LocalDev:
            def __init__(self): self.config = Config()
            def nginx_managed(self): return True
            def dns_strategy(self): return "networkmanager"
            def configure_dns(self): events.append(("dns", self.config.domain))
            def configure_nginx_sites(self): events.append(("nginx", self.config.park_dir, self.config.domain))

        localdev = LocalDev()
        controller = NativeDevController(object(), localdev)
        controller.update_localdev_settings("/tmp/nativedev-test-user/Code", "tests")
        self.assertEqual(localdev.config.domain, "tests")
        self.assertIn(("dns", "tests"), events)
        self.assertIn(("nginx", "/tmp/nativedev-test-user/Code", "tests"), events)

    def test_localdev_park_change_rebuilds_nginx_without_touching_dns(self):
        from nativedev.controller import NativeDevController

        events = []

        class Config:
            park_dir = "/tmp/nativedev-test-user/Code"
            domain = "test"
            https_enabled = False
            def save(self): events.append(("save", self.park_dir, self.domain))

        class LocalDev:
            def __init__(self): self.config = Config()
            def nginx_managed(self): return True
            def dns_strategy(self): events.append(("dns_strategy",)); return "networkmanager"
            def configure_dns(self): events.append(("dns", self.config.domain))
            def configure_nginx_sites(self): events.append(("nginx", self.config.park_dir, self.config.domain))

        localdev = LocalDev()
        controller = NativeDevController(object(), localdev)
        controller.update_localdev_settings("/tmp/nativedev-test-user/Work", "test")
        self.assertEqual(localdev.config.park_dir, "/tmp/nativedev-test-user/Work")
        self.assertIn(("nginx", "/tmp/nativedev-test-user/Work", "test"), events)
        self.assertFalse(any(event[0].startswith("dns") for event in events))

    def test_localdev_reconcile_failure_rolls_config_and_router_back(self):
        from nativedev.controller import NativeDevController

        events = []

        class Config:
            park_dir = "/tmp/nativedev-test-user/Code"
            domain = "test"
            https_enabled = False
            def save(self): events.append(("save", self.park_dir, self.domain))

        class LocalDev:
            def __init__(self):
                self.config = Config()
                self.nginx_calls = 0
            def nginx_managed(self): return True
            def dns_strategy(self): return "networkmanager"
            def configure_dns(self): events.append(("dns", self.config.domain))
            def configure_nginx_sites(self):
                self.nginx_calls += 1
                events.append(("nginx", self.config.park_dir, self.config.domain))
                if self.nginx_calls == 1:
                    raise RuntimeError("nginx failed")

        localdev = LocalDev()
        controller = NativeDevController(object(), localdev)
        with self.assertRaisesRegex(RuntimeError, "rolled back"):
            controller.update_localdev_settings("/tmp/nativedev-test-user/Work", "tests")
        self.assertEqual(localdev.config.park_dir, "/tmp/nativedev-test-user/Code")
        self.assertEqual(localdev.config.domain, "test")
        self.assertIn(("dns", "tests"), events)
        self.assertIn(("dns", "test"), events)
        self.assertIn(("nginx", "/tmp/nativedev-test-user/Code", "test"), events)

    def test_localdev_domain_change_regenerates_https_instead_of_plain_nginx_rebuild(self):
        from nativedev.controller import NativeDevController

        events = []

        class Config:
            park_dir = "/tmp/nativedev-test-user/Code"
            domain = "test"
            https_enabled = True
            def save(self): events.append(("save", self.domain))

        class LocalDev:
            def __init__(self): self.config = Config()
            def nginx_managed(self): return True
            def dns_strategy(self): return "networkmanager"
            def configure_dns(self): events.append(("dns", self.config.domain))
            def enable_https(self): events.append(("https", self.config.domain))
            def configure_nginx_sites(self): events.append(("nginx", self.config.domain))

        localdev = LocalDev()
        NativeDevController(object(), localdev).update_localdev_settings("/tmp/nativedev-test-user/Code", "tests")
        self.assertEqual(events[-2:], [("dns", "tests"), ("https", "tests")])
        self.assertFalse(any(event[0] == "nginx" for event in events))

    def test_php_uninstall_detaches_nativedev_ini_and_preserves_profile(self):
        from nativedev.controller import NativeDevController

        events = []

        class Php:
            def uninstall_version(self, version): events.append(("uninstall", version))

        class LocalDev:
            def nginx_managed(self): return False

        class Ini:
            def has_active_override(self, version): return True
            def detach_runtime(self, version): events.append(("detach_ini", version))
            def restore_profile(self, version): events.append(("restore_ini", version))

        controller = NativeDevController(Php(), LocalDev(), php_ini=Ini())
        controller.uninstall_php("8.4")
        self.assertEqual(events[:2], [("detach_ini", "8.4"), ("uninstall", "8.4")])
        self.assertNotIn(("restore_ini", "8.4"), events)

    def test_php_uninstall_failure_restores_saved_ini_profile(self):
        from nativedev.controller import NativeDevController

        events = []

        class Php:
            def uninstall_version(self, version):
                events.append(("uninstall", version))
                raise RuntimeError("apt failed")

        class LocalDev:
            def nginx_managed(self): return False

        class Ini:
            def has_active_override(self, version): return True
            def detach_runtime(self, version): events.append(("detach_ini", version))
            def restore_profile(self, version): events.append(("restore_ini", version))

        controller = NativeDevController(Php(), LocalDev(), php_ini=Ini())
        with self.assertRaisesRegex(RuntimeError, "apt failed"):
            controller.uninstall_php("8.4")
        self.assertEqual(events, [("detach_ini", "8.4"), ("uninstall", "8.4"), ("restore_ini", "8.4")])


    def test_database_component_install_auto_provisions_local_access(self):
        from nativedev.controller import NativeDevController
        from nativedev.services import ComponentSpec

        events = []
        class Services:
            def install(self, spec): events.append(("install", spec.key))
        class Db:
            def supports(self, key): return key == "postgresql"
            def ensure_after_install(self, key): events.append(("access", key))
        controller = NativeDevController(object(), object(), services=Services(), database_access=Db())
        controller.install_component(ComponentSpec("postgresql", "PostgreSQL", ("postgresql",)))
        self.assertEqual(events, [("install", "postgresql"), ("access", "postgresql")])

    def test_database_uninstall_deletes_data_only_when_checkbox_requested(self):
        from nativedev.controller import NativeDevController
        from nativedev.services import ComponentSpec

        events = []
        class Services:
            def uninstall(self, spec): events.append(("uninstall", spec.key))
            def delete_database_data(self, spec): events.append(("delete_data", spec.key))
        class Db:
            def forget(self, key): events.append(("forget", key))

        spec = ComponentSpec("mariadb", "MariaDB / MySQL", ("mariadb-server",))
        controller = NativeDevController(object(), object(), services=Services(), database_access=Db())
        controller.uninstall_component(spec)
        self.assertEqual(events, [("uninstall", "mariadb"), ("forget", "mariadb")])

        events.clear()
        controller.uninstall_component(spec, delete_database_data=True)
        self.assertEqual(events, [
            ("uninstall", "mariadb"),
            ("delete_data", "mariadb"),
            ("forget", "mariadb"),
        ])

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
    def test_php_provider_uses_multi_mode_whenever_multi_repo_is_active(self):
        from nativedev.system import DistroInfo

        manager = PhpManager(None, object(), None, DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13"))
        manager.multi_php_configured = lambda: True
        manager.installed_versions = lambda: ["8.4"]
        self.assertEqual(manager.provider(), "multi")

    def test_php_provider_uses_system_without_multi_repo(self):
        from nativedev.system import DistroInfo

        manager = PhpManager(None, object(), None, DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13"))
        manager.multi_php_configured = lambda: False
        manager.installed_versions = lambda: ["8.4"]
        self.assertEqual(manager.provider(), "system")

    def test_php_multi_repo_backend_is_sury_on_debian_and_ondrej_on_ubuntu_derivative(self):
        from nativedev.system import DistroInfo

        debian = PhpManager(None, object(), None, DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13"))
        ubuntu_derivative = PhpManager(
            None, object(), None,
            DistroInfo("linuxmint", "Linux Mint", "22", "noble", ("ubuntu", "debian"), "Linux Mint 22", "noble"),
        )
        self.assertEqual(debian.expected_multi_php_backend, "sury")
        self.assertEqual(debian.multi_php_repository_name, "Sury")
        self.assertTrue(debian.multi_php_supported)
        self.assertEqual(ubuntu_derivative.expected_multi_php_backend, "ondrej")
        self.assertEqual(ubuntu_derivative.multi_php_repository_name, "Ondřej PHP PPA")
        self.assertTrue(ubuntu_derivative.multi_php_supported)

    def test_php_multi_repo_detection_recognizes_existing_sury_and_ondrej_sources(self):
        from unittest.mock import patch
        from nativedev.system import DistroInfo

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sources = root / "sources.list.d"
            sources.mkdir()
            main = root / "sources.list"
            main.write_text("# no multi-PHP repository here\n", encoding="utf-8")

            ppa = sources / "ondrej.list"
            ppa.write_text(
                "deb https://ppa.launchpadcontent.net/ondrej/php/ubuntu noble main\n",
                encoding="utf-8",
            )
            ubuntu = PhpManager(
                None, object(), None,
                DistroInfo("linuxmint", "Linux Mint", "22", "noble", ("ubuntu", "debian"), "Linux Mint 22", "noble"),
            )
            with patch("nativedev.managers.php.APT_SOURCES_LIST", main), \
                 patch("nativedev.managers.php.APT_SOURCES_DIR", sources):
                self.assertEqual(ubuntu.multi_php_backend(), "ondrej")
                self.assertTrue(ubuntu.multi_php_configured())

            ppa.unlink()
            sury = sources / "sury.sources"
            sury.write_text(
                "Types: deb\n"
                "URIs: https://packages.sury.org/php/\n"
                "Suites: trixie\n"
                "Components: main\n",
                encoding="utf-8",
            )
            debian = PhpManager(None, object(), None, DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13"))
            with patch("nativedev.managers.php.APT_SOURCES_LIST", main), \
                 patch("nativedev.managers.php.APT_SOURCES_DIR", sources):
                self.assertEqual(debian.multi_php_backend(), "sury")
                self.assertTrue(debian.multi_php_configured())

    def test_php_system_to_multi_migrates_in_place_after_repository_preflight(self):
        from nativedev.system import DistroInfo

        events = []
        manager = PhpManager(None, object(), None, DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13"))
        manager.default_fpm_version = lambda: "8.4"
        manager.cli_version = lambda: "8.4"
        manager.multi_php_configured = lambda: False
        manager.configure_multi_php = lambda explicit=False: events.append(("configure_multi", explicit))
        manager.available_versions = lambda: ["8.5", "8.4"]
        manager.installed_versions = lambda: ["8.4"]
        manager.install_version = lambda version: events.append(("install_multi", version))
        manager.set_cli_default = lambda version: events.append(("default", version))

        self.assertEqual(manager.enable_multi_php(), "8.4")
        self.assertEqual(events[0], ("configure_multi", True))
        self.assertIn(("install_multi", "8.4"), events)
        self.assertIn(("default", "8.4"), events)

    def test_multi_php_migration_needed_reports_system_package_origin_without_changing_provider(self):
        from nativedev.system import DistroInfo

        class Apt:
            def is_installed(self, package): return package == "php8.4-cli"

        manager = PhpManager(None, Apt(), None, DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13"))
        manager.multi_php_configured = lambda: True
        manager.installed_versions = lambda: ["8.4"]
        manager.installed_package_provider = lambda package: "system"
        self.assertEqual(manager.provider(), "multi")
        self.assertTrue(manager.multi_php_migration_needed())

    def test_node_provider_nvm_wins_when_system_node_is_also_present(self):
        class Apt:
            def is_installed(self, package): return package == "nodejs"

        manager = NodeManager(None, Apt())
        manager.installed = lambda: True
        self.assertEqual(manager.provider(), "nvm")

    def test_node_provider_uses_system_name_for_distribution_packages(self):
        class Apt:
            def is_installed(self, package): return package == "nodejs"

        manager = NodeManager(None, Apt())
        manager.installed = lambda: False
        self.assertEqual(manager.provider(), "system")

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

    def test_provider_ui_is_one_way_and_uses_system_wording(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        controller = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "controller.py").read_text()
        php = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "managers" / "php.py").read_text()
        node = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "managers" / "node.py").read_text()

        self.assertIn('Gtk.Button(label="Enable Multi-PHP")', gui)
        self.assertIn('Gtk.Button(label="Enable NVM Multi-Node")', gui)
        self.assertNotIn('Gtk.Button(label="Switch to System PHP")', gui)
        self.assertNotIn('Gtk.Button(label="Switch to Debian Node")', gui)
        self.assertNotIn('Gtk.Button(label="Normalize to Debian")', gui)
        self.assertNotIn("Install Debian PHP", gui)
        self.assertNotIn("Debian system PHP", gui)
        self.assertNotIn("Debian Node", gui)
        self.assertIn("def enable_multi_php", controller)
        self.assertIn("def enable_nvm_multi_node", controller)
        self.assertNotIn("def switch_php_provider", controller)
        self.assertNotIn("def switch_node_provider", controller)
        self.assertNotIn("def switch_php_provider", php)
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

    def test_php_refresh_checks_provider_before_parallel_catalog(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        php_page = gui[gui.index("class PhpPage"):gui.index("class NodePage")]
        self.assertIn('provider = self.context.php.provider()', php_page)
        self.assertIn('available = self.context.php.available_versions() if provider == "multi" else []', php_page)

    def test_php_available_versions_require_multi_repo(self):
        from nativedev.system import DistroInfo

        class Runner:
            def run(self, argv, **kwargs):
                raise AssertionError("APT metadata should not be queried before Multi-PHP is configured")

        distro = DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13")
        manager = PhpManager(Runner(), None, None, distro)
        manager.multi_php_configured = lambda: False
        self.assertEqual(manager.available_versions(), [])


class PhpExtensionManagerTests(unittest.TestCase):
    def _manager(self, root, *, installed=None, apt_simulation="", manual="", runtime_modules="", runtime_version="", common_files=""):
        from nativedev.managers.php_extensions import PhpExtensionManager
        from nativedev.system import CommandResult

        installed = set(installed or {"php8.4-cli", "php8.4-fpm"})

        class Php:
            def installed_versions(self): return ["8.4"]
            def fpm_config_ready(self, version): return True

        class Runner:
            def __init__(self): self.operations = []; self.commands = []
            def run(self, argv, **kwargs):
                self.commands.append(list(argv))
                if argv[:3] == ["apt-get", "-s", "remove"]:
                    return CommandResult(list(argv), 0, apt_simulation, "")
                if argv[:2] == ["apt-mark", "showmanual"]:
                    return CommandResult(list(argv), 0, manual, "")
                if len(argv) >= 3 and str(argv[0]).endswith("php8.4") and argv[1:] == ["-n", "-m"]:
                    return CommandResult(list(argv), 0, runtime_modules, "")
                if len(argv) >= 3 and str(argv[0]).endswith("php8.4") and argv[1:] == ["-r", "echo PHP_VERSION;"]:
                    return CommandResult(list(argv), 0, runtime_version, "")
                if argv[:3] == ["dpkg-query", "-L", "php8.4-common"]:
                    return CommandResult(list(argv), 0, common_files, "")
                return CommandResult(list(argv), 0, "", "")
            def privileged_operation(self, action, **fields):
                self.operations.append((action, fields))
                return CommandResult([f"nativedev:{action}"], 0, "", "")

        class Apt:
            def __init__(self): self.installed = installed; self.installs = []; self.removes = []
            def is_installed(self, package): return package in self.installed
            def candidate(self, package): return "1"
            def install_php(self, packages):
                self.installs.append(list(packages))
                self.installed.update(packages)
            def remove(self, packages):
                self.removes.append(list(packages))
                self.installed.difference_update(packages)

        class Systemd:
            def __init__(self): self.reloads = []; self.restarts = []
            def is_active(self, service): return False
            def reload(self, service): self.reloads.append(service)
            def restart(self, service): self.restarts.append(service)

        runner, apt, systemd = Runner(), Apt(), Systemd()
        return PhpExtensionManager(runner, apt, systemd, Php(), config_root=Path(root)), runner, apt, systemd

    def test_mysql_package_maps_to_all_mysql_modules(self):
        from nativedev.managers.php_extensions import EXTENSIONS_BY_KEY
        spec = EXTENSIONS_BY_KEY["mysql"]
        self.assertEqual(spec.package_suffix, "mysql")
        self.assertEqual(spec.modules, ("mysqlnd", "mysqli", "pdo_mysql"))

    def test_root_helper_curated_catalog_matches_application_catalog(self):
        from nativedev.managers.php_extensions import EXTENSIONS_BY_KEY
        from nativedev.privileged_helper import PHP_EXTENSION_CATALOG
        self.assertEqual(set(EXTENSIONS_BY_KEY), set(PHP_EXTENSION_CATALOG))
        for key, spec in EXTENSIONS_BY_KEY.items():
            suffix, modules, built_in_from = PHP_EXTENSION_CATALOG[key]
            self.assertEqual(suffix, spec.package_suffix)
            self.assertEqual(modules, spec.modules)
            self.assertEqual(built_in_from, spec.built_in_from)

    def test_enabled_state_requires_cli_and_fpm_together(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager, _runner, apt, _systemd = self._manager(root, installed={"php8.4-cli", "php8.4-fpm", "php8.4-mysql"})
            for sapi in ("cli", "fpm"):
                conf = root / "8.4" / sapi / "conf.d"
                mods = root / "8.4" / "mods-available"
                conf.mkdir(parents=True, exist_ok=True)
                mods.mkdir(parents=True, exist_ok=True)
                for module in ("mysqlnd", "mysqli", "pdo_mysql"):
                    source = mods / f"{module}.ini"
                    source.write_text(f"extension={module}.so\n")
                    target = conf / f"20-{module}.ini"
                    if not target.exists():
                        target.symlink_to(source)
            self.assertTrue(manager.extension_enabled("8.4", "mysql"))
            (root / "8.4" / "fpm" / "conf.d" / "20-pdo_mysql.ini").unlink()
            self.assertFalse(manager.extension_enabled("8.4", "mysql"))

    def test_install_and_disable_apply_to_cli_and_fpm_as_one_semantic_action(self):
        with tempfile.TemporaryDirectory() as td:
            manager, runner, apt, _systemd = self._manager(td)
            manager.install("8.4", "gd")
            install = next((action, fields) for action, fields in runner.operations if action == "php.extension_install")
            self.assertEqual(install[1]["version"], "8.4")
            self.assertEqual(install[1]["extension"], "gd")
            self.assertNotIn("sapi", install[1])
            # The real root helper installs the package; reflect that state in the
            # test double before exercising the next UI action.
            apt.installed.add("php8.4-gd")
            runner.operations.clear()
            manager.disable("8.4", "gd")
            self.assertEqual(runner.operations[0][0], "php.extension_disable")
            self.assertEqual(runner.operations[0][1]["extension"], "gd")
            self.assertNotIn("sapi", runner.operations[0][1])

    def test_opcache_is_built_in_from_php_85(self):
        from nativedev.managers.php_extensions import PhpExtensionManager
        self.assertFalse(PhpExtensionManager._version_key("8.4") >= (8, 5))
        with tempfile.TemporaryDirectory() as td:
            manager, _runner, _apt, _systemd = self._manager(td)
            self.assertFalse(manager.is_built_in("8.4", "opcache"))
            self.assertTrue(manager.is_built_in("8.5", "opcache"))

    def test_runtime_inventory_shows_compiled_and_php_common_modules_without_package_actions(self):
        runtime = "[PHP Modules]\nCore\ndate\njson\nopenssl\nPDO\n[Zend Modules]\n"
        common = "/etc/php/8.4/mods-available/ctype.ini\n/etc/php/8.4/mods-available/fileinfo.ini\n"
        with tempfile.TemporaryDirectory() as td:
            manager, _runner, _apt, _systemd = self._manager(
                td,
                installed={"php8.4-cli", "php8.4-fpm", "php8.4-common"},
                runtime_modules=runtime,
                common_files=common,
            )
            from unittest.mock import patch
            with patch("nativedev.managers.php_extensions.Path.is_file", return_value=True):
                modules = manager.runtime_modules("8.4")
            self.assertIn("json", modules)
            self.assertIn("openssl", modules)
            self.assertIn("PDO", modules)
            self.assertIn("ctype", modules)
            self.assertIn("fileinfo", modules)
            self.assertNotIn("Core", modules)

    def test_prerelease_detection_uses_runtime_php_version(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            manager, _runner, _apt, _systemd = self._manager(td, runtime_version="8.4.0RC2")
            with patch("nativedev.managers.php_extensions.Path.is_file", return_value=True):
                self.assertTrue(manager.is_prerelease("8.4"))
            manager, _runner, _apt, _systemd = self._manager(td, runtime_version="8.4.3")
            with patch("nativedev.managers.php_extensions.Path.is_file", return_value=True):
                self.assertFalse(manager.is_prerelease("8.4"))

    def test_optional_catalog_includes_common_debian_sury_suggestions(self):
        from nativedev.managers.php_extensions import EXTENSIONS_BY_KEY
        for key in ("apcu", "bz2", "odbc", "snmp", "tidy", "mongodb", "ssh2", "yaml", "pcov"):
            self.assertIn(key, EXTENSIONS_BY_KEY)

    def test_uninstall_preflight_ignores_matching_generic_meta_but_blocks_unrelated_manual_package(self):
        simulation = "Remv php8.4-gd [8.4]\nRemv php-gd [2:8.4]\nRemv composer-plugin [1]\n"
        manual = "php8.4-gd\nphp-gd\ncomposer-plugin\n"
        with tempfile.TemporaryDirectory() as td:
            manager, _runner, _apt, _systemd = self._manager(
                td,
                installed={"php8.4-cli", "php8.4-fpm", "php8.4-gd"},
                apt_simulation=simulation,
                manual=manual,
            )
            self.assertEqual(manager.removal_impact("8.4", "gd"), ["composer-plugin"])

    def test_gui_keeps_php_extensions_as_contextual_php_subpage(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        pages = gui[gui.index("PAGES = ("):gui.index("PHP_SUBPAGES = (", gui.index("PAGES = ("))]
        self.assertIn('("php", "PHP", PhpPage)', pages)
        self.assertIn('("node", "Node.js", NodePage)', pages)
        self.assertNotIn('PHP Extensions', pages)
        self.assertNotIn('PHP Settings', pages)
        subpages = gui[gui.index("PHP_SUBPAGES = ("):gui.index("def __init__", gui.index("PHP_SUBPAGES = ("))]
        self.assertLess(subpages.index('("extensions", PhpExtensionsPage)'), subpages.index('("php_ini", PhpIniPage)'))
        php_page = gui[gui.index("class PhpPage"):gui.index("class PhpExtensionsPage")]
        self.assertIn('Gtk.Button(label="Extensions")', php_page)
        self.assertIn('Gtk.Button(label="Settings")', php_page)
        self.assertIn('open_php_subpage("extensions")', php_page)
        self.assertIn('open_php_subpage("php_ini")', php_page)
        extension_page = gui[gui.index("class PhpExtensionsPage"):gui.index("class NodePage")]
        self.assertIn("back=self.window.open_php_page", extension_page)
        self.assertIn("CLI and FPM are always changed together", extension_page)
        self.assertIn("Runtime / Core", extension_page)
        self.assertIn("Default PHP", extension_page)
        self.assertIn("state.package", extension_page)
        self.assertNotIn('copy.append(label(spec.title, "row-title"))', extension_page)
        # Explicit Refresh follows a newly changed system default, while dropdown
        # changes and extension mutations keep the user's selected PHP version.
        self.assertIn("self._refresh(prefer_default=True)", extension_page)
        self.assertIn("def refresh_selected(self):", extension_page)
        self.assertIn("self._refresh(prefer_default=False)", extension_page)
        self.assertIn("if prefer_default and cli in versions:", extension_page)
        # Normal extension states are communicated entirely by the far-right
        # action buttons; only Built-in and Unavailable retain a status pill.
        package_rows = extension_page[extension_page.index("row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)"):]
        self.assertIn("name.set_hexpand(True)", package_rows)
        self.assertLess(package_rows.index("row.append(name)"), package_rows.index("row.append(actions)"))
        self.assertNotIn('status_pill("Installed · Enabled"', package_rows)
        self.assertNotIn('status_pill("Installed · Disabled"', package_rows)
        self.assertNotIn('status_pill("Available"', package_rows)
        self.assertIn('status_pill("Unavailable", False)', package_rows)
        self.assertIn('status_pill("Pre-release", False)', extension_page)
        self.assertIn('self.context.php_extensions.is_prerelease(selected)', extension_page)
        style = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "style.css").read_text()
        self.assertIn(".extension-status", style)
        self.assertIn("padding: 1px 6px", style)
        self.assertIn("button {\n  min-height: 15px;", style)

    def test_php_subpage_back_button_sits_above_page_title(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text(encoding="utf-8")
        start = gui.index("def page_header")
        end = gui.index("\ndef card()", start)
        helper = gui[start:end]
        self.assertIn("outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL", helper)
        self.assertIn('back_button = Gtk.Button(label="← PHP")', helper)
        self.assertIn("back_button.set_halign(Gtk.Align.START)", helper)
        self.assertLess(helper.index("outer.append(back_button)"), helper.index("outer.append(row)"))
        self.assertIn("button.set_valign(Gtk.Align.CENTER)", helper)

    def test_status_pills_keep_natural_height(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text(encoding="utf-8")
        start = gui.index("def status_pill")
        end = gui.index("\ndef confirm(", start)
        helper = gui[start:end]
        self.assertIn("widget.set_valign(Gtk.Align.CENTER)", helper)
        main_window = gui[gui.index("class MainWindow"):gui.index("class NativeDevApplication")]
        self.assertIn("self.activity_spinner = Gtk.Spinner()", main_window)
        self.assertIn("self.activity_spinner.start()", main_window)
        self.assertIn("self.activity_spinner.stop()", main_window)
        self.assertIn('self.status.set_text("")', main_window)


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_phpmyadmin_and_adminer_runtime_state(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from nativedev.managers.doctor import Doctor

        class Distro:
            is_debian_family = True
            pretty_name = "Debian 13"

        class Apt:
            available = True

        class Systemd:
            available = True

        class Php:
            developer_user = "developer"
            multi_php_repository_name = "Multi-PHP"
            def multi_php_configured(self): return True
            def installed_versions(self): return ["8.4"]
            def fpm_config_ready(self, version): return version == "8.4"
            def developer_pool_configured(self, version): return version == "8.4"

        class Node:
            def provider(self): return "nvm"

        class DeveloperTools:
            def state(self, spec):
                if spec.key == "phpmyadmin":
                    return SimpleNamespace(
                        installed=True, document_root_ready=True, php_version="8.4",
                        runtime_ready=True, runtime_note="", url="http://phpmyadmin.localhost",
                    )
                return SimpleNamespace(
                    installed=False, document_root_ready=False, php_version="8.4",
                    runtime_ready=True, runtime_note="", url="http://adminer.localhost",
                )
            def adminer_sqlite_state(self):
                return SimpleNamespace(
                    installed=False, adminer_installed=False, php_version="",
                    runtime_ready=False, runtime_note="", url="http://adminer-sqlite.localhost",
                )

        class Services:
            def state(self, _spec):
                return SimpleNamespace(installed=True, running=False)

        class LocalDev:
            config = SimpleNamespace(domain="dev")
            def dns_ready(self): return True
            def dns_strategy(self): return "NetworkManager/dnsmasq"
            def nginx_ready(self): return True
            def projects(self): return []

        doctor = Doctor(Distro(), Apt(), Systemd(), Php(), Node(), DeveloperTools(), Services(), LocalDev())
        with patch("nativedev.managers.doctor.shutil.which", return_value="/usr/bin/pkexec"):
            checks = doctor.run()

        by_name = {item.name: item for item in checks}
        self.assertTrue(by_name["phpMyAdmin"].ok)
        self.assertIn("PHP 8.4", by_name["phpMyAdmin"].detail)
        self.assertIn("http://phpmyadmin.localhost", by_name["phpMyAdmin"].detail)
        self.assertFalse(by_name["Adminer"].ok)
        self.assertEqual(by_name["Adminer"].detail, "not installed")

    def test_context_wires_developer_tools_into_doctor(self):
        context = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "context.py").read_text()
        self.assertIn(
            "Doctor(distro, apt, systemd, php, node, developer_tools, services, localdev)",
            context,
        )


class PhpIniManagerTests(unittest.TestCase):
    def _manager(self, root: Path):
        from nativedev.managers.php_ini import PhpIniManager
        from nativedev.system import CommandResult

        class Php:
            def installed_versions(self): return ["8.4", "8.3"]
            def fpm_config_ready(self, version): return version in {"8.4", "8.3"}

        class Runner:
            def __init__(self): self.operations = []; self.commands = []
            def run(self, argv, **kwargs):
                self.commands.append((list(argv), kwargs))
                if len(argv) >= 2 and str(argv[0]).endswith("php8.4") and argv[1] == "-r":
                    return CommandResult(list(argv), 0, '{"memory_limit":"512M","display_errors":"1"}', "")
                return CommandResult(list(argv), 0, "{}", "")
            def privileged_operation(self, action, **fields):
                self.operations.append((action, fields))
                return CommandResult([f"nativedev:{action}"], 0, "", "")

        class Systemd:
            pass

        runner = Runner()
        manager = PhpIniManager(
            runner,
            Systemd(),
            Php(),
            config_root=root / "etc-php",
            profile_root=root / "profiles",
        )
        return manager, runner

    def test_root_helper_ini_validation_contract_matches_manager(self):
        from nativedev.managers import php_ini as manager
        from nativedev import privileged_helper as helper

        self.assertEqual(manager.DIRECTIVE_RE.pattern, helper.PHP_INI_DIRECTIVE_RE.pattern)
        self.assertEqual(manager.BLOCKED_DIRECTIVES, helper.PHP_INI_BLOCKED_DIRECTIVES)
        self.assertEqual(manager.MAX_SETTINGS, helper.PHP_INI_MAX_SETTINGS)
        self.assertEqual(manager.MAX_DIRECTIVE_LENGTH, helper.PHP_INI_MAX_DIRECTIVE_LENGTH)
        self.assertEqual(manager.MAX_VALUE_LENGTH, helper.PHP_INI_MAX_VALUE_LENGTH)

    def test_directive_and_value_validation_is_injection_safe(self):
        from nativedev.managers.php_ini import PhpIniManager

        for directive in ("memory_limit", "opcache.enable", "date.timezone", "A1_b.c"):
            PhpIniManager.validate_setting(directive, "512M")
        for directive in ("_bad", "bad-name", "bad name", "foo=bar", "[section]", ";comment"):
            with self.assertRaises(RuntimeError, msg=directive):
                PhpIniManager.validate_setting(directive, "1")
        for value in ("512M\nfoo=bar", "512M\rfoo=bar", "512M\0foo"):
            with self.assertRaisesRegex(RuntimeError, "single line"):
                PhpIniManager.validate_setting("memory_limit", value)
        for directive in ("extension", "zend_extension", "extension_dir"):
            with self.assertRaisesRegex(RuntimeError, "PHP Extensions"):
                PhpIniManager.validate_setting(directive, "anything")
        for directive in ("auto_prepend_file", "auto_append_file"):
            with self.assertRaisesRegex(RuntimeError, "inject PHP files"):
                PhpIniManager.validate_setting(directive, "/tmp/bootstrap.php")

    def test_reads_only_nativedev_owned_override_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager, _runner = self._manager(root)
            target = root / "etc-php" / "8.4" / "mods-available" / "nativedev.ini"
            target.parent.mkdir(parents=True)
            target.write_text(
                "; Managed by NativeDev\nmemory_limit = 512M\ndate.timezone = Asia/Dhaka\n",
                encoding="utf-8",
            )
            self.assertEqual(
                manager.settings("8.4"),
                {"memory_limit": "512M", "date.timezone": "Asia/Dhaka"},
            )

    def test_apply_uses_semantic_rpc_and_keeps_user_profile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager, runner = self._manager(root)
            manager.apply("8.4", {"memory_limit": "512M", "display_errors": "On"})
            self.assertEqual(runner.operations[0][0], "php.ini.apply")
            fields = runner.operations[0][1]
            self.assertEqual(fields["version"], "8.4")
            self.assertEqual(fields["settings"]["memory_limit"], "512M")
            self.assertNotIn("path", fields)
            self.assertNotIn("content", fields)
            self.assertNotIn("sapi", fields)
            profile = manager.profile_file("8.4")
            self.assertTrue(profile.is_file())
            self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
            self.assertEqual(manager.saved_profile("8.4")["display_errors"], "On")

    def test_reset_removes_only_saved_profile_client_side(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager, runner = self._manager(root)
            manager._write_profile("8.4", {"memory_limit": "512M"})
            manager.reset("8.4")
            self.assertEqual(runner.operations[-1][0], "php.ini.reset")
            self.assertFalse(manager.profile_file("8.4").exists())

    def test_effective_values_are_read_in_one_php_process(self):
        with tempfile.TemporaryDirectory() as td:
            manager, runner = self._manager(Path(td))
            values = manager.effective_settings("8.4", ("memory_limit", "display_errors"))
            self.assertEqual(values, {"memory_limit": "512M", "display_errors": "1"})
            php_calls = [call for call in runner.commands if str(call[0][0]).endswith("php8.4")]
            self.assertEqual(len(php_calls), 1)
            self.assertIn("NATIVEDEV_INI_KEYS", php_calls[0][1]["env"])

    def test_root_helper_uses_fixed_nativedev_file_and_both_sapi_links(self):
        from unittest.mock import patch
        from nativedev import privileged_helper as helper

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for path in (
                root / "8.4" / "mods-available",
                root / "8.4" / "cli" / "conf.d",
                root / "8.4" / "fpm" / "conf.d",
            ):
                path.mkdir(parents=True)
            with patch.object(helper, "PHP_CONFIG_ROOT", root), \
                 patch.object(helper, "_php_ini_runtime_ready"), \
                 patch.object(helper, "_validate_php_ini_runtime"), \
                 patch.object(helper, "_fpm_is_active", return_value=False):
                helper._execute_php_ini_change("8.4", {"memory_limit": "512M"}, 30)
                managed = root / "8.4" / "mods-available" / "nativedev.ini"
                cli = root / "8.4" / "cli" / "conf.d" / "99-nativedev.ini"
                fpm = root / "8.4" / "fpm" / "conf.d" / "99-nativedev.ini"
                self.assertIn("memory_limit = 512M", managed.read_text())
                self.assertTrue(cli.is_symlink())
                self.assertTrue(fpm.is_symlink())
                self.assertEqual(cli.readlink(), Path("../../mods-available/nativedev.ini"))
                self.assertEqual(fpm.readlink(), Path("../../mods-available/nativedev.ini"))

                helper._execute_php_ini_change("8.4", None, 30)
                self.assertFalse(managed.exists())
                self.assertFalse(cli.is_symlink())
                self.assertFalse(fpm.is_symlink())

    def test_gui_keeps_php_settings_as_contextual_php_subpage(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        pages = gui[gui.index("PAGES = ("):gui.index("PHP_SUBPAGES = (", gui.index("PAGES = ("))]
        self.assertNotIn('PHP Extensions', pages)
        self.assertNotIn('PHP Settings', pages)
        subpages = gui[gui.index("PHP_SUBPAGES = ("):gui.index("def __init__", gui.index("PHP_SUBPAGES = ("))]
        self.assertIn('("extensions", PhpExtensionsPage)', subpages)
        self.assertIn('("php_ini", PhpIniPage)', subpages)
        php_ini_page = gui[gui.index("class PhpIniPage"):gui.index("class NodePage")]
        self.assertIn("back=self.window.open_php_page", php_ini_page)
        self.assertIn("99-nativedev.ini", php_ini_page)
        self.assertIn("Extension loading is managed on PHP Extensions", php_ini_page)
        self.assertIn("newline, carriage-return and NUL", php_ini_page)

    def test_php_settings_save_reset_remain_visible_for_unsaved_final_removal(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        php_ini_page = gui[gui.index("class PhpIniPage"):gui.index("class NodePage")]
        dirty = php_ini_page.index("has_unsaved_changes = self.pending_settings != self.applied_settings")
        rows = php_ini_page.index("if self.pending_settings:", dirty)
        final_removal = php_ini_page.index("All NativeDev overrides are marked for removal", rows)
        actions_condition = php_ini_page.index("if self.pending_settings or has_unsaved_changes:", final_removal)
        save = php_ini_page.index('save = Gtk.Button(label="Save")', actions_condition)
        reset = php_ini_page.index('reset = Gtk.Button(label="Reset")', save)
        add = php_ini_page.index('add_heading = label("Add / update setting"', reset)
        self.assertLess(dirty, rows)
        self.assertLess(rows, final_removal)
        self.assertLess(final_removal, actions_condition)
        self.assertLess(actions_condition, save)
        self.assertLess(save, reset)
        self.assertLess(reset, add)
        self.assertIn("apply({}) performs reset", php_ini_page)
        self.assertNotIn("reset.set_sensitive", php_ini_page)


    def test_main_window_registers_php_subpages_without_sidebar_rows(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        main = gui[gui.index("class MainWindow"):gui.index("class NativeDevApplication")]
        self.assertIn("for key, klass in self.PHP_SUBPAGES:", main)
        self.assertIn("self.stack.add_named(page, key)", main)
        self.assertIn("def open_php_subpage(self, key: str)", main)
        self.assertIn('self.stack.set_visible_child_name("php")', main)
        open_php_page = main[main.index("def open_php_page(self) -> None:"):main.index("def set_activity", main.index("def open_php_page(self) -> None:"))]
        self.assertNotIn(".refresh()", open_php_page)
        # Sidebar rows are created only by the top-level PAGES loop.
        sidebar_loop = main[main.index("for key, title_text, klass in self.PAGES:"):main.index("for key, klass in self.PHP_SUBPAGES:")]
        self.assertIn("self.sidebar.append(row)", sidebar_loop)
        subpage_loop = main[main.index("for key, klass in self.PHP_SUBPAGES:"):main.index("self.sidebar.connect", main.index("for key, klass in self.PHP_SUBPAGES:"))]
        self.assertNotIn("self.sidebar.append", subpage_loop)


if __name__ == "__main__":
    unittest.main()


class LocalDevSettingsUiTests(unittest.TestCase):
    def test_save_settings_uses_controller_reconciliation(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        local = gui[gui.index("class LocalDevPage"):gui.index("class DoctorPage")]
        self.assertIn("self.context.controller.update_localdev_settings(park_value, value)", local)
        self.assertNotIn("self.context.config.park_dir = park_value", local)


class DeveloperToolIntegrationTests(unittest.TestCase):
    def test_helper_uses_fixed_apt_packages_and_rejects_client_package_fields(self):
        from unittest.mock import patch
        from nativedev.privileged_helper import command_for_operation, validate_operation

        with patch("nativedev.privileged_helper._binary", side_effect=lambda name: f"/usr/bin/{name}"):
            self.assertEqual(
                command_for_operation(
                    {"protocol": 23, "action": "developer_tool.install", "tool": "phpmyadmin"},
                    uid=1000,
                ),
                [
                    "/usr/bin/apt-get", "-o", "DPkg::Lock::Timeout=0",
                    "install", "-y", "--no-install-recommends", "phpmyadmin",
                ],
            )
            self.assertEqual(
                command_for_operation(
                    {"protocol": 23, "action": "developer_tool.uninstall", "tool": "adminer"},
                    uid=1000,
                ),
                [
                    "/usr/bin/apt-get", "-o", "DPkg::Lock::Timeout=0",
                    "remove", "-y", "adminer",
                ],
            )

        self.assertTrue(validate_operation({"protocol": 23, "action": "developer_tool.install", "tool": "adminer"})[0])
        self.assertFalse(validate_operation({"protocol": 23, "action": "developer_tool.install", "tool": "evil"})[0])
        self.assertFalse(validate_operation({
            "protocol": 23,
            "action": "developer_tool.install",
            "tool": "adminer",
            "package": "openssh-server",
        })[0])
        self.assertFalse(validate_operation({
            "protocol": 23,
            "action": "developer_tool.install",
            "tool": "phpmyadmin",
            "url": "https://example.invalid/tool.deb",
        })[0])

    def test_phpmyadmin_install_preseeds_no_webserver_and_no_dbconfig(self):
        from unittest.mock import patch
        import subprocess
        from nativedev.privileged_helper import execute_operation

        request = {"protocol": 23, "action": "developer_tool.install", "tool": "phpmyadmin"}
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("nativedev.privileged_helper._binary", side_effect=lambda name: f"/usr/bin/{name}"), \
             patch("nativedev.privileged_helper.subprocess.run", return_value=completed) as run, \
             patch("nativedev.privileged_helper._execute_phpmyadmin_reconcile", return_value=completed) as reconcile:
            result = execute_operation(request, uid=1000, timeout=None)

        self.assertEqual(result.returncode, 0)
        reconcile.assert_called_once_with(1000)
        self.assertEqual(run.call_count, 2)
        seed_call = run.call_args_list[0]
        self.assertEqual(seed_call.args[0], ["/usr/bin/debconf-set-selections"])
        self.assertIn("phpmyadmin/reconfigure-webserver multiselect", seed_call.kwargs["input"])
        self.assertIn("phpmyadmin/dbconfig-install boolean false", seed_call.kwargs["input"])
        apt_call = run.call_args_list[1]
        self.assertIn("--no-install-recommends", apt_call.args[0])
        self.assertEqual(apt_call.kwargs["env"]["DEBIAN_FRONTEND"], "noninteractive")

    def test_manager_tracks_distro_package_version_and_per_tool_php(self):
        from nativedev.managers.developer_tools import DeveloperToolManager, DeveloperToolSpec
        from nativedev.system import CommandResult

        class Config:
            domain = "test"
            https_enabled = False
            developer_tools = {}
            def save(self):
                pass

        class Apt:
            installed = {"adminer"}
            def is_installed(self, package):
                return package in self.installed
            def candidate(self, package):
                return "1.0" if package in {"adminer", "phpmyadmin"} else None

        class Php:
            def installed_fpm_versions(self):
                return ["8.4", "8.3"]
            def default_fpm_version(self):
                return "8.4"
            def fpm_config_ready(self, version):
                return version in {"8.4", "8.3"}

        class Runner:
            def __init__(self):
                self.operations = []
            def run(self, argv, **_kwargs):
                if argv[:2] == ["dpkg-query", "-W"]:
                    return CommandResult(list(argv), 0, "5.2.1+dfsg-1", "")
                return CommandResult(list(argv), 0, "", "")
            def privileged_operation(self, action, **fields):
                self.operations.append((action, dict(fields)))
                return CommandResult([f"nativedev:{action}"], 0, "", "")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            spec = DeveloperToolSpec("adminer", "Adminer", "adminer", root, "adminer.php", "Database administration")
            runner = Runner()
            manager = DeveloperToolManager(runner, Apt(), Php(), Config())
            state = manager.state(spec)
            self.assertTrue(state.installed)
            self.assertEqual(state.version, "5.2.1+dfsg-1")
            self.assertEqual(state.php_version, "8.4")
            self.assertEqual(state.url, "http://adminer.localhost")

            manager.set_selected_php("adminer", "8.3")
            self.assertEqual(manager.effective_php("adminer"), "8.3")

    def test_nginx_renders_developer_tool_hosts_on_fixed_localhost_domain(self):
        from nativedev.managers.developer_tools import DeveloperToolManager
        from nativedev.config import AppConfig

        class Apt:
            def is_installed(self, package):
                return package in {"phpmyadmin", "adminer"}

        with tempfile.TemporaryDirectory() as td:
            config = AppConfig(
                park_dir=td,
                domain="test",
                developer_tools={"phpmyadmin": {"php": "8.3"}},
            )
            manager = DeveloperToolManager(None, Apt(), StubPhp(default="8.4", installed=["8.4", "8.3"]), config)
            rendered = manager.render_nginx()
            self.assertIn("server_name phpmyadmin.localhost;", rendered)
            self.assertIn("server_name adminer.localhost;", rendered)
            self.assertIn('root "/usr/share/phpmyadmin";', rendered)
            self.assertIn('SCRIPT_FILENAME "/usr/share/adminer/adminer.php";', rendered)
            self.assertIn('fastcgi_pass "unix:/run/php/php8.3-fpm-nativedev-1000.sock";', rendered)
            self.assertIn('fastcgi_pass "unix:/run/php/php8.4-fpm-nativedev-1000.sock";', rendered)

            config.domain = "dev"
            rendered = manager.render_nginx()
            self.assertIn("server_name phpmyadmin.localhost;", rendered)
            self.assertIn("server_name adminer.localhost;", rendered)
            self.assertNotIn("server_name phpmyadmin.dev;", rendered)
            self.assertNotIn("server_name adminer.dev;", rendered)

    def test_developer_tool_urls_stay_http_even_when_local_https_is_enabled(self):
        from nativedev.managers.developer_tools import DeveloperToolManager, DEVELOPER_TOOL_BY_KEY
        from nativedev.system import CommandResult

        class Config:
            domain = "test"
            https_enabled = True
            developer_tools = {}
            def save(self):
                pass

        class Apt:
            def is_installed(self, package):
                return package == "adminer"
            def candidate(self, package):
                return "1.0"

        class Php:
            def installed_fpm_versions(self):
                return ["8.4"]
            def default_fpm_version(self):
                return "8.4"
            def fpm_config_ready(self, version):
                return True

        class Runner:
            def run(self, argv, **_kwargs):
                return CommandResult(list(argv), 0, "5.2.1", "")

        manager = DeveloperToolManager(Runner(), Apt(), Php(), Config())
        self.assertEqual(manager.state(DEVELOPER_TOOL_BY_KEY["adminer"]).url, "http://adminer.localhost")

    def test_nginx_uses_compiled_adminer_and_keeps_developer_tools_http_only(self):
        from nativedev.config import AppConfig
        from nativedev.managers.developer_tools import DeveloperToolManager

        class Apt:
            def is_installed(self, package):
                return package in {"phpmyadmin", "adminer"}

        with tempfile.TemporaryDirectory() as td:
            config = AppConfig(park_dir=td, domain="test", https_enabled=True)
            manager = DeveloperToolManager(None, Apt(), StubPhp(), config)
            rendered = manager.render_nginx()
            adminer_start = rendered.index("server_name adminer.localhost;")
            next_server = rendered.find("server {", adminer_start)
            adminer_block = rendered[adminer_start: next_server if next_server != -1 else len(rendered)]
            self.assertIn('/usr/share/adminer/adminer.php', adminer_block)
            self.assertNotIn('/usr/share/adminer/adminer/static', adminer_block)
            self.assertNotIn('listen 443', adminer_block)
            self.assertIn('allow 127.0.0.1;', adminer_block)
            self.assertIn('allow ::1;', adminer_block)
            self.assertIn('deny all;', adminer_block)
            pma_start = rendered.index("server_name phpmyadmin.localhost;")
            pma_end = rendered.find("server {", pma_start)
            pma_block = rendered[pma_start: pma_end if pma_end != -1 else len(rendered)]
            self.assertIn('try_files $uri =404;', pma_block)
            self.assertIn('PHP_ADMIN_VALUE "display_errors=Off\\nerror_reporting=E_ALL & ~E_DEPRECATED & ~E_USER_DEPRECATED"', pma_block)
            self.assertNotIn('listen 443', pma_block)
            self.assertIn('allow 127.0.0.1;', pma_block)
            self.assertIn('allow ::1;', pma_block)
            self.assertIn('deny all;', pma_block)

    def test_phpmyadmin_runtime_reconcile_writes_stable_secret_and_user_tempdir(self):
        import subprocess
        from types import SimpleNamespace
        from unittest.mock import patch
        import nativedev.privileged_helper as helper

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            entry = base / "usr/share/phpmyadmin/index.php"
            entry.parent.mkdir(parents=True)
            entry.write_text("<?php", encoding="utf-8")
            conf = base / "etc/phpmyadmin/conf.d/nativedev.php"
            conf.parent.mkdir(parents=True)
            runtime = base / "var/lib/nativedev/phpmyadmin"
            account = SimpleNamespace(pw_gid=1000)
            with patch.object(helper, "PHPMYADMIN_ENTRYPOINT", entry), \
                 patch.object(helper, "PHPMYADMIN_NATIVEDEV_CONFIG", conf), \
                 patch.object(helper, "PHPMYADMIN_RUNTIME_ROOT", runtime), \
                 patch.object(helper, "_database_username_for_uid", return_value="developer"), \
                 patch.object(helper.pwd, "getpwuid", return_value=account), \
                 patch.object(helper.os, "chown"), patch.object(helper.os, "chmod"):
                first = helper._execute_phpmyadmin_reconcile(1000)
                self.assertEqual(first.returncode, 0)
                first_text = conf.read_text(encoding="utf-8")
                second = helper._execute_phpmyadmin_reconcile(1000)
                self.assertEqual(second.returncode, 0)
                second_text = conf.read_text(encoding="utf-8")

            self.assertEqual(first_text, second_text)
            self.assertIn("$cfg['blowfish_secret']", first_text)
            self.assertIn(str(runtime / "1000/tmp"), first_text)
            self.assertIn("$cfg['PmaNoRelation_DisableWarning'] = true;", first_text)
            self.assertTrue((runtime / "1000/tmp").is_dir())

    def test_reconcile_rpc_is_phpmyadmin_only_and_rejects_paths(self):
        from nativedev.privileged_helper import validate_operation
        from unittest.mock import patch
        with patch("nativedev.privileged_helper._database_username_for_uid", return_value="developer"):
            self.assertTrue(validate_operation({
                "protocol": 23, "action": "developer_tool.reconcile", "tool": "phpmyadmin"
            })[0])
            self.assertFalse(validate_operation({
                "protocol": 23, "action": "developer_tool.reconcile", "tool": "adminer"
            })[0])
            self.assertFalse(validate_operation({
                "protocol": 23, "action": "developer_tool.reconcile", "tool": "phpmyadmin",
                "path": "/tmp/evil",
            })[0])

    def test_adminer_sqlite_rpc_is_fixed_semantic_and_rejects_paths(self):
        from nativedev.privileged_helper import validate_operation

        self.assertTrue(validate_operation({
            "protocol": 23, "action": "developer_tool.adminer_sqlite.install"
        })[0])
        self.assertTrue(validate_operation({
            "protocol": 23, "action": "developer_tool.adminer_sqlite.uninstall"
        })[0])
        self.assertFalse(validate_operation({
            "protocol": 23, "action": "developer_tool.adminer_sqlite.install",
            "path": "/tmp/wrapper.php",
        })[0])
        self.assertFalse(validate_operation({
            "protocol": 23, "action": "developer_tool.adminer_sqlite.install",
            "password": "changed",
        })[0])

    def test_adminer_sqlite_wrapper_supports_debian_12_and_13_adminer_majors(self):
        import nativedev.privileged_helper as helper

        legacy = helper._adminer_sqlite_wrapper(4).decode("utf-8")
        self.assertIn("NativeDev Adminer major: 4", legacy)
        self.assertIn("plugins/plugin.php", legacy)
        self.assertIn("new AdminerPlugin", legacy)
        self.assertIn("new AdminerLoginPasswordLess", legacy)
        self.assertNotIn("nativedev'", legacy)

        current = helper._adminer_sqlite_wrapper(5).decode("utf-8")
        self.assertIn("NativeDev Adminer major: 5", current)
        self.assertNotIn("plugins/plugin.php", current)
        self.assertIn("new Adminer\\Plugins", current)
        self.assertIn("new AdminerLoginPasswordLess", current)
        self.assertNotIn("nativedev'", current)

        self.assertEqual(helper._adminer_major("4.8.1-1"), 4)
        self.assertEqual(helper._adminer_major("5.2.1+dfsg-1"), 5)
        with self.assertRaisesRegex(RuntimeError, "not supported"):
            helper._adminer_major("6.0.0-1")

    def test_adminer_sqlite_install_writes_managed_wrapper_without_php_extension_mutation(self):
        import subprocess
        from unittest.mock import patch
        import nativedev.privileged_helper as helper

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            adminer = root / "usr/share/adminer/adminer.php"
            plugins = root / "usr/share/adminer/plugins"
            wrapper_root = root / "usr/lib/nativedev/adminer-sqlite"
            wrapper = wrapper_root / "index.php"
            adminer.parent.mkdir(parents=True)
            plugins.mkdir(parents=True)
            adminer.write_text("<?php", encoding="utf-8")
            (plugins / "login-password-less.php").write_text("<?php", encoding="utf-8")
            (plugins / "plugin.php").write_text("<?php", encoding="utf-8")
            dpkg = subprocess.CompletedProcess([], 0, "4.8.1-1", "")
            with patch.object(helper, "ADMINER_ENTRYPOINT", adminer), \
                 patch.object(helper, "ADMINER_PLUGIN_ROOT", plugins), \
                 patch.object(helper, "ADMINER_SQLITE_ROOT", wrapper_root), \
                 patch.object(helper, "ADMINER_SQLITE_ENTRYPOINT", wrapper), \
                 patch.object(helper.subprocess, "run", return_value=dpkg) as run:
                result = helper._execute_adminer_sqlite_install()

            self.assertEqual(result.returncode, 0)
            text = wrapper.read_text(encoding="utf-8")
            self.assertIn(helper.ADMINER_SQLITE_MARKER, text)
            self.assertIn("NativeDev Adminer major: 4", text)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][-1], "adminer")
            self.assertNotIn("sqlite3", " ".join(run.call_args.args[0]))

    def test_adminer_sqlite_state_requires_adminer_and_inherits_adminer_php(self):
        from unittest.mock import patch
        from nativedev.managers.developer_tools import DeveloperToolManager
        from nativedev.system import CommandResult
        import nativedev.managers.developer_tools as module

        class Config:
            developer_tools = {"adminer": {"php": "8.3"}}
            def save(self): pass

        class Apt:
            def __init__(self, installed): self.installed = installed
            def is_installed(self, package): return package in self.installed
            def candidate(self, package): return "1.0"

        class Php:
            def installed_fpm_versions(self): return ["8.4", "8.3"]
            def default_fpm_version(self): return "8.4"
            def fpm_config_ready(self, version): return True

        class Runner:
            def run(self, argv, **_kwargs):
                return CommandResult(list(argv), 0, "5.2.1+dfsg-1", "")
            def privileged_operation(self, *_args, **_kwargs):
                return CommandResult([], 0, "", "")

        with tempfile.TemporaryDirectory() as td:
            wrapper = Path(td) / "index.php"
            with patch.object(module, "ADMINER_SQLITE_ENTRYPOINT", wrapper):
                manager = DeveloperToolManager(Runner(), Apt(set()), Php(), Config())
                state = manager.adminer_sqlite_state()
                self.assertFalse(state.installable)
                self.assertFalse(state.adminer_installed)
                self.assertEqual(state.url, "http://adminer-sqlite.localhost")
                self.assertEqual(state.password, "nativedev")

                manager = DeveloperToolManager(Runner(), Apt({"adminer"}), Php(), Config())
                state = manager.adminer_sqlite_state()
                self.assertTrue(state.installable)
                self.assertEqual(state.php_version, "8.3")

                wrapper.write_text(
                    module.ADMINER_SQLITE_MARKER + "\n// NativeDev Adminer major: 5\n",
                    encoding="utf-8",
                )
                state = manager.adminer_sqlite_state()
                self.assertTrue(state.installed)
                from nativedev.managers import developer_tools as developer_tools_module
                developer_tools_module.DEVELOPER_TOOLS_NGINX.parent.mkdir(parents=True, exist_ok=True)
                developer_tools_module.DEVELOPER_TOOLS_NGINX.write_text(
                    developer_tools_module.DEVELOPER_TOOLS_NGINX_MARKER
                    + "\nserver_name adminer-sqlite.localhost;\n"
                    + 'fastcgi_pass "unix:/run/php/php8.3-fpm-nativedev-1000.sock";\n',
                    encoding="utf-8",
                )
                state = manager.adminer_sqlite_state()
                self.assertTrue(state.runtime_ready)

    def test_nginx_adminer_sqlite_is_fixed_localhost_and_inherits_adminer_php(self):
        from unittest.mock import patch
        from nativedev.config import AppConfig
        import nativedev.managers.developer_tools as developer_tools_module
        from nativedev.managers.developer_tools import DeveloperToolManager

        class Apt:
            def is_installed(self, package):
                return package == "adminer"

        with tempfile.TemporaryDirectory() as td:
            wrapper = Path(td) / "adminer-sqlite-index.php"
            wrapper.write_text("// Managed by NativeDev: Adminer SQLite\n", encoding="utf-8")
            config = AppConfig(
                park_dir=td,
                domain="dev",
                developer_tools={"adminer": {"php": "8.3"}},
            )
            with patch.object(developer_tools_module, "ADMINER_SQLITE_ENTRYPOINT", wrapper):
                manager = DeveloperToolManager(None, Apt(), StubPhp(default="8.4", installed=["8.4", "8.3"]), config)
                rendered = manager.render_nginx()

            self.assertIn("server_name adminer-sqlite.localhost;", rendered)
            sqlite_start = rendered.index("server_name adminer-sqlite.localhost;")
            sqlite_end = rendered.find("server {", sqlite_start)
            sqlite_block = rendered[sqlite_start: sqlite_end if sqlite_end != -1 else len(rendered)]
            self.assertIn(str(wrapper), sqlite_block)
            self.assertIn('fastcgi_pass "unix:/run/php/php8.3-fpm-nativedev-1000.sock";', sqlite_block)
            self.assertIn("allow 127.0.0.1;", sqlite_block)
            self.assertIn("allow ::1;", sqlite_block)
            self.assertIn("deny all;", sqlite_block)
            self.assertNotIn("listen 443", sqlite_block)
            self.assertNotIn("adminer-sqlite.dev", rendered)

    def test_adminer_sqlite_ui_is_optional_child_of_adminer_with_fixed_password(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        self.assertIn('copy.append(label("Adminer SQLite", "section-title"))', gui)
        self.assertIn('copy.append(label(f"Password: {state.password}", "muted"))', gui)
        self.assertIn('"Requires Adminer. Install Adminer first."', gui)
        self.assertIn('self.context.controller.install_adminer_sqlite', gui)
        self.assertIn('self.context.controller.uninstall_adminer_sqlite', gui)
        self.assertNotIn('install_extension("sqlite3")', gui)

    def test_php_uninstall_is_blocked_while_developer_tool_uses_version(self):
        from nativedev.controller import NativeDevController

        class Php:
            def uninstall_version(self, version):
                raise AssertionError("PHP uninstall must not start")

        class LocalDev:
            def nginx_managed(self):
                return False

        class DeveloperTools:
            def tools_using_php(self, version):
                return ["phpMyAdmin"] if version == "8.4" else []

        controller = NativeDevController(Php(), LocalDev(), developer_tools=DeveloperTools())
        with self.assertRaisesRegex(RuntimeError, "phpMyAdmin"):
            controller.uninstall_php("8.4")

    def test_services_ui_groups_developer_tools_and_puts_php_selector_before_uninstall(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        self.assertIn('self._section_panel(\n                "SYSTEM SERVICES"', gui)
        self.assertIn('self._section_panel(\n                "SYSTEM TOOLS"', gui)
        self.assertIn('self._section_panel(\n                "DEVELOPER TOOLS"', gui)
        self.assertIn('section.add_css_class("services-section")', gui)
        self.assertIn('Generate *.{self.context.config.domain} certificate', gui)
        self.assertIn('button {\n  min-height: 15px;', (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "style.css").read_text())
        developer_card = gui[gui.index("def _developer_web_tool_card"):gui.index("def _use_default_database_user")]
        self.assertLess(developer_card.index("actions.append(php_dropdown)"), developer_card.index("actions.append(uninstall)"))


class DeveloperToolManagerExportTests(unittest.TestCase):
    def test_developer_tool_manager_is_exported_from_managers_package(self):
        from nativedev.managers import DeveloperToolManager
        from nativedev.managers.developer_tools import DeveloperToolManager as DirectDeveloperToolManager

        self.assertIs(DeveloperToolManager, DirectDeveloperToolManager)


class ApplicationUpdateManagerTests(unittest.TestCase):
    class Runner:
        def __init__(self, *, installed="0.2.0"):
            self.installed = installed
            self.commands = []
            self.privileged = []

        def run(self, argv, **_kwargs):
            from nativedev.system import CommandResult

            argv = list(argv)
            self.commands.append(argv)
            if argv[:2] == ["dpkg-query", "-W"]:
                if self.installed is None:
                    return CommandResult(argv, 1, "", "not installed")
                return CommandResult(argv, 0, f"ii \t{self.installed}", "")
            if argv[:2] == ["dpkg", "--compare-versions"]:
                def key(value):
                    return tuple(int(part) for part in value.split("."))
                return CommandResult(argv, 0 if key(argv[2]) > key(argv[4]) else 1, "", "")
            return CommandResult(argv, 1, "", "unexpected")

        def privileged_operation(self, action, **kwargs):
            from nativedev.system import CommandResult

            self.privileged.append((action, kwargs))
            return CommandResult([f"nativedev:{action}"], 0, "updated", "")

    class ReleaseSource:
        name = "github"

        def __init__(self, version="0.2.1"):
            self.version = version
            self.calls = []

        def latest(self, asset_name):
            from nativedev.managers.application import GitHubRelease

            self.calls.append(asset_name)
            if self.version is None:
                return None
            version = self.version
            return GitHubRelease(
                version=version,
                tag_name=f"v{version}",
                asset_name=f"nativedev_{version}_all.deb",
                asset_url=f"https://github.com/sayedsahin/nativedev/releases/download/v{version}/nativedev_{version}_all.deb",
                sha256="a" * 64,
            )

    @staticmethod
    def distro():
        from nativedev.system import DistroInfo

        return DistroInfo("debian", "Debian", "13", "trixie", (), "Debian 13")

    def test_background_update_check_is_due_only_every_24_hours(self):
        from nativedev.managers.application import ApplicationManager, UPDATE_CHECK_INTERVAL_SECONDS

        with tempfile.TemporaryDirectory() as td:
            now = [1_000_000.0]
            source = self.ReleaseSource()
            manager = ApplicationManager(
                self.Runner(), self.distro(), state_file=Path(td) / "update.json",
                clock=lambda: now[0], release_source=source,
            )
            self.assertTrue(manager.check_due())
            update = manager.check_for_update()
            self.assertEqual(update.candidate_version, "0.2.1")
            self.assertEqual(source.calls, ["nativedev_{version}_all.deb"])
            self.assertFalse(manager.check_due())
            now[0] += UPDATE_CHECK_INTERVAL_SECONDS - 1
            self.assertFalse(manager.check_due())
            now[0] += 1
            self.assertTrue(manager.check_due())

    def test_update_check_uses_github_release_not_apt_candidate_metadata(self):
        from nativedev.managers.application import ApplicationManager

        with tempfile.TemporaryDirectory() as td:
            runner = self.Runner()
            manager = ApplicationManager(
                runner, self.distro(), state_file=Path(td) / "update.json",
                release_source=self.ReleaseSource(),
            )
            manager.check_for_update(force=True)
            self.assertEqual(runner.privileged, [])
            self.assertFalse(any(command[:2] == ["apt-cache", "policy"] for command in runner.commands))
            self.assertFalse(any(command[:2] == ["apt-get", "update"] for command in runner.commands))

    def test_update_action_is_fixed_semantic_rpc(self):
        from nativedev.managers.application import ApplicationManager

        with tempfile.TemporaryDirectory() as td:
            runner = self.Runner()
            manager = ApplicationManager(
                runner, self.distro(), state_file=Path(td) / "update.json",
                release_source=self.ReleaseSource(),
            )
            manager.update()
            self.assertEqual(runner.privileged[0][0], "application.update")
            self.assertNotIn("package", runner.privileged[0][1])
            self.assertNotIn("url", runner.privileged[0][1])
            self.assertIsNone(runner.privileged[0][1]["timeout"])

    def test_source_copy_install_does_not_offer_self_update(self):
        from nativedev.managers.application import ApplicationManager

        with tempfile.TemporaryDirectory() as td:
            manager = ApplicationManager(
                self.Runner(installed=None), self.distro(), state_file=Path(td) / "update.json",
                release_source=self.ReleaseSource(),
            )
            self.assertIsNone(manager.check_for_update(force=True))
            with self.assertRaisesRegex(RuntimeError, "native system package"):
                manager.update()

    def test_application_manager_export(self):
        from nativedev.managers import ApplicationManager
        from nativedev.managers.application import ApplicationManager as DirectApplicationManager

        self.assertIs(ApplicationManager, DirectApplicationManager)


class GitHubReleaseSourceTests(unittest.TestCase):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return self.payload

    def test_latest_release_requires_fixed_deb_and_github_digest(self):
        from nativedev.managers.application import GitHubReleaseSource

        payload = json.dumps({
            "tag_name": "v0.2.1",
            "draft": False,
            "prerelease": False,
            "assets": [{
                "name": "nativedev_0.2.1_all.deb",
                "state": "uploaded",
                "browser_download_url": "https://github.com/sayedsahin/nativedev/releases/download/v0.2.1/nativedev_0.2.1_all.deb",
                "digest": "sha256:" + "b" * 64,
            }],
        }).encode()

        source = GitHubReleaseSource(opener=lambda *_args, **_kwargs: self.Response(payload))
        release = source.latest("nativedev_{version}_all.deb")
        self.assertEqual(release.version, "0.2.1")
        self.assertEqual(release.sha256, "b" * 64)

    def test_latest_release_rejects_missing_digest(self):
        from nativedev.managers.application import GitHubReleaseSource

        payload = json.dumps({
            "tag_name": "v0.2.1",
            "draft": False,
            "prerelease": False,
            "assets": [{
                "name": "nativedev_0.2.1_all.deb",
                "state": "uploaded",
                "browser_download_url": "https://github.com/sayedsahin/nativedev/releases/download/v0.2.1/nativedev_0.2.1_all.deb",
            }],
        }).encode()
        source = GitHubReleaseSource(opener=lambda *_args, **_kwargs: self.Response(payload))
        with self.assertRaisesRegex(RuntimeError, "SHA-256 digest"):
            source.latest("nativedev_{version}_all.deb")


class ApplicationPackageSecurityTests(unittest.TestCase):
    def test_application_update_rpc_has_no_client_controlled_package_repository_or_url(self):
        from nativedev.privileged_helper import validate_operation

        self.assertTrue(validate_operation({"protocol": 23, "action": "application.update"})[0])
        self.assertFalse(validate_operation({
            "protocol": 23,
            "action": "application.update",
            "package": "openssh-server",
        })[0])
        self.assertFalse(validate_operation({
            "protocol": 23,
            "action": "application.update",
            "repository": "https://example.invalid/",
        })[0])
        self.assertFalse(validate_operation({
            "protocol": 23,
            "action": "application.update",
            "url": "https://example.invalid/evil.deb",
        })[0])

    def test_root_side_update_is_fixed_github_native_package_only(self):
        helper = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "privileged_helper.py").read_text()
        start = helper.index("def _github_latest_native_release")
        end = helper.index("def execute_operation", start)
        implementation = helper[start:end]
        self.assertIn('GITHUB_REPOSITORY = "nativedev"', helper)
        self.assertIn('GITHUB_OWNER = "sayedsahin"', helper)
        self.assertIn('asset_name = f"nativedev_{version}_all.deb"', implementation)
        self.assertIn('"digest"', implementation)
        self.assertIn('hashlib.sha256()', implementation)
        self.assertIn('"Package"', implementation)
        self.assertIn('"Version"', implementation)
        self.assertIn('"Architecture"', implementation)
        self.assertIn('"DPkg::Lock::Timeout=0"', implementation)
        self.assertNotIn('request.get("package")', implementation)
        self.assertNotIn('request.get("url")', implementation)


class NativePackageWorkflowTests(unittest.TestCase):
    def test_deb_build_is_architecture_independent_and_installs_root_owned_helper(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "packaging" / "build-deb.sh").read_text()
        self.assertIn("Architecture: all", script)
        self.assertIn("/usr/lib/nativedev/privileged_helper.py", script)
        self.assertIn("packaging/debian/prerm", script)
        self.assertIn("dpkg-deb --root-owner-group --build", script)
        self.assertNotIn("NATIVEDEV_APT_REPO_KEYRING", script)
        self.assertIn('OUTPUT="$OUTPUT_DIR/nativedev_${VERSION}_all.deb"', script)

    def test_install_script_uses_native_deb_not_user_source_copy(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "install.sh").read_text()
        self.assertIn("packaging/build-deb.sh", script)
        self.assertIn("apt-get install --reinstall -y", script)
        self.assertNotIn('cp -a "$ROOT/src" "$TARGET/src"', script)

    def test_uninstall_uses_package_manager_and_preserves_development_state(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "uninstall.sh").read_text()
        self.assertIn("apt-get remove -y nativedev", script)
        self.assertNotIn("/var/lib/mysql", script)
        self.assertNotIn("/var/lib/postgresql", script)
        self.assertIn("intentionally preserved", script)

    def test_github_release_workflow_builds_deb_before_publishing_release(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "release.yml").read_text()
        self.assertIn("tags:", workflow)
        self.assertIn("'v*.*.*'", workflow)
        self.assertIn("packaging/build-deb.sh --output-dir dist", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("nativedev_*_all.deb", workflow)
        self.assertLess(workflow.index("packaging/build-deb.sh"), workflow.index("gh release create"))

    def test_gui_starts_background_update_check_and_shows_dialog(self):
        gui = (Path(__file__).resolve().parents[1] / "src" / "nativedev" / "gui.py").read_text()
        self.assertIn("self.window.check_for_updates()", gui)
        self.assertIn("GLib.timeout_add_seconds(60 * 60, self._update_timer_tick)", gui)
        self.assertIn('title="NativeDev update available"', gui)
        self.assertIn('dialog.add_button("Later"', gui)
        self.assertIn('dialog.add_button("Update"', gui)
        self.assertIn('dialog.add_button("Restart NativeDev"', gui)
