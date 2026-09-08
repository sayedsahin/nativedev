from __future__ import annotations

import unittest
from unittest.mock import patch

from nativedev.managers.developer_tools import DeveloperToolSpec
from nativedev.php_uninstall import (
    build_php_uninstall_preview,
    uninstall_php_version,
)


class FakePhp:
    def __init__(self, versions, default):
        self.versions = list(versions)
        self.default = default
        self.prepared = []
        self.uninstalled = []

    @staticmethod
    def _version_key(version):
        major, minor = version.split(".", 1)
        return int(major), int(minor)

    def installed_fpm_versions(self):
        return list(self.versions)

    def cli_version(self):
        return self.default

    def fpm_config_ready(self, version):
        return version in self.versions

    def ensure_developer_pool(self, version):
        self.prepared.append(version)

    def installed_version_packages(self, version):
        return [f"php{version}-cli", f"php{version}-fpm"]

    def uninstall_version(self, version):
        self.uninstalled.append(version)
        self.versions = [item for item in self.versions if item != version]


class FakeApt:
    def __init__(self, installed):
        self.installed = set(installed)
        self.installs = []

    def is_installed(self, package):
        return package in self.installed

    def candidate(self, package):
        return "1" if package.startswith("php") else None

    def install(self, packages):
        packages = list(packages)
        self.installs.append(packages)
        self.installed.update(packages)


class FakeResult:
    def __init__(self, stdout="", ok=True):
        self.stdout = stdout
        self.stderr = ""
        self.ok = ok

    @property
    def output(self):
        return self.stdout or self.stderr


class FakeRunner:
    def __init__(self, removal_output=""):
        self.removal_output = removal_output

    def run(self, argv, timeout=None):
        if argv[:2] == ["apt-cache", "depends"]:
            return FakeResult(
                "  Depends: php-common\n"
                "  Depends: php-mysql\n"
                "  Recommends: php-mbstring\n"
            )
        if argv[:3] == ["apt-get", "-s", "remove"]:
            return FakeResult(self.removal_output)
        return FakeResult()


class FakeDeveloperTools:
    def __init__(self, php, removal_output=""):
        self.php = php
        self.apt = FakeApt(
            {
                "adminer",
                "php8.5-cli",
                "php8.5-fpm",
                "php8.5-common",
                "php8.4-cli",
                "php8.4-fpm",
                "php8.4-common",
            }
        )
        self.runner = FakeRunner(removal_output)
        self.selected = {"adminer": "8.5"}
        self.nginx_reconciles = 0

    def effective_php(self, key):
        selected = self.selected.get(key, "")
        if selected in self.php.installed_fpm_versions():
            return selected
        versions = self.php.installed_fpm_versions()
        default = self.php.cli_version()
        return default if default in versions else (versions[0] if versions else "")

    def selected_php(self, key):
        return self.selected.get(key, "")

    def set_selected_php(self, key, version):
        self.selected[key] = version

    def clear_selected_php(self, key):
        self.selected.pop(key, None)

    def configure_nginx(self):
        self.nginx_reconciles += 1


ADMINER = DeveloperToolSpec(
    "adminer",
    "Adminer",
    "adminer",
    __import__("pathlib").Path("/usr/share/adminer"),
    "adminer.php",
    "Database administration",
)


class PhpUninstallWorkflowTests(unittest.TestCase):
    def test_non_default_removal_uses_current_default_for_tools(self):
        php = FakePhp(["8.5", "8.4"], "8.4")
        tools = FakeDeveloperTools(php)

        preview = build_php_uninstall_preview(php, tools, "8.5")

        self.assertEqual(preview.replacement, "8.4")
        self.assertFalse(preview.removing_default)

        with patch("nativedev.php_uninstall.DEVELOPER_WEB_TOOLS", (ADMINER,)), patch(
            "nativedev.php_uninstall.DEVELOPER_TOOL_BY_KEY",
            {"adminer": ADMINER},
        ), patch("nativedev.php_uninstall.shutil.which", return_value="/usr/sbin/nginx"):
            uninstall_php_version(
                php=php,
                developer_tools=tools,
                php_ini=None,
                reconcile_nginx=lambda: None,
                version="8.5",
            )

        self.assertEqual(tools.selected["adminer"], "8.4")
        self.assertEqual(php.uninstalled, ["8.5"])
        self.assertNotIn("8.5", php.versions)

    def test_default_removal_uses_remaining_php_without_changing_default(self):
        php = FakePhp(["8.5", "8.4"], "8.5")
        tools = FakeDeveloperTools(php)

        with patch("nativedev.php_uninstall.DEVELOPER_WEB_TOOLS", (ADMINER,)):
            preview = build_php_uninstall_preview(php, tools, "8.5")

        self.assertTrue(preview.removing_default)
        self.assertEqual(preview.replacement, "8.4")

        with patch("nativedev.php_uninstall.DEVELOPER_WEB_TOOLS", (ADMINER,)), patch(
            "nativedev.php_uninstall.DEVELOPER_TOOL_BY_KEY",
            {"adminer": ADMINER},
        ), patch("nativedev.php_uninstall.shutil.which", return_value=None):
            uninstall_php_version(
                php=php,
                developer_tools=tools,
                php_ini=None,
                reconcile_nginx=lambda: None,
                version="8.5",
            )

        # The workflow never calls set_cli_default/update-alternatives.
        self.assertEqual(php.default, "8.5")
        self.assertEqual(tools.selected["adminer"], "8.4")

    def test_last_php_requires_explicit_tool_removal_confirmation(self):
        php = FakePhp(["8.5"], "8.5")
        tools = FakeDeveloperTools(php)

        with patch("nativedev.php_uninstall.DEVELOPER_WEB_TOOLS", (ADMINER,)):
            with self.assertRaisesRegex(RuntimeError, "Explicit confirmation"):
                uninstall_php_version(
                    php=php,
                    developer_tools=tools,
                    php_ini=None,
                    reconcile_nginx=lambda: None,
                    version="8.5",
                )

            uninstall_php_version(
                php=php,
                developer_tools=tools,
                php_ini=None,
                reconcile_nginx=lambda: None,
                version="8.5",
                confirmed_tool_removal=("adminer",),
            )

        self.assertEqual(php.uninstalled, ["8.5"])

    def test_apt_impact_blocks_and_rolls_back_tool_selection(self):
        php = FakePhp(["8.5", "8.4"], "8.4")
        tools = FakeDeveloperTools(php, removal_output="Remv adminer [4.8.1]\n")

        with patch("nativedev.php_uninstall.DEVELOPER_WEB_TOOLS", (ADMINER,)), patch(
            "nativedev.php_uninstall.DEVELOPER_TOOL_BY_KEY",
            {"adminer": ADMINER},
        ), patch("nativedev.php_uninstall.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "APT would also remove"):
                uninstall_php_version(
                    php=php,
                    developer_tools=tools,
                    php_ini=None,
                    reconcile_nginx=lambda: None,
                    version="8.5",
                )

        self.assertEqual(tools.selected["adminer"], "8.5")
        self.assertEqual(php.uninstalled, [])


if __name__ == "__main__":
    unittest.main()
