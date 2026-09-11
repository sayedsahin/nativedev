from __future__ import annotations

import os
import sys

from . import gui
from .dashboard_conflicts import install_dashboard_conflicts
from .php_uninstall_ui import install_php_uninstall_confirmation
from .provider_uninstall_ui import install_provider_uninstall_ui

# Desktop-launched sessions (double-clicking the .desktop entry, an app
# launcher, etc.) do not always inherit the sbin directories that hold
# nginx, mysqld, and friends -- unlike a login shell, where distro profile
# scripts usually add them. When they're missing, every `shutil.which(...)`
# check throughout the app (nginx detection, status pills, "is X installed")
# silently reports the tool as absent even though it's installed, which is
# why this has been observed to vary between machines/desktop environments.
# Widen PATH once, here, before any manager code runs a which() check.
_SBIN_DIRS = ("/usr/local/sbin", "/usr/sbin", "/sbin")


def _ensure_sbin_on_path() -> None:
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    missing = [d for d in _SBIN_DIRS if d not in parts]
    if missing:
        os.environ["PATH"] = os.pathsep.join([*missing, *parts]) if parts else os.pathsep.join(missing)


def main() -> int:
    _ensure_sbin_on_path()
    # Small runtime integrations keep the large GTK module unchanged while
    # preserving the existing PHP uninstall and Dashboard conflict workflows.
    install_php_uninstall_confirmation(gui)
    install_dashboard_conflicts(gui)
    install_provider_uninstall_ui(gui)

    app = gui.NativeDevApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
