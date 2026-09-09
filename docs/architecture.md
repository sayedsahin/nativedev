# Architecture

## Design philosophy

> Use native Linux packages and services, then provide a modern GUI management layer around them.

NativeDev does not bundle PHP, Nginx, a database engine, Redis, Node.js, containers, VMs, Electron, or a private server stack. It orchestrates the equivalent native services your distribution already provides (APT packages, systemd units, `update-alternatives`, NVM) through a GTK4 desktop application. The GUI itself never runs GTK/PyGObject-heavy work for the actual service management — that logic lives in plain-Python managers with no GTK dependency, so the same mutation semantics could be reused by a future CLI.

## Layered model

```text
GTK4 GUI / future CLI
   |
   +-- AppContext
          |
          +-- NativeDevController -- serialized mutations + cross-manager reconciliation
          |        |
          |        +-- PhpManager ------------ APT / systemd / Multi-PHP
          |        +-- LocalDevManager ------- wildcard DNS / park Nginx / mkcert
          |        +-- DeveloperToolManager -- persistent localhost Nginx tools
          |        +-- DatabaseAccessManager - local DB account / credentials
          |
          +-- NodeManager -------- NVM (per user)
          +-- ServiceManager ----- APT / systemd
          +-- Doctor ------------- read-only checks
                   |
               System layer
          CommandRunner / AptManager / SystemdManager
                   |
          structured Polkit helper RPC
```

### GUI

Pure presentation. Every button/action calls into `NativeDevController` or a manager method; the GUI does not construct shell commands, SQL, or file paths itself. Long-running operations run off the GTK main thread. Read-only probes may run concurrently; all mutating operations are serialized through one global queue/lock so two privileged operations never race each other.

### `NativeDevController`

Owns the cross-manager invariants that a single manager cannot enforce on its own — for example, changing the default PHP version or uninstalling a PHP version requires regenerating Nginx configuration for both Local Development and Developer Tools. The controller sequences these steps and rolls back on failure where practical. Because the controller has no GTK dependency, a future command-line interface could drive the same operations.

### Managers

Each manager owns one functional area and talks to the system layer, never directly to `subprocess`:

- **`PhpManager`** — System PHP detection, the one-way Multi-PHP migration, version install/uninstall, extensions, per-version FPM pools, and NativeDev's own INI override layer. See [PHP management](features/php.md).
- **`LocalDevManager`** — the parked projects directory, wildcard `*.test` Nginx routing, NetworkManager DNS integration, and mkcert HTTPS. See [Local development](features/local-development.md).
- **`DeveloperToolManager`** — persistent `*.localhost` tools such as phpMyAdmin and Adminer, each bound to a PHP-FPM version. See [Developer tools](features/developer-tools.md).
- **`DatabaseAccessManager`** — the NativeDev-managed local database account, its stored credential, and password change/reset flows. See [Services & databases](features/services-and-databases.md).
- **`NodeManager`** — System Node detection, the one-way NVM migration, and NVM-managed Node versions. See [Node.js management](features/nodejs.md).
- **`ServiceManager`** — generic install/start/stop/enable/disable for native services like Nginx, MariaDB, PostgreSQL, Redis, Memcached, Composer, and mkcert.
- **`Doctor`** — read-only diagnostics across all of the above; it never mutates system state.

### System layer

`CommandRunner`, `AptManager`, and `SystemdManager` wrap `subprocess` with argv lists only — no `shell=True` anywhere in the codebase. This layer is where APT non-interactive flags, dpkg-lock-busy handling, and systemd unit control live.

### Privileged helper (Polkit)

The application ships a separate root-owned script, `privileged_helper.py`, launched through a dedicated Polkit action rather than a generic `sudo`/`pkexec <shell command>`. It exposes a fixed set of **semantic operations** — for example `php.ini.apply`, `database.postgresql.ensure_cluster`, `application.update` — and re-validates every target (package names, file paths, INI keys) on the root side. It never accepts client-supplied command argv, arbitrary file paths, or arbitrary SQL. See [Security model](security.md) for the full contract and its limits.

GUI and helper communicate using a versioned protocol number; a package update ships the GUI and the root-owned helper together, so a stale protocol version fails closed instead of executing an operation the two sides disagree about.

## Why this split exists

Putting cross-manager invariants in the controller (rather than in the GUI event handlers, or scattered across managers) keeps three properties:

1. **Testability** — managers and the controller have no GTK dependency, so the test suite exercises real logic without a display server.
2. **Safety** — the privileged helper's fixed operation set means the attack surface for "what can root be asked to do" is enumerable and reviewable, independent of what the GUI happens to expose this release.
3. **Future reuse** — a CLI or a different frontend could drive the same controller/manager layer without duplicating the privilege boundary.
