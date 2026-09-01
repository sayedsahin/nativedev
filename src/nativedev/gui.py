from __future__ import annotations

import html
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from .context import AppContext
from .services import COMPONENTS, ComponentSpec


class Worker:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="nativedev")

    def submit(self, fn: Callable, success: Callable | None = None, error: Callable | None = None):
        future = self.pool.submit(fn)

        def done(f):
            try:
                value = f.result()
            except Exception as exc:  # noqa: BLE001 - UI boundary
                if error:
                    GLib.idle_add(error, exc)
            else:
                if success:
                    GLib.idle_add(success, value)

        future.add_done_callback(done)

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


def label(text: str = "", css: str | None = None, *, wrap: bool = False) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_wrap(wrap)
    widget.set_selectable(False)
    if css:
        widget.add_css_class(css)
    return widget


def page_header(title: str, subtitle: str, refresh: Callable | None = None) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_margin_bottom(18)
    copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    copy.set_hexpand(True)
    copy.append(label(title, "page-title"))
    copy.append(label(subtitle, "muted", wrap=True))
    box.append(copy)
    if refresh:
        button = Gtk.Button(label="Refresh")
        button.connect("clicked", lambda *_: refresh())
        box.append(button)
    return box


def card() -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.add_css_class("card")
    return box


def status_pill(text: str, ok: bool | None = None) -> Gtk.Label:
    widget = label(text, "pill")
    if ok is True:
        widget.add_css_class("status-ok")
    elif ok is False:
        widget.add_css_class("status-warn")
    return widget


def confirm(parent: Gtk.Window, title: str, message: str, on_accept: Callable[[], None]):
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.CANCEL,
        text=title,
        secondary_text=message,
    )
    dialog.add_button("Continue", Gtk.ResponseType.OK)

    def response(_dialog, response_id):
        dialog.destroy()
        if response_id == Gtk.ResponseType.OK:
            on_accept()

    dialog.connect("response", response)
    dialog.present()


class Page(Gtk.ScrolledWindow):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.context = window.context
        self.worker = window.worker
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.body.set_margin_top(28)
        self.body.set_margin_bottom(28)
        self.body.set_margin_start(28)
        self.body.set_margin_end(28)
        self.set_child(self.body)

    def busy(self, widget: Gtk.Widget, active: bool):
        widget.set_sensitive(not active)

    def action(self, button: Gtk.Button, fn: Callable, *, success_message: str, after: Callable | None = None):
        self.busy(button, True)
        self.window.set_activity(True, "Working…")

        def success(_value=None):
            self.busy(button, False)
            self.window.set_activity(False, success_message)
            if after:
                after()
            return False

        def error(exc):
            self.busy(button, False)
            self.window.set_activity(False, str(exc), error=True)
            return False

        self.worker.submit(fn, success, error)


class DashboardPage(Page):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.body.append(page_header("Dashboard", "Native Debian development services at a glance.", self.refresh))
        self.system_card = card()
        self.environment_card = card()
        self.body.append(self.system_card)
        self.body.append(self.environment_card)
        self.refresh()

    def refresh(self):
        self._replace(self.system_card, [label("Loading system status…", "muted")])
        self._replace(self.environment_card, [label("Checking development environment…", "muted")])

        def collect():
            php = self.context.php.installed_versions()
            return {
                "distro": self.context.distro.pretty_name,
                "supported": self.context.distro.is_debian_family,
                "php": php,
                "node": self.context.node.current_node(),
                "nvm": self.context.node.installed(),
                "projects": len(self.context.localdev.projects()),
                "dns": self.context.localdev.dns_ready(),
                "nginx": self.context.systemd.is_active("nginx"),
            }

        def done(data):
            system = [label("System", "section-title")]
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(label(data["distro"]))
            row.append(status_pill("Supported" if data["supported"] else "Unsupported", data["supported"]))
            system.append(row)
            self._replace(self.system_card, system)

            env = [label("Environment", "section-title")]
            env.extend([
                self._metric("PHP", ", ".join(data["php"]) if data["php"] else "Not installed", bool(data["php"])),
                self._metric("Node / NVM", data["node"] or ("NVM installed" if data["nvm"] else "Not installed"), data["nvm"]),
                self._metric("Nginx", "Running" if data["nginx"] else "Stopped / missing", data["nginx"]),
                self._metric(f"*.{self.context.config.domain}", "Ready" if data["dns"] else "Not configured", data["dns"]),
                self._metric("Projects", str(data["projects"]), None),
            ])
            self._replace(self.environment_card, env)
            return False

        self.worker.submit(collect, done, lambda exc: self.window.set_activity(False, str(exc), error=True))

    @staticmethod
    def _metric(name: str, value: str, ok: bool | None) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.append(label(name, "row-title"))
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)
        row.append(status_pill(value, ok))
        return row

    @staticmethod
    def _replace(container: Gtk.Box, children: list[Gtk.Widget]):
        while child := container.get_first_child():
            container.remove(child)
        for child in children:
            container.append(child)


class PhpPage(Page):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.body.append(page_header("PHP", "Sury PHP discovery, parallel versions and PHP-FPM.", self.refresh))
        self.repo_card = card()
        self.versions_card = card()
        self.body.append(self.repo_card)
        self.body.append(self.versions_card)
        self.refresh()

    def refresh(self):
        self._clear(self.repo_card, "Checking repository…")
        self._clear(self.versions_card, "Loading PHP versions…")

        def collect():
            installed = self.context.php.installed_versions()
            available = self.context.php.available_versions()
            versions = sorted(set(installed) | set(available), key=self.context.php._version_key, reverse=True)
            fpm = {}
            for version in versions:
                fpm_installed = self.context.apt.is_installed(f"php{version}-fpm")
                fpm[version] = {
                    "installed": fpm_installed,
                    "running": self.context.php.fpm_running(version) if fpm_installed else False,
                    "enabled_state": self.context.php.fpm_enabled_state(version) if fpm_installed else "n/a",
                    "developer_pool": self.context.php.developer_pool_configured(version) if fpm_installed else False,
                }
            return {
                "sury": self.context.php.sury_configured(),
                "sury_supported": self.context.php.sury_supported,
                "codename": self.context.distro.codename,
                "installed": installed,
                "available": available,
                "versions": versions,
                "cli": self.context.php.cli_version(),
                "developer_user": self.context.php.developer_user,
                "fpm": fpm,
            }

        def done(data):
            self._build_repo(data)
            self._build_versions(data)
            return False

        self.worker.submit(collect, done, lambda exc: self.window.set_activity(False, str(exc), error=True))

    def _build_repo(self, data):
        self._remove_all(self.repo_card)
        self.repo_card.append(label("Sury repository", "section-title"))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.append(status_pill("Configured" if data["sury"] else "Not configured", data["sury"]))
        row.append(label(f"Base suite: {data['codename'] or 'unknown'}", "muted"))
        row.set_hexpand(True)
        self.repo_card.append(row)
        if not data["sury"]:
            button = Gtk.Button(label="Configure Sury")
            button.set_sensitive(data["sury_supported"] and self.context.distro.is_debian_family)
            button.add_css_class("suggested-action")
            button.connect(
                "clicked",
                lambda *_: confirm(
                    self.window,
                    "Configure Sury PHP?",
                    "This installs the Sury archive keyring, creates NativeDev's own APT source file, and refreshes APT metadata.",
                    lambda: self.action(button, self.context.php.configure_sury, success_message="Sury configured", after=self.refresh),
                ),
            )
            self.repo_card.append(button)
            if not data["sury_supported"]:
                self.repo_card.append(label("Sury does not currently publish this detected base suite.", "muted", wrap=True))

    def _build_versions(self, data):
        self._remove_all(self.versions_card)
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title_row.append(label("PHP versions", "section-title"))
        if data["cli"]:
            title_row.append(status_pill(f"Default {data['cli']}", True))
        self.versions_card.append(title_row)
        if not data["versions"]:
            self.versions_card.append(label("No versioned PHP packages found in APT metadata.", "muted"))
            return

        installed = set(data["installed"])
        available = set(data["available"])
        for version in data["versions"]:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            row.add_css_class("service-row")
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            name = label(f"PHP {version}", "row-title")
            name.set_hexpand(True)
            top.append(name)

            if version in installed:
                fpm = data["fpm"].get(version, {})
                if fpm.get("installed"):
                    top.append(status_pill("FPM running" if fpm.get("running") else "FPM stopped", fpm.get("running")))
                    state = fpm.get("enabled_state", "unknown")
                    top.append(status_pill("Enabled" if state.startswith("enabled") else state.capitalize(), state.startswith("enabled") if state in {"enabled", "disabled"} else None))
                    if fpm.get("developer_pool"):
                        top.append(status_pill(f"*.test as {data['developer_user']}", True))
                    else:
                        top.append(status_pill("*.test pool not configured", None))
                else:
                    top.append(status_pill("CLI installed", True))
            else:
                top.append(status_pill("Available", None))
            row.append(top)

            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            if version in installed:
                default = Gtk.Button(label="Default")
                is_default = data["cli"] == version
                default.set_sensitive(not is_default)
                if not is_default:
                    default.add_css_class("suggested-action")
                    default.connect(
                        "clicked",
                        lambda _b, v=version, btn=default: confirm(
                            self.window,
                            f"Set PHP {v} as default?",
                            "NativeDev will use update-alternatives to change the default /usr/bin/php command.",
                            lambda: self.action(btn, lambda: self.context.php.set_cli_default(v), success_message=f"PHP {v} is now default", after=self.refresh),
                        ),
                    )
                actions.append(default)

                fpm = data["fpm"].get(version, {})
                if fpm.get("installed"):
                    if not fpm.get("developer_pool"):
                        pool = Gtk.Button(label="Configure *.test pool")
                        pool.connect(
                            "clicked",
                            lambda _b, v=version, btn=pool: confirm(
                                self.window,
                                f"Configure PHP {v} for *.test?",
                                f"NativeDev will create its own PHP-FPM pool running as {data['developer_user']}. Debian/Sury's www pool is not modified.",
                                lambda: self.action(btn, lambda: self.context.php.ensure_developer_pool(v), success_message=f"PHP {v} NativeDev pool configured", after=self.refresh),
                            ),
                        )
                        actions.append(pool)
                    if fpm.get("running"):
                        stop = Gtk.Button(label="Stop")
                        stop.connect("clicked", lambda _b, v=version, btn=stop: self.action(btn, lambda: self.context.php.stop_fpm(v), success_message=f"PHP {v} FPM stopped", after=self.refresh))
                        actions.append(stop)
                        restart = Gtk.Button(label="Restart")
                        restart.connect("clicked", lambda _b, v=version, btn=restart: self.action(btn, lambda: self.context.php.restart_fpm(v), success_message=f"PHP {v} FPM restarted", after=self.refresh))
                        actions.append(restart)
                    else:
                        start_btn = Gtk.Button(label="Start")
                        start_btn.connect("clicked", lambda _b, v=version, btn=start_btn: self.action(btn, lambda: self.context.php.start_fpm(v), success_message=f"PHP {v} FPM started", after=self.refresh))
                        actions.append(start_btn)

                    enabled_state = fpm.get("enabled_state")
                    if enabled_state == "enabled":
                        disable = Gtk.Button(label="Disable")
                        disable.connect("clicked", lambda _b, v=version, btn=disable: self.action(btn, lambda: self.context.php.disable_fpm(v), success_message=f"PHP {v} FPM disabled", after=self.refresh))
                        actions.append(disable)
                    elif enabled_state == "disabled":
                        enable = Gtk.Button(label="Enable")
                        enable.connect("clicked", lambda _b, v=version, btn=enable: self.action(btn, lambda: self.context.php.enable_fpm(v), success_message=f"PHP {v} FPM enabled", after=self.refresh))
                        actions.append(enable)

                uninstall = Gtk.Button(label="Uninstall")
                uninstall.add_css_class("destructive-action")
                uninstall.connect(
                    "clicked",
                    lambda _b, v=version, btn=uninstall: confirm(
                        self.window,
                        f"Uninstall PHP {v}?",
                        f"All installed php{v}-* packages managed by APT will be removed. Other PHP versions are not touched.",
                        lambda: self.action(btn, lambda: self.context.php.uninstall_version(v), success_message=f"PHP {v} uninstalled", after=self.refresh),
                    ),
                )
                actions.append(uninstall)
            elif version in available:
                install = Gtk.Button(label="Install")
                install.add_css_class("suggested-action")
                install.connect(
                    "clicked",
                    lambda _b, v=version, btn=install: confirm(
                        self.window,
                        f"Install PHP {v}?",
                        "This installs CLI, FPM and common development extensions from your configured APT repositories.",
                        lambda: self.action(btn, lambda: self.context.php.install_version(v), success_message=f"PHP {v} installed", after=self.refresh),
                    ),
                )
                actions.append(install)
            row.append(actions)
            self.versions_card.append(row)

    @staticmethod
    def _remove_all(box: Gtk.Box):
        while child := box.get_first_child():
            box.remove(child)

    def _clear(self, box: Gtk.Box, text: str):
        self._remove_all(box)
        box.append(label(text, "muted"))


class NodePage(Page):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.body.append(page_header("Node.js", "Per-user NVM with complete LTS generation management.", self.refresh))
        self.nvm_card = card()
        self.node_card = card()
        self.body.append(self.nvm_card)
        self.body.append(self.node_card)
        self.refresh()

    def refresh(self):
        self._replace(self.nvm_card, [label("Checking NVM…", "muted")])
        self._replace(self.node_card, [label("Loading Node.js LTS releases…", "muted")])

        def collect():
            installed = self.context.node.installed()
            releases = []
            lts_error = ""
            if installed:
                try:
                    releases = self.context.node.available_lts()
                except Exception as exc:  # network/NVM boundary
                    lts_error = str(exc)
            return {
                "installed": installed,
                "nvm": self.context.node.nvm_version() if installed else "",
                "current": self.context.node.current_node() if installed else "",
                "default": self.context.node.default_node() if installed else "",
                "versions": self.context.node.installed_versions() if installed else [],
                "lts": releases,
                "lts_error": lts_error,
                "rc": str(self.context.node.shell_rc()),
            }

        def done(data):
            nvm = [label("NVM", "section-title")]
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.append(status_pill(f"Installed {data['nvm']}" if data["installed"] else "Not installed", data["installed"]))
            row.append(label(f"Shell: {data['rc']}", "muted"))
            nvm.append(row)
            if data["installed"]:
                shell_btn = Gtk.Button(label="Configure shell")
                shell_btn.connect("clicked", lambda *_: self.action(shell_btn, self.context.node.configure_shell, success_message="Shell integration configured"))
                nvm.append(shell_btn)
            else:
                install = Gtk.Button(label="Install NVM")
                install.add_css_class("suggested-action")
                install.connect(
                    "clicked",
                    lambda *_: confirm(
                        self.window,
                        "Install NVM?",
                        "NVM will be installed for your user only. NativeDev adds a clearly marked block to your shell startup file.",
                        lambda: self.action(install, self.context.node.install_nvm, success_message="NVM installed", after=self.refresh),
                    ),
                )
                nvm.append(install)
            self._replace(self.nvm_card, nvm)

            node = [label("Node.js LTS releases", "section-title")]
            if not data["installed"]:
                node.append(label("Install NVM first to manage Node.js versions.", "muted"))
                self._replace(self.node_card, node)
                return False

            summary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            summary.append(status_pill(f"Default: {data['default'] or 'none'}", bool(data["default"])))
            if data["current"]:
                summary.append(status_pill(f"Current shell: {data['current']}", True))
            node.append(summary)
            if data["lts_error"]:
                node.append(label(f"Could not refresh remote LTS list: {data['lts_error']}", "error-text", wrap=True))

            installed = set(data["versions"])
            shown: set[str] = set()
            for release in data["lts"]:
                shown.add(release.version)
                node.append(self._node_version_row(release.version, release.codename, installed, data["default"]))

            extras = [version for version in data["versions"] if version not in shown]
            if extras:
                node.append(label("Other installed versions", "section-title"))
                for version in extras:
                    node.append(self._node_version_row(version, "Installed", installed, data["default"]))
            elif not data["lts"] and not data["versions"]:
                node.append(label("No Node.js versions found.", "muted"))

            self._replace(self.node_card, node)
            return False

        self.worker.submit(collect, done, lambda exc: self.window.set_activity(False, str(exc), error=True))

    def _node_version_row(self, version: str, subtitle: str, installed: set[str], default_version: str) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        row.add_css_class("service-row")
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        copy.set_hexpand(True)
        copy.append(label(version, "row-title"))
        copy.append(label(subtitle, "muted"))
        top.append(copy)
        is_installed = version in installed
        top.append(status_pill("Installed" if is_installed else "Available", True if is_installed else None))
        row.append(top)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if is_installed:
            default = Gtk.Button(label="Default")
            is_default = default_version == version
            default.set_sensitive(not is_default)
            if not is_default:
                default.add_css_class("suggested-action")
                default.connect(
                    "clicked",
                    lambda _b, v=version, btn=default: self.action(
                        btn,
                        lambda: self.context.node.set_default(v),
                        success_message=f"Node {v} is now default",
                        after=self.refresh,
                    ),
                )
            actions.append(default)

            uninstall = Gtk.Button(label="Uninstall")
            uninstall.add_css_class("destructive-action")
            uninstall.connect(
                "clicked",
                lambda _b, v=version, btn=uninstall: confirm(
                    self.window,
                    f"Uninstall Node {v}?",
                    "This removes only this NVM-managed Node.js version from your user account.",
                    lambda: self.action(btn, lambda: self.context.node.uninstall_version(v), success_message=f"Node {v} uninstalled", after=self.refresh),
                ),
            )
            actions.append(uninstall)
        else:
            install = Gtk.Button(label="Install")
            install.add_css_class("suggested-action")
            install.connect(
                "clicked",
                lambda _b, v=version, btn=install: self.action(
                    btn,
                    lambda: self.context.node.install_version(v),
                    success_message=f"Node {v} installed",
                    after=self.refresh,
                ),
            )
            actions.append(install)
        row.append(actions)
        return row

    @staticmethod
    def _replace(container: Gtk.Box, children: list[Gtk.Widget]):
        while child := container.get_first_child():
            container.remove(child)
        for child in children:
            container.append(child)


class ServicesPage(Page):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.body.append(page_header("Services & tools", "Install, remove and control Debian-family system components.", self.refresh))
        note = label("System authentication is requested once per NativeDev session; privileged actions reuse the restricted helper until the app closes.", "muted", wrap=True)
        self.body.append(note)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.body.append(self.list_box)
        self.refresh()

    def refresh(self):
        self._clear()
        self.list_box.append(label("Detecting installed components…", "muted"))

        def collect():
            return [self.context.services.state(spec) for spec in COMPONENTS]

        def done(states):
            self._clear()
            for state in states:
                spec = state.spec
                box = card()
                top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                copy.set_hexpand(True)
                copy.append(label(spec.title, "section-title"))
                if spec.note:
                    copy.append(label(spec.note, "muted", wrap=True))
                top.append(copy)
                if state.running:
                    top.append(status_pill("Running", True))
                elif state.installed:
                    top.append(status_pill("Installed", True))
                else:
                    top.append(status_pill("Not installed", False))
                if spec.service and state.installed:
                    top.append(status_pill(state.enabled_state.capitalize(), state.enabled if state.enabled_state in {"enabled", "disabled"} else None))
                box.append(top)

                actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                if not state.installed:
                    install = Gtk.Button(label="Install")
                    install.set_sensitive(state.installable and self.context.distro.is_debian_family)
                    install.add_css_class("suggested-action")
                    install.connect(
                        "clicked",
                        lambda _b, s=spec, btn=install: confirm(
                            self.window,
                            f"Install {s.title}?",
                            "NativeDev will install the Debian-family system package(s). Services are enabled and started when applicable.",
                            lambda: self.action(btn, lambda: self.context.services.install(s), success_message=f"{s.title} installed", after=self.refresh),
                        ),
                    )
                    actions.append(install)
                else:
                    if spec.service:
                        if state.running:
                            stop = Gtk.Button(label="Stop")
                            stop.connect("clicked", lambda _b, s=spec, btn=stop: self.action(btn, lambda: self.context.services.stop(s), success_message=f"{s.title} stopped", after=self.refresh))
                            actions.append(stop)
                            restart = Gtk.Button(label="Restart")
                            restart.connect("clicked", lambda _b, s=spec, btn=restart: self.action(btn, lambda: self.context.services.restart(s), success_message=f"{s.title} restarted", after=self.refresh))
                            actions.append(restart)
                        else:
                            start_btn = Gtk.Button(label="Start")
                            start_btn.connect("clicked", lambda _b, s=spec, btn=start_btn: self.action(btn, lambda: self.context.services.start(s), success_message=f"{s.title} started", after=self.refresh))
                            actions.append(start_btn)

                        if state.enabled_state == "enabled":
                            disable = Gtk.Button(label="Disable")
                            disable.connect("clicked", lambda _b, s=spec, btn=disable: self.action(btn, lambda: self.context.services.disable(s), success_message=f"{s.title} disabled", after=self.refresh))
                            actions.append(disable)
                        elif state.enabled_state == "disabled":
                            enable = Gtk.Button(label="Enable")
                            enable.connect("clicked", lambda _b, s=spec, btn=enable: self.action(btn, lambda: self.context.services.enable(s), success_message=f"{s.title} enabled", after=self.refresh))
                            actions.append(enable)

                    uninstall = Gtk.Button(label="Uninstall")
                    uninstall.set_sensitive(state.uninstallable)
                    uninstall.add_css_class("destructive-action")
                    if state.uninstallable:
                        uninstall.connect(
                            "clicked",
                            lambda _b, s=spec, btn=uninstall: confirm(
                                self.window,
                                f"Uninstall {s.title}?",
                                "NativeDev removes only the package(s) listed for this component. Configuration files are not purged.",
                                lambda: self.action(btn, lambda: self.context.services.uninstall(s), success_message=f"{s.title} uninstalled", after=self.refresh),
                            ),
                        )
                    actions.append(uninstall)
                    if state.uninstall_note:
                        box.append(label(state.uninstall_note, "muted", wrap=True))
                if actions.get_first_child():
                    box.append(actions)
                self.list_box.append(box)
            return False

        self.worker.submit(collect, done, lambda exc: self.window.set_activity(False, str(exc), error=True))

    def _clear(self):
        while child := self.list_box.get_first_child():
            self.list_box.remove(child)


class ProjectsPage(Page):
    PHP_DEFAULT = "default"

    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.body.append(
            page_header(
                "Projects",
                "Per-project PHP-FPM version for *.test sites.",
                self.refresh,
            )
        )
        self.summary_card = card()
        self.projects_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.body.append(self.summary_card)
        self.body.append(self.projects_box)
        self.refresh()

    def refresh(self):
        self._replace(self.summary_card, [label("Loading projects…", "muted")])
        self._clear_projects()

        def collect():
            projects = self.context.localdev.projects()
            # A newly discovered project needs Nginx to be able to reach its
            # document root. Apply that read-only grant here so users do not
            # have to troubleshoot www-data permissions before the site runs.
            readable_errors: dict[str, str] = {}
            for project in projects:
                try:
                    self.context.localdev.ensure_project_readable(project)
                except Exception as exc:  # permission/filesystem boundary
                    readable_errors[str(project)] = str(exc)
            default_php = self.context.localdev.default_php_version()
            fpm_versions = self.context.php.installed_fpm_versions()
            rows = []
            for project in projects:
                rows.append(
                    {
                        "path": project,
                        "root": self.context.localdev.document_root(project),
                        "prefs": self.context.localdev.project_preferences(project),
                        "readable_error": readable_errors.get(str(project), ""),
                    }
                )
            return {
                "projects": rows,
                "default_php": default_php,
                "fpm_versions": fpm_versions,
            }

        def done(data):
            summary = [label("Parked projects", "section-title")]
            summary.append(label(str(self.context.localdev.park_dir), "muted", wrap=True))
            summary.append(
                status_pill(
                    f"Default PHP-FPM: {data['default_php']}" if data["default_php"] else "No PHP-FPM installed",
                    bool(data["default_php"]),
                )
            )
            self._replace(self.summary_card, summary)

            self._clear_projects()
            if not data["projects"]:
                empty = card()
                empty.append(label("No project directories found.", "muted"))
                self.projects_box.append(empty)
                return False

            for item in data["projects"]:
                self.projects_box.append(
                    self._project_card(
                        item["path"],
                        item["root"],
                        item["prefs"],
                        data["default_php"],
                        data["fpm_versions"],
                        item["readable_error"],
                    )
                )
            return False

        self.worker.submit(collect, done, lambda exc: self.window.set_activity(False, str(exc), error=True))

    def _project_card(
        self,
        project: Path,
        docroot: Path,
        prefs: dict[str, str],
        default_php: str,
        fpm_versions: list[str],
        readable_error: str,
    ) -> Gtk.Widget:
        box = card()
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        copy.set_hexpand(True)
        copy.append(label(f"{project.name}.{self.context.config.domain}", "section-title"))
        copy.append(label(str(project), "muted", wrap=True))
        if docroot != project:
            copy.append(label(f"Document root: {docroot}", "muted", wrap=True))
        top.append(copy)

        controls = Gtk.Grid(column_spacing=10, row_spacing=8)
        controls.set_valign(Gtk.Align.CENTER)

        php_labels = [f"Default ({default_php})" if default_php else "Default"] + [
            f"PHP {version}" for version in fpm_versions
        ]
        php_values = [self.PHP_DEFAULT, *fpm_versions]
        php_dropdown = Gtk.DropDown.new_from_strings(php_labels)
        try:
            php_index = php_values.index(prefs.get("php", self.PHP_DEFAULT))
        except ValueError:
            php_index = 0
        php_dropdown.set_selected(php_index)
        php_dropdown.set_sensitive(bool(default_php or fpm_versions))

        controls.attach(label("PHP"), 0, 0, 1, 1)
        controls.attach(php_dropdown, 1, 0, 1, 1)
        top.append(controls)
        box.append(top)

        if readable_error:
            box.append(label(f"Nginx read access: {readable_error}", "error-text", wrap=True))
        else:
            box.append(
                label(
                    f"PHP runs as {self.context.php.developer_user} (same as your terminal). "
                    "Nginx only has read access to the document root shown above.",
                    "muted",
                    wrap=True,
                )
            )

        def php_changed(dropdown, _param):
            selected = dropdown.get_selected()
            if selected < 0 or selected >= len(php_values):
                return
            value = php_values[selected]
            if value == prefs.get("php", self.PHP_DEFAULT):
                return
            self.action(
                dropdown,
                lambda: self.context.localdev.set_project_php(project, value),
                success_message=f"{project.name} now uses {'Default PHP' if value == 'default' else 'PHP ' + value}",
                after=self.refresh,
            )

        php_dropdown.connect("notify::selected", php_changed)
        return box

    def _clear_projects(self):
        while child := self.projects_box.get_first_child():
            self.projects_box.remove(child)

    @staticmethod
    def _replace(container: Gtk.Box, children: list[Gtk.Widget]):
        while child := container.get_first_child():
            container.remove(child)
        for child in children:
            container.append(child)


class LocalDevPage(Page):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.body.append(page_header("Local development", "Park projects, configure *.test, Nginx and trusted local HTTPS.", self.refresh))
        self.settings_card = card()
        self.dns_card = card()
        self.nginx_card = card()
        self.https_card = card()
        self.body.append(self.settings_card)
        self.body.append(self.dns_card)
        self.body.append(self.nginx_card)
        self.body.append(self.https_card)
        self.refresh()

    def refresh(self):
        self._build_settings()
        self._replace(self.dns_card, [label("Checking DNS…", "muted")])
        self._replace(self.nginx_card, [label("Checking sites…", "muted")])
        self._replace(self.https_card, [label("Checking HTTPS tools…", "muted")])

        def collect():
            return {
                "dns": self.context.localdev.dns_ready(),
                "strategy": self.context.localdev.dns_strategy(),
                "projects": self.context.localdev.projects(),
                "nginx": self.context.localdev.nginx_ready(),
                "mkcert": self.context.localdev.mkcert_installed(),
                "https": self.context.config.https_enabled,
            }

        def done(data):
            dns = [label(f"*.{self.context.config.domain} DNS", "section-title")]
            dns.append(status_pill("Ready" if data["dns"] else f"Not configured ({data['strategy']})", data["dns"]))
            dns_btn = Gtk.Button(label="Configure automatically")
            dns_btn.set_sensitive(data["strategy"] == "networkmanager")
            dns_btn.add_css_class("suggested-action")
            dns_btn.connect(
                "clicked",
                lambda *_: confirm(
                    self.window,
                    "Configure wildcard DNS?",
                    "NativeDev will add its own NetworkManager dnsmasq snippets and restart NetworkManager. Your connection may briefly reconnect. It will not overwrite /etc/resolv.conf.",
                    lambda: self.action(dns_btn, self.context.localdev.configure_dns, success_message="Wildcard DNS configured", after=self.refresh),
                ),
            )
            dns.append(dns_btn)
            self._replace(self.dns_card, dns)

            sites = [label("Nginx sites", "section-title")]
            sites.append(status_pill(f"{len(data['projects'])} projects detected", data["nginx"]))
            sites.append(
                label(
                    f"*.{self.context.config.domain} PHP-FPM runs as {self.context.php.developer_user} "
                    "via NativeDev's per-user pool. Set the PHP version per project on the Projects page.",
                    "muted",
                    wrap=True,
                )
            )
            if data["projects"]:
                names = ", ".join(f"{p.name}.{self.context.config.domain}" for p in data["projects"][:8])
                if len(data["projects"]) > 8:
                    names += ", …"
                sites.append(label(names, "muted", wrap=True))
            site_btn = Gtk.Button(label="Generate / refresh Nginx sites")
            site_btn.add_css_class("suggested-action")
            site_btn.connect(
                "clicked",
                lambda *_: confirm(
                    self.window,
                    "Generate NativeDev Nginx config?",
                    "Only /etc/nginx/sites-available/nativedev-sites.conf and its symlink are managed. nginx -t is run before reload and a failed change is rolled back.",
                    lambda: self.action(site_btn, self.context.localdev.configure_nginx_sites, success_message="Nginx sites refreshed", after=self.refresh),
                ),
            )
            sites.append(site_btn)
            self._replace(self.nginx_card, sites)

            https = [label("Local HTTPS", "section-title")]
            https.append(status_pill("mkcert installed" if data["mkcert"] else "mkcert missing", data["mkcert"]))
            if data["mkcert"]:
                trust = Gtk.Button(label="Trust local CA")
                trust.connect("clicked", lambda *_: self.action(trust, self.context.localdev.trust_mkcert_ca, success_message="Local CA trust configured"))
                enable = Gtk.Button(label="Generate *.test certificate")
                enable.add_css_class("suggested-action")
                enable.connect(
                    "clicked",
                    lambda *_: confirm(
                        self.window,
                        "Enable HTTPS for local sites?",
                        "A wildcard certificate is generated with mkcert and copied to NativeDev's Nginx certificate directory.",
                        lambda: self.action(enable, self.context.localdev.enable_https, success_message="HTTPS enabled", after=self.refresh),
                    ),
                )
                actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                actions.append(trust)
                actions.append(enable)
                https.append(actions)
            else:
                https.append(label("Install mkcert from Services & tools first.", "muted"))
            self._replace(self.https_card, https)
            return False

        self.worker.submit(collect, done, lambda exc: self.window.set_activity(False, str(exc), error=True))

    def _build_settings(self):
        self._replace(self.settings_card, [])
        self.settings_card.append(label("Project settings", "section-title"))
        grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        park = Gtk.Entry()
        park.set_text(self.context.config.park_dir)
        domain = Gtk.Entry()
        domain.set_text(self.context.config.domain)
        domain.set_max_length(30)
        grid.attach(label("Park directory"), 0, 0, 1, 1)
        grid.attach(park, 1, 0, 1, 1)
        grid.attach(label("Local TLD"), 0, 1, 1, 1)
        grid.attach(domain, 1, 1, 1, 1)
        self.settings_card.append(grid)
        self.settings_card.append(
            label(
                "Projects use the system Default PHP-FPM automatically. Override PHP per site from Projects.",
                "muted",
                wrap=True,
            )
        )
        save = Gtk.Button(label="Save settings")
        save.add_css_class("suggested-action")

        def save_settings(*_):
            value = domain.get_text().strip().lower().lstrip(".")
            if not value or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in value):
                self.window.set_activity(False, "Local TLD must contain only letters, numbers or hyphens", error=True)
                return
            self.context.config.park_dir = str(Path(park.get_text()).expanduser())
            self.context.config.domain = value
            self.context.config.save()
            self.window.set_activity(False, "Settings saved")
            self.refresh()

        save.connect("clicked", save_settings)
        self.settings_card.append(save)

    @staticmethod
    def _replace(container: Gtk.Box, children: list[Gtk.Widget]):
        while child := container.get_first_child():
            container.remove(child)
        for child in children:
            container.append(child)


class DoctorPage(Page):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.body.append(page_header("Doctor", "Read-only diagnostics for the current system.", self.refresh))
        self.card = card()
        self.output = Gtk.TextView()
        self.output.set_editable(False)
        self.output.set_cursor_visible(False)
        self.output.set_monospace(True)
        self.output.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.card.append(self.output)
        self.body.append(self.card)
        self.refresh()

    def refresh(self):
        self.output.get_buffer().set_text("Running diagnostics…")

        def done(checks):
            self.output.get_buffer().set_text(self.context.doctor.format(checks))
            return False

        self.worker.submit(self.context.doctor.run, done, lambda exc: self.output.get_buffer().set_text(str(exc)))


class MainWindow(Gtk.ApplicationWindow):
    PAGES = (
        ("dashboard", "Dashboard", DashboardPage),
        ("php", "PHP", PhpPage),
        ("node", "Node.js", NodePage),
        ("services", "Services & tools", ServicesPage),
        ("projects", "Projects", ProjectsPage),
        ("local", "Local development", LocalDevPage),
        ("doctor", "Doctor", DoctorPage),
    )

    def __init__(self, application: Gtk.Application, context: AppContext):
        super().__init__(application=application)
        self.context = context
        self.worker = Worker()
        self.set_title("NativeDev")
        self.set_default_size(1040, 700)
        self.set_size_request(760, 520)

        header = Gtk.HeaderBar()
        title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title.append(label("NativeDev", "app-title"))
        title.append(label("Native Debian development manager", "muted"))
        header.set_title_widget(title)
        self.set_titlebar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        root.append(content)

        self.sidebar = Gtk.ListBox()
        self.sidebar.add_css_class("sidebar")
        self.sidebar.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.sidebar.set_size_request(210, -1)
        content.append(self.sidebar)

        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        content.append(self.stack)

        self.status = label("Ready", "statusbar")
        self.status.set_margin_start(14)
        self.status.set_margin_end(14)
        self.status.set_margin_top(7)
        self.status.set_margin_bottom(7)
        root.append(self.status)
        self.set_child(root)

        for key, title_text, klass in self.PAGES:
            row = Gtk.ListBoxRow()
            row.set_name(key)
            nav_label = label(title_text, "nav-label")
            nav_label.set_margin_top(11)
            nav_label.set_margin_bottom(11)
            nav_label.set_margin_start(14)
            nav_label.set_margin_end(14)
            row.set_child(nav_label)
            self.sidebar.append(row)
            self.stack.add_named(klass(self), key)

        self.sidebar.connect("row-selected", self._on_row_selected)
        self.sidebar.select_row(self.sidebar.get_row_at_index(0))
        self.connect("close-request", self._on_close)

        if not self.context.distro.is_debian_family:
            self.set_activity(False, f"{self.context.distro.pretty_name} is outside the supported Debian family", error=True)

    def _on_row_selected(self, _listbox, row):
        if row:
            self.stack.set_visible_child_name(row.get_name())

    def set_activity(self, active: bool, message: str, *, error: bool = False):
        self.status.set_text(message)
        self.status.remove_css_class("error-text")
        self.status.remove_css_class("muted")
        if error:
            self.status.add_css_class("error-text")
        elif active:
            self.status.add_css_class("muted")

    def _on_close(self, *_):
        self.worker.shutdown()
        self.context.runner.close()
        return False


class NativeDevApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.nativedev.Manager")
        self.context = AppContext.create()
        self.window: MainWindow | None = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self._load_css()

    def do_activate(self):
        if not self.window:
            self.window = MainWindow(self, self.context)
        self.window.present()

    @staticmethod
    def _load_css():
        css_path = Path(__file__).with_name("style.css")
        provider = Gtk.CssProvider()
        provider.load_from_path(str(css_path))
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
