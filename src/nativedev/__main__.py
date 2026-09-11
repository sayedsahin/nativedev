from __future__ import annotations

import os
import sys


# GUI-launched desktop sessions do not reliably inherit the sbin directories
# used by Debian for administrative executables such as nginx.  Keep the
# existing environment intact, but make the standard sbin locations available
# before importing NativeDev modules that may use shutil.which().
_SBIN_DIRS = ("/usr/local/sbin", "/usr/sbin", "/sbin")


def _ensure_sbin_on_path() -> None:
    """Ensure standard system sbin directories are present in this process PATH."""
    current = os.environ.get("PATH", "")
    parts = [part for part in current.split(os.pathsep) if part]

    # Prepend only missing, fixed system directories.  This lets NativeDev find
    # Debian's system nginx from a desktop launcher without repeatedly adding
    # entries if main() is invoked more than once in tests or embedding code.
    missing = [directory for directory in _SBIN_DIRS if directory not in parts]
    if not missing:
        return

    os.environ["PATH"] = os.pathsep.join([*missing, *parts])


def main() -> int:
    # Normalize PATH before importing application modules.  Some manager code
    # uses shutil.which() for nginx/tool detection, so importing those modules
    # only after this point guarantees a consistent desktop-launch environment.
    _ensure_sbin_on_path()

    from . import gui
    from .dashboard_conflicts import install_dashboard_conflicts
    from .php_uninstall_ui import install_php_uninstall_confirmation
    from .provider_uninstall_ui import install_provider_uninstall_ui

    # Small runtime integrations keep the large GTK module unchanged while
    # preserving the existing PHP uninstall and Dashboard conflict workflows.
    install_php_uninstall_confirmation(gui)
    install_dashboard_conflicts(gui)
    install_provider_uninstall_ui(gui)

    app = gui.NativeDevApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
