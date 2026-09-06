from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRERM = ROOT / "packaging" / "debian" / "prerm"
POSTRM = ROOT / "packaging" / "debian" / "postrm"


class PackagingLifecycleTests(unittest.TestCase):
    def test_prerm_removes_only_generated_python_cache_from_package_tree(self):
        text = PRERM.read_text(encoding="utf-8")
        self.assertIn("/usr/lib/nativedev/app", text)
        self.assertIn("__pycache__", text)
        self.assertIn("*.pyc", text)
        self.assertIn("*.pyo", text)
        self.assertNotIn("rm -rf /usr/lib/nativedev", text)

    def test_prerm_localdev_cleanup_is_remove_only(self):
        text = PRERM.read_text(encoding="utf-8")
        self.assertIn("remove|deconfigure", text)
        self.assertIn("upgrade|failed-upgrade", text)
        self.assertIn("/etc/NetworkManager/conf.d/nativedev-dns.conf", text)
        self.assertIn("/etc/NetworkManager/dnsmasq.d/nativedev-test.conf", text)
        self.assertIn("/etc/nginx/sites-available/nativedev-sites.conf", text)
        self.assertIn("/etc/nginx/sites-enabled/nativedev-sites.conf", text)

        # Standalone tool/service integration must survive application removal.
        self.assertNotIn("nativedev-tools.conf", text)
        self.assertNotIn("adminer-sqlite", text)
        self.assertNotIn("phpmyadmin", text.lower())
        self.assertNotIn("mailpit", text.lower())
        self.assertNotIn("99-nativedev.ini", text)

    def test_postrm_purge_only_cleans_bytecode_and_empty_package_dirs(self):
        text = POSTRM.read_text(encoding="utf-8")
        self.assertIn('= "purge"', text)
        self.assertIn("__pycache__", text)
        self.assertIn("rmdir /usr/lib/nativedev/app/nativedev", text)
        self.assertNotIn("rm -rf /usr/lib/nativedev", text)
        self.assertNotIn("nativedev-tools.conf", text)
        self.assertNotIn("mailpit", text.lower())


if __name__ == "__main__":
    unittest.main()
