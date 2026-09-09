from __future__ import annotations

import sys

from . import gui
from .dashboard_conflicts import install_dashboard_conflicts
from .php_uninstall_ui import install_php_uninstall_confirmation
from .provider_uninstall_ui import install_provider_uninstall_ui


def main() -> int:
    # Small runtime integrations keep the large GTK module unchanged while
    # preserving the existing PHP uninstall and Dashboard conflict workflows.
    install_php_uninstall_confirmation(gui)
    install_dashboard_conflicts(gui)
    install_provider_uninstall_ui(gui)

    app = gui.NativeDevApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
