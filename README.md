# NativeDev

**NativeDev provides a graphical control plane for a native PHP development stack on Debian-based Linux.**

It is a minimal **Python + PyGObject + GTK4** desktop manager that orchestrates native host services instead of replacing them with a private container/runtime stack.

NativeDev deliberately manages the services already provided by your Linux system. It does **not** bundle PHP, Nginx, databases, Redis, Node.js, containers, VMs, Electron, or a private server stack.

> Status: **0.1.9 alpha / runnable MVP**. Review every privileged change before using this on an important workstation.

## Current target

The distro detector accepts Debian/Ubuntu families through `/etc/os-release` (`ID` / `ID_LIKE`). The practical GTK4/Python baseline is:

- Debian 12 (Bookworm) and Debian 13 (Trixie)
- Ubuntu 22.04+ and current derivatives such as Linux Mint, Pop!_OS and Zorin when their base repositories provide the required GTK4 packages

NativeDev intentionally does not bundle a newer Python/GTK runtime for old distributions. If `gir1.2-gtk-4.0` is unavailable from the distro, the lightweight installer stops instead of growing a second runtime stack.

## Included in this MVP

### PHP
- Detect and manage an existing Debian system PHP without changing its repository merely because NativeDev starts
- Use an existing Debian PHP-FPM installation for NativeDev `*.test` sites through a separate per-user pool
- Offer an explicit, one-way **Enable Sury Multi-PHP** migration when multi-version PHP is wanted; once Sury is active NativeDev no longer offers Debian as a second PHP provider
- Resolve Ubuntu-derived base codenames via `UBUNTU_CODENAME` and configure the Sury keyring + a NativeDev-owned DEB822 source file
- Migrate existing Debian PHP package names to Sury candidates in place rather than uninstalling first, avoiding unnecessary removal of reverse dependants such as Composer
- Discover versioned `phpX.Y-fpm` packages from Sury and render installed PHP versions before available versions
- Install CLI/FPM plus a Laravel/Symfony-friendly baseline (`bcmath`, `curl`, `gd`, `intl`, `mbstring`, MySQL/PostgreSQL/SQLite drivers, `xml`, `zip`, etc.)
- Restore missing Debian/Sury UCF-managed module definitions during the explicit Install operation, then enable the baseline for CLI and FPM; later manual module disables are not overridden during refresh/start/stop
- Install/enable the separate OPcache package for PHP versions before 8.5; PHP 8.5+ does not request a separate OPcache package
- Start/stop/restart and enable/disable each installed PHP-FPM version
- Uninstall a PHP version together with all currently installed `phpX.Y` / `phpX.Y-*` packages for that version
- Select the default `/usr/bin/php` with `update-alternatives`; the current Default button is disabled
- Create a NativeDev-owned per-user PHP-FPM pool for each version used by `*.test`; PHP workers run as the logged-in developer while Debian/Sury's `www` pool stays untouched

### Node.js
- Detect and manage an existing Debian `nodejs`/`npm` installation without replacing it merely because NativeDev starts
- Offer an explicit, one-way **Enable NVM Multi-Node** migration; NativeDev first simulates Debian Node removal and blocks the migration when unrelated APT packages would also be removed
- Remove Debian `nodejs`/`npm` during an approved migration, then install/configure NVM and an LTS Node runtime; failed migrations attempt to restore Debian Node
- Once NVM is present, NativeDev treats NVM as the Node provider and does not offer Debian Node as a second selectable runtime; a leftover Debian Node is shown only as an incomplete migration to clean up
- Install pinned NVM installer version `v0.40.6`
- Add a clearly marked NativeDev block to Bash/Zsh/profile startup config
- Load all NVM LTS generations and show the latest patch for each LTS codename
- Render installed NVM Node versions before available LTS versions
- Install/uninstall individual NVM-managed Node versions and select a default version

### Native services and tools
- Nginx
- Redis Server + `redis-cli` as one component (`redis-server` + `redis-tools`)
- Memcached
- MariaDB
- MySQL when the configured APT repositories actually provide `mysql-server`
- PostgreSQL
- Composer
- mkcert
- Install/uninstall system packages; PostgreSQL/MariaDB uninstall also removes their concrete server/client runtime packages without purging database data or configuration
- systemd start/stop/restart and enable/disable controls when the unit supports them
- MariaDB/MySQL conflict guard

### Projects / local development
- Park one projects directory (default `~/Code`)
- Scan first-level project directories and expose a dedicated **Projects** page
- Configure one persistent wildcard Nginx router: after one-time setup, creating a lowercase DNS-safe folder such as `~/Code/my-app` makes `my-app.test` available immediately without reopening NativeDev or regenerating Nginx
- Resolve the project directory dynamically from the hostname and use `public/` automatically when present, otherwise project root
- Use the system default PHP-FPM automatically; no global PHP-FPM field is required
- Per-project PHP dropdown: `Default (X.Y)` plus installed PHP-FPM versions
- Route `*.test` PHP requests to each project's own `/run/php/phpX.Y-fpm-nativedev-UID.sock`, not the distro `www-data` FPM pool
- Grant Nginx a read-only ACL on existing document roots and an inheritable read/traverse ACL on the configured park directory so projects created later work immediately; PHP itself never needs an ACL since it already runs as the developer
- Install Debian's `acl` package automatically the first time that read grant is needed
- Generate only `/etc/nginx/sites-available/nativedev-sites.conf`; the file contains a wildcard/default PHP route plus explicit backend overrides only for projects pinned to another PHP version
- Quote generated Nginx document-root paths safely, including project directories containing spaces
- Validate with `nginx -t` before reload and restore both the previous site file and enablement state on failure
- Configure `*.test -> 127.0.0.1` using **NetworkManager-managed dnsmasq**
- Apply DNS changes with targeted `nmcli general reload` operations instead of restarting NetworkManager
- Verify wildcard resolution after applying DNS changes and restore NativeDev-owned DNS files if setup fails
- Never overwrite `/etc/resolv.conf`
- Generate a wildcard mkcert certificate and configure HTTPS in NativeDev sites

### Safety / ownership
- GUI runs as the normal user
- A normal `./install.sh` installation places the privileged helper at `/usr/lib/nativedev/privileged_helper.py` as a root-owned, non-user-writable file
- The first privileged action launches that restricted helper through a dedicated installed Polkit action; authorization is reused for the rest of the app session. `./run.sh` explicitly opts into the source-tree helper for development only.
- The privileged helper accepts **structured NativeDev operations**, not client-supplied command argv. Package/service/file targets are validated again on the root side; it is not an arbitrary root shell.
- GUI and helper use privileged RPC protocol **5** in this release; `install.sh` installs the matching root-owned helper so stale protocol versions fail closed instead of executing an incompatible privileged request.
- `subprocess` calls use argv lists; no generic `shell=True`
- NVM is the only shell-sourced integration, with shell-quoted arguments
- NativeDev writes distinct, named configuration files instead of editing unrelated user configs
- The GUI confirms system-changing operations
- Long-running operations run off the GTK main thread; read-only probes may run concurrently, while all mutations are serialized through one global queue/lock.

## Screens

The GTK4 UI has seven deliberately small pages:

1. Dashboard
2. Local development
3. Services & tools
4. PHP
5. Node.js
6. Projects
7. Doctor

It uses GTK CSS only; no libadwaita, WebView, Node/Electron, Qt, database, or extra Python UI framework.

## Run from the ZIP

Install the runtime dependencies on a Debian-family desktop:

```bash
sudo apt update
sudo apt install python3 python3-gi gir1.2-gtk-4.0 pkexec
```

Then:

```bash
./run.sh
```

## Local desktop install

```bash
./install.sh
```

This installs the GTK/Python runtime packages through APT, installs the restricted privileged helper as a root-owned file under `/usr/lib/nativedev`, then copies the GUI/application source into your user directories:

- `~/.local/share/nativedev`
- `~/.local/bin/nativedev`
- `~/.local/share/applications/io.github.nativedev.Manager.desktop`

Run:

```bash
nativedev
```

## Development

No PyPI dependency is required for the application runtime. Use distro-provided PyGObject/GTK4.

```bash
./run.sh
```

`run.sh` sets the explicit development-only source-helper opt-in. Installed builds do not fall back to a user-writable helper.

Core-only tests do not import GTK:

```bash
python3 -m unittest discover -s tests -v
```

Syntax check:

```bash
python3 -m compileall -q src
```

## Managed system files

NativeDev currently owns only files with explicit NativeDev names:

```text
/etc/apt/sources.list.d/nativedev-sury-php.sources
/etc/NetworkManager/conf.d/nativedev-dns.conf
/etc/NetworkManager/dnsmasq.d/nativedev-test.conf
/etc/nginx/sites-available/nativedev-sites.conf
/etc/nginx/sites-enabled/nativedev-sites.conf
/etc/nginx/nativedev/nativedev.pem
/etc/nginx/nativedev/nativedev-key.pem
/etc/php/X.Y/fpm/pool.d/nativedev-UID.conf
```

User state:

```text
~/.config/nativedev/config.json
~/.local/share/nativedev/
/usr/lib/nativedev/privileged_helper.py
```

NVM shell integration is enclosed by:

```text
# >>> NativeDev NVM >>>
...
# <<< NativeDev NVM <<<
```

## Important alpha limitations

- Automatic wildcard DNS is intentionally limited to NetworkManager. Other resolver layouts are detected as unsupported instead of rewriting resolver configuration. NativeDev reloads only NetworkManager configuration/DNS state; it does not intentionally restart the whole NetworkManager service.
- Generic Redis/MariaDB/PostgreSQL configuration editors are not implemented yet; v0.1 installs, detects and controls their native services. Nginx/local DNS/HTTPS configuration is implemented.
- Site scanning is refresh-based, not a persistent filesystem daemon.
- PHP requests for `*.test` run as the logged-in developer, which avoids CLI-vs-FPM ownership conflicts for cache/uploads/rate-limit directories. Nginx still needs read/traverse permission to serve static files directly under the project's document root; NativeDev grants that automatically via a read-only ACL scoped to the document root only, and never broadens permissions on the rest of the project or the home directory.
- Project ACL management assumes normal development projects are owned by the desktop user; files owned by another account may require ownership repair outside NativeDev.
- Oracle MySQL packaging differs between Debian-family distributions. The MVP only offers the button when APT exposes an actual `mysql-server` candidate.

## Architecture

```text
GTK4 GUI / future CLI
   |
   +-- AppContext
          |
          +-- NativeDevController -- serialized mutations + cross-manager reconciliation
          |        |
          |        +-- PhpManager ------ APT / systemd / Sury
          |        +-- LocalDevManager - NetworkManager / Nginx / mkcert
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

The core managers do not depend on GTK. Cross-manager invariants (for example, PHP default/pin changes requiring Nginx regeneration) live in `NativeDevController`, so a future CLI can reuse the same mutation semantics.

## Recommended next milestones

- Managed configuration forms for Redis, Memcached, MariaDB and PostgreSQL
- `.nvmrc` project detection
- Uninstall/reset screen for NativeDev-owned system integration
- `.deb` packaging and GitHub Actions CI
- Automated integration tests in Debian/Ubuntu VMs

## License

MIT.
