from __future__ import annotations

import sys

from . import gui
from .php_uninstall_ui import install_php_uninstall_confirmation


def main() -> int:
    # Keep the large GUI module unchanged. This wrapper replaces only the
    # existing PHP uninstall confirmation message when Developer Tools are
    # affected, and records explicit approval for the "last PHP" case.
    install_php_uninstall_confirmation(gui)
    app = gui.NativeDevApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
