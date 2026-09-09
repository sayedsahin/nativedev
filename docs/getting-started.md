# Getting Started

## Supported systems

NativeDev implements a Debian/Ubuntu package and service backend through `/etc/os-release`. Ubuntu derivatives are resolved through `UBUNTU_CODENAME` when present, so their parent Ubuntu suite is used for repository decisions. Additional Linux distro backends can be added later without changing the GUI or update model.

The current practical GTK4/Python baseline is:

- Debian 12 (Bookworm) and Debian 13 (Trixie)
- Ubuntu 22.04 (Jammy) and 24.04 (Noble), plus derivatives (Linux Mint, Pop!_OS, Zorin, etc.) whose base repositories provide the required GTK4 packages

NativeDev intentionally does **not** bundle a newer Python/GTK runtime for older distributions. If `gir1.2-gtk-4.0` is unavailable from the distro's repositories, the installer stops rather than growing a second runtime stack alongside the system one.

## Option 1 — Install the `.deb` package (recommended)

Official builds are published as GitHub Release assets on `sayedsahin/nativedev`. Each release is triggered by pushing a version tag (for example `v0.2.2`), which runs the test suite, builds `nativedev_<version>_all.deb`, and attaches it to the GitHub Release.

```bash
# download the release asset from the GitHub Releases page, then:
sudo apt install ./nativedev_<version>_all.deb
```

This installs:

- `/usr/bin/nativedev` — the launcher
- `/usr/lib/nativedev/app` — the application code
- `/usr/lib/nativedev/privileged_helper.py` — the root-owned, non-user-writable privileged helper
- The desktop entry and Polkit policy needed to authorize privileged actions

Launch it from your application menu, or:

```bash
nativedev
```

## Option 2 — Build the `.deb` from a source checkout

```bash
./packaging/build-deb.sh --output-dir dist
sudo apt install ./dist/nativedev_<version>_all.deb
```

`./install.sh` is a convenience wrapper that builds and installs that same package from a source checkout. It does not copy application source into `~/.local`; it installs the real package.

## Option 3 — Run directly from a source checkout (development)

Install the runtime dependencies on a Debian/Ubuntu-family desktop:

```bash
sudo apt update
sudo apt install python3 python3-gi gir1.2-gtk-4.0 pkexec
```

Then:

```bash
./run.sh
```

No PyPI dependency is required for the application runtime — NativeDev uses the distro-provided PyGObject/GTK4 bindings. `run.sh` explicitly opts into the source-tree privileged helper; this opt-in only exists for local development. Installed builds always use the root-owned package helper and never fall back to a user-writable one.

## First launch

On first launch, NativeDev inspects your system to discover what is already installed (System PHP, Nginx, databases, Node.js, etc.) and presents that state without changing anything on its own. The first action that needs root privileges (for example installing a PHP version, or enabling wildcard local development routing) triggers a single Polkit authorization prompt; that authorization is then reused for the rest of the application session.

From here:

- To manage PHP versions and extensions, see [PHP management](features/php.md)
- To set up `*.test` project routing, see [Local development](features/local-development.md)
- To install a database, Redis, or other native services, see [Services & databases](features/services-and-databases.md)

## Verifying an installation

The **Doctor** screen (see the sidebar) runs read-only checks against the current system state and reports problems in plain language — for example a missing PHP-FPM runtime, a stopped service that local routing depends on, or an incomplete migration. It changes nothing itself; use the relevant feature page to fix anything it reports.

## Uninstalling

See [Uninstalling](uninstalling.md) for exactly what a package removal deletes versus intentionally leaves in place (PHP versions, databases, and your projects are preserved by design).
