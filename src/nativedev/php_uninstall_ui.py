from __future__ import annotations

from functools import wraps
import re
from types import ModuleType
from typing import Callable

from .php_uninstall import PhpUninstallPreview


_PHP_UNINSTALL_TITLE_RE = re.compile(r"^Uninstall PHP (\d+\.\d+)\?$")


def _migration_message(preview: PhpUninstallPreview) -> str:
    tools = ", ".join(preview.tool_titles)

    if preview.removing_default:
        target = (
            f"{tools} currently use PHP {preview.version}, which is also the "
            "current system Default PHP. "
        )
        default_note = (
            f"NativeDev will move the affected Developer Tools to PHP "
            f"{preview.replacement}, but it will NOT set PHP "
            f"{preview.replacement} as the new system Default. The package/"
            "update-alternatives mechanism will decide the system Default after "
            f"PHP {preview.version} is removed. "
        )
    else:
        target = (
            f"{tools} currently use PHP {preview.version}. "
        )
        default_note = (
            f"NativeDev will move the affected Developer Tools to the current "
            f"Default PHP {preview.replacement}. "
        )

    return (
        target
        + default_note
        + "Before removal, NativeDev will prepare the replacement PHP and "
        "required tool PHP packages, regenerate Developer Tools Nginx routing, "
        "and run an APT removal simulation. If APT would still remove an "
        "affected Developer Tool, PHP uninstall will be blocked."
    )


def _last_php_message(preview: PhpUninstallPreview) -> str:
    tools = ", ".join(preview.tool_titles)
    return (
        f"{tools} currently use PHP {preview.version}, and no other PHP-FPM "
        "version is installed. Removing this PHP version may also cause APT to "
        f"remove these Developer Tools: {tools}. Continue only if you accept "
        "that they may also be removed."
    )


def install_php_uninstall_confirmation(gui_module: ModuleType) -> None:
    """Wrap NativeDev's existing confirmation dialog without editing gui.py."""

    original = gui_module.confirm
    if getattr(original, "_nativedev_php_uninstall_wrapper", False):
        return

    @wraps(original)
    def confirm(
        parent,
        title: str,
        message: str,
        on_accept: Callable[[], None],
    ):
        match = _PHP_UNINSTALL_TITLE_RE.fullmatch(title)
        if not match:
            return original(parent, title, message, on_accept)

        version = match.group(1)

        try:
            controller = parent.context.controller
            preview = controller.preview_php_uninstall(version)
        except Exception:
            # Confirmation rendering must never make an otherwise valid generic
            # uninstall button unusable. The controller still performs the
            # authoritative safety checks when the action runs.
            return original(parent, title, message, on_accept)

        if preview.tool_keys and preview.remaining_fpm:
            message = _migration_message(preview)

        elif preview.tool_keys:
            message = _last_php_message(preview)

            original_on_accept = on_accept

            def authorized_accept() -> None:
                controller.authorize_php_tool_removal(
                    version,
                    preview.tool_keys,
                )
                original_on_accept()

            on_accept = authorized_accept

        return original(parent, title, message, on_accept)

    confirm._nativedev_php_uninstall_wrapper = True
    gui_module.confirm = confirm
