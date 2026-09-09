from __future__ import annotations

from types import ModuleType


def install_provider_uninstall_ui(gui_module: ModuleType) -> None:
    """Add guarded Sury/Ondřej and NVM uninstall actions to provider cards."""

    Gtk = gui_module.Gtk
    label = gui_module.label
    confirm = gui_module.confirm

    def close_only(parent, title: str, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=parent,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
            secondary_text=message,
        )
        dialog.connect("response", lambda dlg, _response: dlg.destroy())
        dialog.present()

    base_php = gui_module.PhpPage
    if not getattr(base_php, "_nativedev_provider_uninstall", False):

        class PhpPageWithProviderUninstall(base_php):
            _nativedev_provider_uninstall = True

            def _build_repo(self, data):
                super()._build_repo(data)
                if data.get("provider") != "multi":
                    return

                button = Gtk.Button(label="Uninstall repository")
                # Keep the provider action at its natural text width instead of
                # stretching across the vertical provider card.
                button.set_halign(Gtk.Align.END)

                installed = tuple(data.get("installed") or ())
                repo_name = data.get("multi_name") or "Sury/Ondřej"

                def clicked(*_args):
                    if installed:
                        versions = "\n".join(
                            f"• PHP {version}" for version in installed
                        )
                        close_only(
                            self.window,
                            "Cannot uninstall PHP repository",
                            (
                                f"Installed PHP versions:\n{versions}\n\n"
                                "Uninstall all PHP versions before removing the "
                                f"{repo_name} multi-PHP repository."
                            ),
                        )
                        return

                    confirm(
                        self.window,
                        "Uninstall PHP repository?",
                        (
                            f"NativeDev will remove {repo_name} and refresh APT. "
                            "No PHP runtime is installed, so no PHP package will "
                            "be removed by this action."
                        ),
                        lambda: self.action(
                            button,
                            self.context.controller.uninstall_multi_php_repository,
                            success_message="PHP repository removed",
                            after=self.refresh,
                        ),
                    )

                button.connect("clicked", clicked)
                self.repo_card.append(button)

        gui_module.PhpPage = PhpPageWithProviderUninstall

    base_node = gui_module.NodePage
    if not getattr(base_node, "_nativedev_provider_uninstall", False):

        class NodePageWithProviderUninstall(base_node):
            _nativedev_provider_uninstall = True

            def _build_provider(self, data):
                super()._build_provider(data)
                if data.get("provider") != "nvm":
                    return

                button = Gtk.Button(label="Uninstall NVM")

                installed = tuple(data.get("versions") or ())

                def clicked(*_args):
                    if installed:
                        versions = "\n".join(
                            f"• {version.removeprefix('v')}"
                            for version in installed
                        )
                        close_only(
                            self.window,
                            "Cannot uninstall NVM",
                            (
                                f"Installed NVM Node versions:\n{versions}\n\n"
                                "Uninstall all NVM-managed Node.js versions first."
                            ),
                        )
                        return

                    confirm(
                        self.window,
                        "Uninstall NVM?",
                        (
                            "NativeDev will remove the NVM framework and only "
                            "NativeDev-owned NVM marker blocks from your shell "
                            "configuration. Other .bashrc/.zshrc content is not changed."
                        ),
                        lambda: self.action(
                            button,
                            self.context.controller.uninstall_nvm,
                            success_message="NVM uninstalled",
                            after=self.refresh,
                        ),
                    )

                button.connect("clicked", clicked)

                # Base NodePage already renders a horizontal provider-action
                # box containing "Configure shell" (and, when applicable, the
                # migration cleanup action). Reuse that same row so Uninstall
                # NVM sits beside Configure shell on the right rather than
                # becoming a separate full-width card child.
                actions = None
                child = self.nvm_card.get_first_child()
                while child is not None:
                    if isinstance(child, Gtk.Box):
                        nested = child.get_first_child()
                        while nested is not None:
                            if (
                                isinstance(nested, Gtk.Button)
                                and nested.get_label() == "Configure shell"
                            ):
                                actions = child
                                break
                            nested = nested.get_next_sibling()
                    if actions is not None:
                        break
                    child = child.get_next_sibling()

                if actions is None:
                    # Defensive fallback for future GUI refactors. Keep natural
                    # button width and right alignment even if the base action
                    # row can no longer be located.
                    actions = Gtk.Box(
                        orientation=Gtk.Orientation.HORIZONTAL,
                        spacing=8,
                    )
                    actions.append(button)
                    actions.set_halign(Gtk.Align.END)
                    self.nvm_card.append(actions)
                else:
                    actions.append(button)
                    actions.set_halign(Gtk.Align.END)

        gui_module.NodePage = NodePageWithProviderUninstall

    # MainWindow.PAGES caches class objects at gui.py import time. Update the
    # cached registry so the subclasses above are actually instantiated.
    replacement = {
        "php": gui_module.PhpPage,
        "node": gui_module.NodePage,
    }
    gui_module.MainWindow.PAGES = tuple(
        (key, title, replacement.get(key, page_class))
        for key, title, page_class in gui_module.MainWindow.PAGES
    )
