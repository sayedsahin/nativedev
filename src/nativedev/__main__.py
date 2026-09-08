from __future__ import annotations

import sys

from . import gui
from .dashboard_conflicts import install_dashboard_conflicts
from .php_uninstall_ui import install_php_uninstall_confirmation


def main() -> int:
    # Keep the large GUI module unchanged. Small feature integrations replace
    # only the relevant runtime classes/callbacks before the application builds
    # its MainWindow.
    install_php_uninstall_confirmation(gui)
    install_dashboard_conflicts(gui)
    app = gui.NativeDevApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
