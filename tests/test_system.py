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

class NginxRenderTests(unittest.TestCase):
    def test_project_public_directory_is_preferred(self):
        from nativedev.config import AppConfig
        from nativedev.managers.localdev import LocalDevManager

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "shop" / "public").mkdir(parents=True)
            config = AppConfig(park_dir=str(root), domain="test", php_version="8.4")
            manager = LocalDevManager(None, None, None, config)  # render path is pure
            rendered = manager.render_nginx()
            self.assertIn("server_name shop.test;", rendered)
            self.assertIn(str(root / "shop" / "public"), rendered)
            self.assertIn("php8.4-fpm.sock", rendered)


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

if __name__ == "__main__":
    unittest.main()
