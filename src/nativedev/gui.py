from __future__ import annotations

import html
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from . import __version__
from .context import AppContext
from .services import COMPONENTS, ComponentSpec


class Worker:
    def __init__(self):
        # Read-only probes may run concurrently. Every mutating action goes
        # through one queue so multi-command transactions cannot interleave.
        self.read_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="nativedev-read")
        self.mutation_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nativedev-mutate")

    def _submit_to(self, pool: ThreadPoolExecutor, fn: Callable, success: Callable | None = None, error: Callable | None = None):
        future = pool.submit(fn)

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

    def submit(self, fn: Callable, success: Callable | None = None, error: Callable | None = None):
        self._submit_to(self.read_pool, fn, success, error)

    def submit_mutation(self, fn: Callable, success: Callable | None = None, error: Callable | None = None):
        self._submit_to(self.mutation_pool, fn, success, error)

    def shutdown(self):
        self.read_pool.shutdown(wait=False, cancel_futures=True)
        self.mutation_pool.shutdown(wait=False, cancel_futures=True)


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

        self.worker.submit_mutation(
            lambda: self.context.controller.run_mutation(fn), success, error
        )


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
                "node_provider": self.context.node.provider(),
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
                self._metric("Node", (f"{data['node']} · {data['node_provider'].upper()}" if data["node"] else "Not installed"), bool(data["node"])),
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
        self.body.append(page_header("PHP", "One active PHP provider: Debian system PHP or Sury multi-version PHP.", self.refresh))
        self.repo_card = card()
        self.versions_card = card()
        self.body.append(self.repo_card)
        self.body.append(self.versions_card)
        self.refresh()

    def refresh(self):
        self._clear(self.repo_card, "Checking repository…")
        self._clear(self.versions_card, "Loading PHP versions…")

        def collect():
            sury = self.context.php.sury_configured()
            provider = self.context.php.provider()
            installed = self.context.php.installed_versions()
            available = self.context.php.available_versions() if provider == "sury" else []
            installed_sorted = sorted(installed, key=self.context.php._version_key, reverse=True)
            available_sorted = sorted(
                set(available).difference(installed),
                key=self.context.php._version_key,
                reverse=True,
            )
            versions = installed_sorted + available_sorted
            fpm = {}
            for version in versions:
                fpm_installed = self.context.apt.is_installed(f"php{version}-fpm")
                config_ready = self.context.php.fpm_config_ready(version) if fpm_installed else False
                fpm[version] = {
                    "installed": fpm_installed,
                    "config_ready": config_ready,
                    "running": self.context.php.fpm_running(version) if config_ready else False,
                    "enabled_state": self.context.php.fpm_enabled_state(version) if fpm_installed else "n/a",
                    "developer_pool": self.context.php.developer_pool_configured(version) if config_ready else False,
                }
            return {
                "sury": sury,
                "provider": provider,
                "sury_migration_needed": self.context.php.sury_migration_needed() if provider == "sury" else False,
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
        self.repo_card.append(label("PHP provider", "section-title"))
        provider = data["provider"]
        names = {"debian": "Debian", "sury": "Sury", "none": "Not configured"}
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.append(status_pill(names.get(provider, provider.title()), provider in {"debian", "sury"}))
        if data["cli"]:
            row.append(label(f"Default PHP {data['cli']}", "muted"))
        row.set_hexpand(True)
        self.repo_card.append(row)

        if provider == "debian":
            self.repo_card.append(label(
                "NativeDev is using Debian's system PHP. Existing PHP can serve *.test through NativeDev's per-user FPM pool.",
                "muted", wrap=True,
            ))
            button = Gtk.Button(label="Enable Sury Multi-PHP")
            button.set_sensitive(data["sury_supported"] and self.context.distro.is_debian_family)
            button.add_css_class("suggested-action")
            installed_text = ", ".join(f"PHP {v}" for v in data["installed"]) or "the current Debian PHP runtime"
            button.connect(
                "clicked",
                lambda *_: confirm(
                    self.window,
                    "Enable Sury multi-PHP?",
                    f"NativeDev will migrate {installed_text} from Debian's PHP provider to Sury, enable multi-version PHP, keep the current default where possible, and reconcile *.test. Once Sury is active NativeDev will no longer offer Debian PHP as another provider.",
                    lambda: self.action(button, self.context.controller.enable_sury_multi_php, success_message="Sury multi-PHP enabled", after=self.refresh),
                ),
            )
            self.repo_card.append(button)
        elif provider == "sury":
            self.repo_card.append(label(
                "Sury multi-version PHP is active. NativeDev manages PHP versions only through Sury while this repository is configured.",
                "muted", wrap=True,
            ))
            if data["sury_migration_needed"]:
                self.repo_card.append(label(
                    "Some installed PHP packages still appear to come from the previous system provider. Complete the one-way migration to normalize them to Sury.",
                    "error-text", wrap=True,
                ))
                migrate = Gtk.Button(label="Complete Sury migration")
                migrate.add_css_class("suggested-action")
                migrate.connect(
                    "clicked",
                    lambda *_: confirm(
                        self.window,
                        "Complete Sury PHP migration?",
                        "NativeDev will reinstall the currently installed PHP runtimes from Sury candidates, preserve the current default where possible, and reconcile *.test. Debian PHP will not be offered as a second provider.",
                        lambda: self.action(migrate, self.context.controller.enable_sury_multi_php, success_message="Sury PHP migration completed", after=self.refresh),
                    ),
                )
                self.repo_card.append(migrate)
        else:
            self.repo_card.append(label("No PHP runtime is installed. You can install Debian system PHP or enable Sury multi-version PHP.", "muted", wrap=True))
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            debian = Gtk.Button(label="Install Debian PHP")
            debian.connect(
                "clicked",
                lambda *_: confirm(
                    self.window,
                    "Install Debian PHP?",
                    "NativeDev will install Debian's default PHP CLI/FPM and the standard local-development extension baseline, then configure its *.test FPM pool.",
                    lambda: self.action(debian, self.context.controller.install_debian_php, success_message="Debian PHP installed", after=self.refresh),
                ),
            )
            actions.append(debian)
            sury = Gtk.Button(label="Enable Sury Multi-PHP")
            sury.set_sensitive(data["sury_supported"] and self.context.distro.is_debian_family)
            sury.add_css_class("suggested-action")
            sury.connect(
                "clicked",
                lambda *_: confirm(
                    self.window,
                    "Enable Sury and install PHP?",
                    "NativeDev will configure Sury and install its newest available PHP runtime with the standard local-development extension baseline. While Sury is active, Debian PHP will not be offered as another provider.",
                    lambda: self.action(sury, self.context.controller.enable_sury_multi_php, success_message="Sury multi-PHP enabled", after=self.refresh),
                ),
            )
            actions.append(sury)
            self.repo_card.append(actions)

    def _build_versions(self, data):
        self._remove_all(self.versions_card)
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title_row.append(label("PHP versions", "section-title"))
        if data["cli"]:
            title_row.append(status_pill(f"Default {data['cli']}", True))
        self.versions_card.append(title_row)
        if not data["versions"]:
            message = "No PHP versions are currently installed for the selected provider."
            self.versions_card.append(label(message, "muted"))
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
                    if not fpm.get("config_ready"):
                        top.append(status_pill("FPM config missing", False))
                    else:
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
                            lambda: self.action(btn, lambda: self.context.controller.set_default_php(v), success_message=f"PHP {v} is now default", after=self.refresh),
                        ),
                    )
                actions.append(default)

                fpm = data["fpm"].get(version, {})
                if fpm.get("installed"):
                    if not fpm.get("config_ready"):
                        repair = Gtk.Button(label="Repair FPM")
                        repair.add_css_class("suggested-action")
                        repair.connect(
                            "clicked",
                            lambda _b, v=version, btn=repair: confirm(
                                self.window,
                                f"Repair PHP {v} FPM?",
                                f"The php{v}-fpm package is installed, but its master configuration is missing. NativeDev will reinstall that package with dpkg's missing-conffile restore mode. Existing custom configuration is not purged or overwritten.",
                                lambda: self.action(btn, lambda: self.context.controller.repair_php_fpm(v), success_message=f"PHP {v} FPM repaired", after=self.refresh),
                            ),
                        )
                        actions.append(repair)
                    else:
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
                            disable = Gtk.Button(label="Disable & Stop")
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
                        lambda: self.action(btn, lambda: self.context.controller.uninstall_php(v), success_message=f"PHP {v} uninstalled", after=self.refresh),
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
                        lambda: self.action(btn, lambda: self.context.controller.install_php(v), success_message=f"PHP {v} installed", after=self.refresh),
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


class PhpExtensionsPage(Page):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.selected_version: str | None = None
        self.body.append(
            page_header(
                "PHP Extensions",
                "Install, remove, enable or disable extensions for one PHP version. CLI and FPM are always changed together.",
                self.refresh,
            )
        )
        self.version_card = card()
        self.extensions_card = card()
        self.body.append(self.version_card)
        self.body.append(self.extensions_card)
        self.refresh()

    def refresh(self):
        self._replace(self.version_card, [label("Loading PHP versions…", "muted")])
        self._replace(self.extensions_card, [label("Loading extension state…", "muted")])

        def collect():
            versions = sorted(
                self.context.php_extensions.installed_versions(),
                key=self.context.php._version_key,
                reverse=True,
            )
            selected = self.selected_version if self.selected_version in versions else ""
            cli = self.context.php.cli_version()
            if not selected and cli in versions:
                selected = cli
            if not selected and versions:
                selected = versions[0]
            states = self.context.php_extensions.states(selected) if selected else []
            return {
                "provider": self.context.php.provider(),
                "versions": versions,
                "selected": selected,
                "states": states,
            }

        def done(data):
            self.selected_version = data["selected"] or None
            self._build_version_selector(data)
            self._build_extensions(data)
            return False

        self.worker.submit(collect, done, lambda exc: self.window.set_activity(False, str(exc), error=True))

    def _build_version_selector(self, data):
        children = [label("PHP version", "section-title")]
        versions = data["versions"]
        if not versions:
            children.append(label("Install a PHP runtime before managing extensions.", "muted", wrap=True))
            self._replace(self.version_card, children)
            return

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        version_labels = [f"PHP {version}" for version in versions]
        dropdown = Gtk.DropDown.new_from_strings(version_labels)
        selected_index = versions.index(data["selected"])
        dropdown.set_selected(selected_index)
        row.append(dropdown)
        provider_name = {"debian": "Debian", "sury": "Sury"}.get(data["provider"], data["provider"].title())
        row.append(status_pill(provider_name, data["provider"] in {"debian", "sury"}))
        children.append(row)
        children.append(label(
            "Every action applies to CLI and FPM together. Refresh only reads state; it never re-enables an extension you disabled.",
            "muted", wrap=True,
        ))

        def changed(widget, _param):
            index = widget.get_selected()
            if index < 0 or index >= len(versions):
                return
            version = versions[index]
            if version == self.selected_version:
                return
            self.selected_version = version
            self.refresh()

        dropdown.connect("notify::selected", changed)
        self._replace(self.version_card, children)

    def _build_extensions(self, data):
        children: list[Gtk.Widget] = [label("Extensions", "section-title")]
        version = data["selected"]
        if not version:
            children.append(label("No PHP version selected.", "muted"))
            self._replace(self.extensions_card, children)
            return

        category = None
        for state in data["states"]:
            spec = state.spec
            if spec.category != category:
                category = spec.category
                heading = label(category, "section-title")
                heading.set_margin_top(6)
                children.append(heading)

            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
            row.add_css_class("service-row")
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            copy.set_hexpand(True)
            copy.append(label(spec.title, "row-title"))
            detail = "Built into this PHP runtime" if state.built_in else state.package
            copy.append(label(detail, "muted"))
            if spec.note:
                copy.append(label(spec.note, "muted", wrap=True))
            top.append(copy)

            if state.built_in:
                top.append(status_pill("Built-in", True))
            elif state.installed and state.enabled:
                top.append(status_pill("Installed · Enabled", True))
            elif state.installed:
                top.append(status_pill("Installed · Disabled", None))
            elif state.installable:
                top.append(status_pill("Available", None))
            else:
                top.append(status_pill("Unavailable", False))
            row.append(top)

            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            if not state.built_in:
                if not state.installed and state.installable:
                    install = Gtk.Button(label="Install")
                    install.add_css_class("suggested-action")
                    install.connect(
                        "clicked",
                        lambda _b, v=version, key=spec.key, title=spec.title, btn=install: confirm(
                            self.window,
                            f"Install {title} for PHP {v}?",
                            f"NativeDev will install the version-specific APT package, enable its modules for CLI and FPM together, validate PHP {v} FPM, then reload it when running.",
                            lambda: self.action(
                                btn,
                                lambda: self.context.php_extensions.install(v, key),
                                success_message=f"{title} installed and enabled for PHP {v}",
                                after=self.refresh,
                            ),
                        ),
                    )
                    actions.append(install)
                elif state.installed:
                    if state.enabled:
                        disable = Gtk.Button(label="Disable")
                        disable.connect(
                            "clicked",
                            lambda _b, v=version, key=spec.key, title=spec.title, btn=disable: self.action(
                                btn,
                                lambda: self.context.php_extensions.disable(v, key),
                                success_message=f"{title} disabled for PHP {v}",
                                after=self.refresh,
                            ),
                        )
                        actions.append(disable)
                    else:
                        enable = Gtk.Button(label="Enable")
                        enable.add_css_class("suggested-action")
                        enable.connect(
                            "clicked",
                            lambda _b, v=version, key=spec.key, title=spec.title, btn=enable: self.action(
                                btn,
                                lambda: self.context.php_extensions.enable(v, key),
                                success_message=f"{title} enabled for PHP {v}",
                                after=self.refresh,
                            ),
                        )
                        actions.append(enable)

                    uninstall = Gtk.Button(label="Uninstall")
                    uninstall.add_css_class("destructive-action")
                    uninstall.connect(
                        "clicked",
                        lambda _b, v=version, key=spec.key, title=spec.title, package=state.package, btn=uninstall: confirm(
                            self.window,
                            f"Uninstall {title} from PHP {v}?",
                            f"NativeDev will APT-remove {package} after a dependency safety preflight. It does not purge PHP configuration. CLI and FPM lose the extension together.",
                            lambda: self.action(
                                btn,
                                lambda: self.context.php_extensions.uninstall(v, key),
                                success_message=f"{title} uninstalled from PHP {v}",
                                after=self.refresh,
                            ),
                        ),
                    )
                    actions.append(uninstall)
            if actions.get_first_child():
                row.append(actions)
            children.append(row)

        children.append(label(
            "Core/runtime modules such as JSON, OpenSSL, PDO and Session are intentionally not exposed as removable packages here.",
            "muted", wrap=True,
        ))
        self._replace(self.extensions_card, children)

    @staticmethod
    def _replace(container: Gtk.Box, children: list[Gtk.Widget]):
        while child := container.get_first_child():
            container.remove(child)
        for child in children:
            container.append(child)


class NodePage(Page):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.body.append(page_header("Node.js", "One active Node provider: Debian system Node or NVM multi-version Node.", self.refresh))
        self.nvm_card = card()
        self.node_card = card()
        self.body.append(self.nvm_card)
        self.body.append(self.node_card)
        self.refresh()

    def refresh(self):
        self._replace(self.nvm_card, [label("Checking Node provider…", "muted")])
        self._replace(self.node_card, [label("Loading Node.js state…", "muted")])

        def collect():
            provider = self.context.node.provider()
            nvm_installed = self.context.node.installed()
            releases = []
            lts_error = ""
            if provider == "nvm" and nvm_installed:
                try:
                    releases = self.context.node.available_lts()
                except Exception as exc:
                    lts_error = str(exc)
            removal_impact = []
            if self.context.node.system_node_installed():
                try:
                    removal_impact = self.context.node.system_removal_impact()
                except Exception as exc:
                    removal_impact = [f"Could not calculate APT impact: {exc}"]
            return {
                "provider": provider,
                "system_installed": self.context.node.system_node_installed(),
                "system_version": self.context.node.system_node_version(),
                "system_npm": self.context.node.system_npm_installed(),
                "removal_impact": removal_impact,
                "nvm_installed": nvm_installed,
                "nvm": self.context.node.nvm_version() if nvm_installed else "",
                "current": self.context.node.current_node(),
                "default": self.context.node.default_node() if nvm_installed else "",
                "versions": self.context.node.installed_versions() if nvm_installed else [],
                "lts": releases,
                "lts_error": lts_error,
                "rc": str(self.context.node.shell_rc()),
            }

        def done(data):
            self._build_provider(data)
            self._build_node_versions(data)
            return False

        self.worker.submit(collect, done, lambda exc: self.window.set_activity(False, str(exc), error=True))

    def _build_provider(self, data):
        provider = data["provider"]
        names = {"debian": "Debian", "nvm": "NVM", "none": "Not configured"}
        children = [label("Node provider", "section-title")]
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.append(status_pill(names.get(provider, provider.title()), provider in {"debian", "nvm"}))
        if provider == "debian" and data["system_version"]:
            row.append(label(f"System {data['system_version']}", "muted"))
        elif data["nvm_installed"]:
            row.append(label(f"NVM {data['nvm'] or 'installed'} · Shell: {data['rc']}", "muted"))
        children.append(row)

        if provider == "debian":
            children.append(label(
                "NativeDev is using Debian's system Node.js. Multi-version Node is available by explicitly migrating to NVM.",
                "muted", wrap=True,
            ))
            if data["removal_impact"]:
                children.append(label(
                    "NVM migration is blocked because APT would also remove manually installed package(s): " + ", ".join(data["removal_impact"]),
                    "error-text", wrap=True,
                ))
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            migrate = Gtk.Button(label="Enable NVM Multi-Node")
            migrate.add_css_class("suggested-action")
            migrate.set_sensitive(not data["removal_impact"])
            migrate.connect(
                "clicked",
                lambda *_: confirm(
                    self.window,
                    "Enable NVM multi-Node?",
                    f"NativeDev will remove Debian Node.js{('/npm' if data['system_npm'] else '')} and its automatically installed Debian Node dependency stack, then install/configure NVM, install an NVM-managed LTS runtime and set it as default. Manually installed packages are never removed implicitly.",
                    lambda: self.action(migrate, self.context.controller.enable_nvm_multi_node, success_message="NVM multi-Node enabled", after=self.refresh),
                ),
            )
            actions.append(migrate)
            remove = Gtk.Button(label="Uninstall System Node")
            remove.add_css_class("destructive-action")
            remove.set_sensitive(not data["removal_impact"])
            remove.connect(
                "clicked",
                lambda *_: confirm(
                    self.window,
                    "Uninstall Debian Node.js?",
                    "NativeDev will remove Debian nodejs/npm and their automatically installed Debian Node dependency packages. Migration is blocked if APT would remove another manually installed package.",
                    lambda: self.action(remove, self.context.node.uninstall_system_node, success_message="Debian Node.js removed", after=self.refresh),
                ),
            )
            actions.append(remove)
            children.append(actions)

        elif provider == "nvm":
            children.append(label(
                "NVM multi-version Node is active. NativeDev manages Node versions through NVM and does not offer Debian Node as a second provider.",
                "muted", wrap=True,
            ))
            if data["system_installed"]:
                if data["removal_impact"]:
                    children.append(label(
                        "A Debian Node installation is still present, but cleanup is blocked because APT would also remove manually installed package(s): " + ", ".join(data["removal_impact"]),
                        "error-text", wrap=True,
                    ))
                else:
                    children.append(label(
                        "A Debian Node installation is still present from before NVM. Complete the migration to remove that system runtime.",
                        "error-text", wrap=True,
                    ))
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            shell_btn = Gtk.Button(label="Configure shell")
            shell_btn.connect("clicked", lambda *_: self.action(shell_btn, self.context.node.configure_shell, success_message="Shell integration configured"))
            actions.append(shell_btn)
            if data["system_installed"]:
                cleanup = Gtk.Button(label="Complete NVM migration")
                cleanup.add_css_class("suggested-action")
                cleanup.set_sensitive(not data["removal_impact"])
                cleanup.connect(
                    "clicked",
                    lambda *_: confirm(
                        self.window,
                        "Complete NVM migration?",
                        "NativeDev will remove the remaining Debian nodejs/npm packages, keep the NVM runtimes, ensure an NVM default, and retain NativeDev's NVM shell integration.",
                        lambda: self.action(cleanup, self.context.controller.enable_nvm_multi_node, success_message="NVM migration completed", after=self.refresh),
                    ),
                )
                actions.append(cleanup)
            children.append(actions)

        else:
            children.append(label("No Node.js runtime is installed. You can install Debian system Node or enable NVM multi-version Node.", "muted", wrap=True))
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            debian = Gtk.Button(label="Install Debian Node")
            debian.connect(
                "clicked",
                lambda *_: confirm(
                    self.window,
                    "Install Debian Node.js?",
                    "NativeDev will install Debian's nodejs package and npm when available.",
                    lambda: self.action(debian, self.context.node.install_system_node, success_message="Debian Node.js installed", after=self.refresh),
                ),
            )
            actions.append(debian)
            nvm = Gtk.Button(label="Enable NVM Multi-Node")
            nvm.add_css_class("suggested-action")
            nvm.connect(
                "clicked",
                lambda *_: confirm(
                    self.window,
                    "Enable NVM and install Node LTS?",
                    "NativeDev will install NVM for your user, install the latest LTS runtime and configure the NativeDev shell block. While NVM is active Debian Node will not be offered as another provider.",
                    lambda: self.action(nvm, self.context.controller.enable_nvm_multi_node, success_message="NVM multi-Node enabled", after=self.refresh),
                ),
            )
            actions.append(nvm)
            children.append(actions)

        self._replace(self.nvm_card, children)

    def _build_node_versions(self, data):
        provider = data["provider"]
        node = [label("Node.js versions", "section-title")]
        if provider == "debian":
            if data["system_version"]:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                title = label(data["system_version"], "row-title")
                title.set_hexpand(True)
                row.append(title)
                row.append(status_pill("Installed · Debian · Default", True))
                node.append(row)
            else:
                node.append(label("Debian Node.js is not currently usable.", "muted"))
            self._replace(self.node_card, node)
            return

        if provider != "nvm":
            node.append(label("Choose a Node provider to manage versions.", "muted"))
            self._replace(self.node_card, node)
            return

        summary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        summary.append(status_pill(f"Default: {data['default'] or 'none'}", bool(data["default"])))
        if data["current"]:
            summary.append(status_pill(f"Current: {data['current']}", True))
        node.append(summary)
        if data["lts_error"]:
            node.append(label(f"Could not refresh remote LTS list: {data['lts_error']}", "error-text", wrap=True))

        installed = set(data["versions"])
        release_names = {release.version: release.codename for release in data["lts"]}

        # Installed versions are deliberately rendered first; remote releases are
        # discovery/install choices and belong below the actual machine state.
        for version in data["versions"]:
            node.append(self._node_version_row(version, release_names.get(version, "Installed"), installed, data["default"]))

        available = [release for release in data["lts"] if release.version not in installed]
        if available:
            node.append(label("Available LTS releases", "section-title"))
            for release in available:
                node.append(self._node_version_row(release.version, release.codename, installed, data["default"]))
        elif not data["versions"]:
            node.append(label("No NVM-managed Node.js versions found.", "muted"))

        self._replace(self.node_card, node)

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
        if is_installed and default_version == version:
            top.append(status_pill("Installed · Default", True))
        else:
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
                        btn, lambda: self.context.node.set_default(v), success_message=f"Node {v} is now default", after=self.refresh
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
                    btn, lambda: self.context.node.install_version(v), success_message=f"Node {v} installed", after=self.refresh
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
                if spec.service and state.service_available:
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
                    if spec.service and state.service_available:
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
                                "NativeDev removes this component's server/client runtime packages. Configuration and database data are not purged.",
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
            # Refresh is intentionally read-only. ACL/package changes belong to
            # the explicit Nginx site configuration action, never page loading.
            projects = self.context.localdev.projects()
            default_php = self.context.localdev.default_php_version()
            fpm_versions = self.context.php.installed_fpm_versions()
            rows = []
            for project in projects:
                rows.append(
                    {
                        "path": project,
                        "root": self.context.localdev.document_root(project),
                        "prefs": self.context.localdev.project_preferences(project),
                        "readable_error": "",
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
                lambda: self.context.controller.set_project_php(project, value),
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
                    "NativeDev will add its own NetworkManager dnsmasq snippets and reload only NetworkManager DNS configuration. It will not restart NetworkManager or overwrite /etc/resolv.conf.",
                    lambda: self.action(dns_btn, self.context.localdev.configure_dns, success_message="Wildcard DNS configured", after=self.refresh),
                ),
            )
            dns.append(dns_btn)
            self._replace(self.dns_card, dns)

            sites = [label("Wildcard Nginx routing", "section-title")]
            sites.append(status_pill("Automatic routing ready" if data["nginx"] else "Not configured / legacy config", data["nginx"]))
            sites.append(
                label(
                    f"After one-time setup, a new folder named with lowercase letters, numbers or hyphens inside {self.context.localdev.park_dir} "
                    f"is immediately available as folder.{self.context.config.domain} — NativeDev does not need to be open. "
                    "A public/ directory is selected automatically when present. Default PHP is used unless a project is pinned on the Projects page.",
                    "muted",
                    wrap=True,
                )
            )
            if data["projects"]:
                sites.append(label(f"{len(data['projects'])} projects currently detected.", "muted", wrap=True))
            site_btn = Gtk.Button(label="Repair / rebuild wildcard routing" if data["nginx"] else "Configure wildcard routing")
            site_btn.add_css_class("suggested-action")
            site_btn.connect(
                "clicked",
                lambda *_: confirm(
                    self.window,
                    "Configure NativeDev wildcard Nginx routing?",
                    "NativeDev will install one persistent *.test router, prepare an inheritable read-only Nginx ACL on the park directory, validate with nginx -t, and reload Nginx. New projects will not require regeneration.",
                    lambda: self.action(site_btn, self.context.localdev.configure_nginx_sites, success_message="Wildcard Nginx routing ready", after=self.refresh),
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
            park_value = str(Path(park.get_text()).expanduser())

            def apply_settings():
                self.context.config.park_dir = park_value
                self.context.config.domain = value
                self.context.config.save()

            self.action(
                save,
                apply_settings,
                success_message="Settings saved",
                after=self.refresh,
            )

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
        ("local", "Local development", LocalDevPage),
        ("services", "Services & tools", ServicesPage),
        ("php", "PHP", PhpPage),
        ("extensions", "PHP Extensions", PhpExtensionsPage),
        ("node", "Node.js", NodePage),
        ("projects", "Projects", ProjectsPage),
        ("doctor", "Doctor", DoctorPage),
    )

    def __init__(self, application: Gtk.Application, context: AppContext):
        super().__init__(application=application)
        self.context = context
        self.worker = Worker()
        self.set_title(f"NativeDev {__version__}")
        self.set_default_size(1040, 700)
        self.set_size_request(760, 520)

        header = Gtk.HeaderBar()
        title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title.append(label("NativeDev", "app-title"))
        title.append(label(f"Native Debian development manager · {__version__}", "muted"))
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
