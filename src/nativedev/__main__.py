from __future__ import annotations

import sys

from .gui import NativeDevApplication


def main() -> int:
    app = NativeDevApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
