from __future__ import annotations

from types import ModuleType

from .port_conflicts import PortConflict, PortConflictManager


def install_dashboard_conflicts(gui_module: ModuleType) -> None:
    """Add the conditional Dashboard Conflict card without modifying gui.py."""

    base = gui_module.DashboardPage
    if getattr(base, "_nativedev_conflict_dashboard", False):
        return

    Gtk = gui_module.Gtk
    label = gui_module.label
    card = gui_module.card
    status_pill = gui_module.status_pill

    class DashboardPageWithConflicts(base):
        _nativedev_conflict_dashboard = True

        def __init__(self, window):
            self._conflict_ui_ready = False
            super().__init__(window)

            # DashboardPage currently appends System then Environment. Appending
            # here places Conflict directly below Environment, exactly where the
            # card belongs without touching Services & Tools.
            self.conflict_card = card()
            self.conflict_card.set_visible(False)
            self.body.append(self.conflict_card)
            self._conflict_manager = PortConflictManager(
                self.context.runner,
                self.context.systemd,
                self.context.config,
            )
            self._conflict_ui_ready = True
            self._refresh_conflicts()

        def refresh(self):
            super().refresh()
            if getattr(self, "_conflict_ui_ready", False):
                self._refresh_conflicts()

        def _refresh_conflicts(self):
            def collect():
                return self._conflict_manager.conflicts()

            def done(conflicts):
                self._render_conflicts(conflicts)
                return False

            def failed(exc):
                # A failed read-only probe should not turn the whole Dashboard
                # into an error state. Keep the optional card hidden and expose
                # the diagnostic through the normal activity/status area.
                self.conflict_card.set_visible(False)
                self.window.set_activity(False, str(exc), error=True)
                return False

            self.worker.submit(collect, done, failed)

        @staticmethod
        def _ports_text(conflict: PortConflict) -> str:
            return ", ".join(str(port) for port in conflict.ports)

        def _render_conflicts(self, conflicts: list[PortConflict]):
            if not conflicts:
                self.conflict_card.set_visible(False)
                self._replace_conflict_children([])
                return

            children = [
                label("Conflict", "section-title"),
                label(
                    "Another listener is using port 80 or 443. "
                    "Disable & Stop will ask for system authorization once, then "
                    "verify the current owner and act only on systemd services. "
                    "NativeDev never kills arbitrary processes.",
                    "muted",
                    wrap=True,
                ),
            ]

            for conflict in conflicts:
                row = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL,
                    spacing=8,
                )
                row.add_css_class("service-row")

                top = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    spacing=10,
                )
                name = label(conflict.title, "row-title")
                name.set_hexpand(True)
                top.append(name)
                top.append(
                    status_pill(
                        f"Port {self._ports_text(conflict)}",
                        False,
                    )
                )

                if conflict.service:
                    enabled = conflict.enabled_state or "unknown"
                    if enabled.startswith("enabled"):
                        top.append(status_pill("Enabled", None))
                    elif enabled == "disabled":
                        top.append(status_pill("Disabled", None))
                    elif enabled:
                        top.append(status_pill(enabled.capitalize(), None))

                row.append(top)

                if conflict.service:
                    description = (
                        f" · {conflict.description}"
                        if conflict.description
                        else ""
                    )
                    detail = (
                        f"{conflict.service}{description} is listening on port "
                        f"{self._ports_text(conflict)}."
                    )
                elif conflict.process:
                    pid_text = (
                        f" · PID {', '.join(str(pid) for pid in conflict.pids)}"
                        if conflict.pids
                        else ""
                    )
                    detail = (
                        f"{conflict.process}{pid_text} is listening on port "
                        f"{self._ports_text(conflict)} and is not currently "
                        "resolvable to a manageable systemd service."
                    )
                else:
                    detail = (
                        f"Port {self._ports_text(conflict)} is in use by a "
                        "system-owned listener. Its service identity is hidden "
                        "until an explicit action is authorized."
                    )

                row.append(label(detail, "muted", wrap=True))

                if conflict.manageable:
                    actions = Gtk.Box(
                        orientation=Gtk.Orientation.HORIZONTAL,
                        spacing=8,
                    )

                    disable = Gtk.Button(label="Disable & Stop")
                    disable.add_css_class("destructive-action")
                    disable.connect(
                        "clicked",
                        lambda _b, item=conflict, btn=disable: self.action(
                            btn,
                            lambda: self._conflict_manager.disable_and_stop(item),
                            success_message=(
                                f"Disabled and stopped {item.service_name}"
                                if item.service_name
                                else "Conflicting service disabled and stopped"
                            ),
                            after=self.refresh,
                        ),
                    )
                    actions.append(disable)
                    row.append(actions)

                children.append(row)

            self._replace_conflict_children(children)
            self.conflict_card.set_visible(True)

        def _replace_conflict_children(self, children):
            while child := self.conflict_card.get_first_child():
                self.conflict_card.remove(child)
            for child in children:
                self.conflict_card.append(child)

    gui_module.DashboardPage = DashboardPageWithConflicts

    # MainWindow.PAGES is created when gui.py is imported, so it already holds
    # the original DashboardPage class object. Replacing only
    # gui_module.DashboardPage is not enough: MainWindow would keep
    # instantiating the cached original class and the Conflict card would never
    # appear. Replace the dashboard entry in the cached page registry too.
    gui_module.MainWindow.PAGES = tuple(
        (
            key,
            title,
            DashboardPageWithConflicts if key == "dashboard" else page_class,
        )
        for key, title, page_class in gui_module.MainWindow.PAGES
    )
