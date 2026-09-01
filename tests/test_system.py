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
    def test_project_public_directory_and_default_php_are_used(self):
        from nativedev.config import AppConfig
        from nativedev.managers.localdev import LocalDevManager

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "shop" / "public").mkdir(parents=True)
            config = AppConfig(park_dir=str(root), domain="test")
            manager = LocalDevManager(None, None, None, config, StubPhp())  # render path is pure
            rendered = manager.render_nginx()
            self.assertIn("server_name shop.test;", rendered)
            self.assertIn(str(root / "shop" / "public"), rendered)
            self.assertIn("php8.4-fpm-nativedev-1000.sock", rendered)
            self.assertNotIn("fastcgi_pass unix:/run/php/php8.4-fpm.sock", rendered)

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
            self.assertIn("php8.3-fpm-nativedev-1000.sock", rendered)

    def test_new_project_defaults_to_default_php(self):
        from nativedev.config import AppConfig
        from nativedev.managers.localdev import LocalDevManager

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "app"
            project.mkdir()
            config = AppConfig(park_dir=str(root), domain="test")
            manager = LocalDevManager(None, None, None, config, StubPhp())
            prefs = manager.project_preferences(project)
            self.assertEqual(prefs["php"], "default")

    def test_project_missing_from_park_dir_is_skipped_not_fatal(self):
        from nativedev.config import AppConfig
        from nativedev.managers.localdev import LocalDevManager

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app").mkdir()
            config = AppConfig(park_dir=str(root), domain="test")
            manager = LocalDevManager(None, None, None, config, StubPhp(default="", installed=[]))
            # No PHP-FPM installed at all: rendering must not raise, it should
            # simply produce a site file with no server blocks for now.
            rendered = manager.render_nginx()
            self.assertNotIn("server_name app.test;", rendered)


class HttpsKeyPermissionTests(unittest.TestCase):
    def test_https_key_is_installed_readable_by_nginx(self):
        # nativedev-key.pem is written by the privileged helper as root:root.
        # Nginx's worker process runs as www-data, not root, so the key must
        # be installed with a mode that lets a non-root process read it, or
        # every HTTPS-enabled site breaks with "nginx -t" failing to load the
        # certificate key.
        localdev = (
            Path(__file__).resolve().parents[1] / "src" / "nativedev" / "managers" / "localdev.py"
        ).read_text()
        key_install_line = next(
            line for line in localdev.splitlines() if "nativedev-key.pem" in line and "install" in line
        )
        self.assertIn('"-m", "0644"', key_install_line)
        self.assertNotIn('"-m", "0600"', key_install_line)


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
    def test_allows_native_service_operations(self):
        from nativedev.privileged_helper import validate_command
        self.assertTrue(validate_command(["systemctl", "restart", "nginx"])[0])
        self.assertTrue(validate_command(["systemctl", "disable", "--now", "php8.4-fpm"])[0])
        self.assertTrue(validate_command(["apt-get", "install", "-y", "redis-tools"])[0])
        self.assertTrue(validate_command(["update-alternatives", "--set", "php", "/usr/bin/php8.4"])[0])
        self.assertTrue(validate_command(["php-fpm8.4", "-tt"])[0])
        self.assertTrue(validate_command(["install", "-m", "0644", "/tmp/nativedev-fpm-test/pool.conf", "/etc/php/8.4/fpm/pool.d/nativedev-1000.conf"], uid=1000)[0])
        self.assertTrue(validate_command(["rm", "-f", "/etc/php/8.4/fpm/pool.d/nativedev-1000.conf"], uid=1000)[0])
        self.assertFalse(validate_command(["rm", "-f", "/etc/php/8.4/fpm/pool.d/nativedev-1001.conf"], uid=1000)[0])

    def test_rejects_arbitrary_root_commands(self):
        from nativedev.privileged_helper import validate_command
        self.assertFalse(validate_command(["rm", "-rf", "/"])[0])
        self.assertFalse(validate_command(["bash", "-c", "id"])[0])
        self.assertFalse(validate_command(["systemctl", "restart", "ssh"])[0])


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

if __name__ == "__main__":
    unittest.main()
