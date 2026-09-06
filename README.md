# NativeDev

**NativeDev provides a graphical control plane for a native PHP development stack on Linux.**

It is a minimal **Python + PyGObject + GTK4** desktop manager that orchestrates native host services instead of replacing them with a private container/runtime stack.

NativeDev deliberately manages the services already provided by your Linux system. It does **not** bundle PHP, Nginx, databases, Redis, Node.js, containers, VMs, Electron, or a private server stack.

> Status: **0.2.0 development / pre-release MVP**. NativeDev has not had an official public release yet; review privileged changes before using it on an important workstation.

## Current target

NativeDev is designed as a Linux application with distro-specific backends. The currently implemented package/service backend supports Debian/Ubuntu families through `/etc/os-release`; Ubuntu derivatives are resolved through `UBUNTU_CODENAME` when available so their parent Ubuntu suite is used for repository decisions. Additional Linux distro backends can be added without changing the GUI/update model. The current practical GTK4/Python baseline is:

- Debian 12 (Bookworm) and Debian 13 (Trixie)
- Ubuntu 22.04 (Jammy) and 24.04 (Noble), plus derivatives based on those suites such as Linux Mint, Pop!_OS and Zorin when their base repositories provide the required GTK4 packages

NativeDev intentionally does not bundle a newer Python/GTK runtime for old distributions. If `gir1.2-gtk-4.0` is unavailable from the distro, the lightweight installer stops instead of growing a second runtime stack.

## Included in this MVP

### PHP
- Detect and manage an existing System PHP without changing its repository merely because NativeDev starts
- Use an existing System PHP-FPM installation for NativeDev `*.test` sites through a separate per-user pool
- Offer an explicit, one-way **Enable Multi-PHP** migration when multi-version PHP is wanted; once the distro-appropriate Multi-PHP repository is active NativeDev no longer offers System PHP as a second provider
- Configure Multi-PHP by distribution family: Debian uses `packages.sury.org/php` with NativeDev-owned DEB822/keyring integration; Ubuntu and Ubuntu derivatives use `ppa:ondrej/php`, with derivatives mapped to their parent Ubuntu suite through `UBUNTU_CODENAME`
- Migrate existing System PHP package names to the active Multi-PHP candidates in place rather than uninstalling first, avoiding unnecessary removal of reverse dependants such as Composer
- Discover versioned `phpX.Y-fpm` packages from the active Multi-PHP repository and render installed PHP versions before available versions
- Install CLI/FPM plus a Laravel/Symfony-friendly baseline (`bcmath`, `curl`, `gd`, `intl`, `mbstring`, MySQL/PostgreSQL/SQLite drivers, `xml`, `zip`, etc.)
- Restore missing System/Multi-PHP UCF-managed module definitions during the explicit Install operation, then enable the baseline for CLI and FPM; later manual module disables are not overridden during refresh/start/stop
- Install/enable the separate OPcache package for PHP versions before 8.5; PHP 8.5+ does not request a separate OPcache package
- Start/stop/restart and enable/disable each installed PHP-FPM version
- Uninstall a PHP version together with all currently installed `phpX.Y` / `phpX.Y-*` packages for that version
- Select the default `/usr/bin/php` with `update-alternatives`; the current Default button is disabled
- Create a NativeDev-owned per-user PHP-FPM pool for each version used by `*.test`; PHP workers run as the logged-in developer while the distribution/Multi-PHP `www` pool stays untouched
- Manage extensions from a dedicated **PHP Extensions** page with an installed-PHP version selector; the current CLI default is preselected and explicitly marked. Install/Uninstall/Enable/Disable always apply to CLI and FPM together, never as separate SAPI controls
- Show selected-version runtime/core modules such as JSON, OpenSSL, PDO and php-common modules as read-only **Built-in** inventory with no package actions
- Curated extension catalog includes database/common packages plus APCu, BZip2, DBA, Enchant, GMP, IMAP, LDAP, ODBC, Pspell, SNMP, SOAP, Tidy, Redis, Memcached, Imagick, AMQP, MongoDB, SSH2, SMB Client, YAML, Igbinary, MessagePack, PCOV and Xdebug; unavailable packages are shown but cannot be installed
- Keep package presence separate from enabled state: an installed-but-disabled extension remains installed until explicitly uninstalled, and ordinary refresh never overrides that choice. Normal rows use their far-right action buttons as the state cue; only **Built-in** and **Unavailable** rows show a status pill
- Detect alpha/beta/RC/dev PHP runtimes from the selected runtime itself and mark them **Pre-release** on the Extensions page
- Run an APT removal simulation before extension uninstall and block when another manually installed package would be removed; PHP configuration is not purged
- Manage per-version custom PHP configuration from a dedicated **PHP Settings** page without editing System/Multi-PHP `php.ini`. NativeDev owns only `/etc/php/X.Y/mods-available/nativedev.ini` plus `cli/fpm/conf.d/99-nativedev.ini`, so CLI and FPM receive the same override layer
- Validate INI directives at both the application and root-helper boundary with `^[a-zA-Z][a-zA-Z0-9_.]*$`; values containing newline, carriage return or NUL are rejected rather than stripped, preventing one value from injecting another directive line
- Keep extension loading on **PHP Extensions**: `extension`, `zend_extension` and `extension_dir` are rejected by PHP Settings. INI apply/reset validates PHP CLI + FPM, reloads FPM only when already running, and rolls back the NativeDev-owned files if validation/reload fails
- Save each applied NativeDev INI profile under `~/.config/nativedev/php/X.Y.json` (mode `0600`). PHP uninstall removes the active `/etc/php/X.Y` NativeDev layer but retains the saved profile for an explicit restore after reinstall

### Node.js
- Detect and manage an existing System `nodejs`/`npm` installation without replacing it merely because NativeDev starts
- Offer an explicit, one-way **Enable NVM Multi-Node** migration; NativeDev first simulates System Node removal and blocks the migration when unrelated APT packages would also be removed
- Remove System `nodejs`/`npm` during an approved migration, then install/configure NVM and an LTS Node runtime; failed migrations attempt to restore System Node
- Once NVM is present, NativeDev treats NVM as the Node provider and does not offer System Node as a second selectable runtime; a leftover System Node is shown only as an incomplete migration to clean up
- Install pinned NVM installer version `v0.40.6`
- Add a clearly marked NativeDev block to Bash/Zsh/profile startup config
- Load all NVM LTS generations and show the latest patch for each LTS codename
- Render installed NVM Node versions before available LTS versions
- Install/uninstall individual NVM-managed Node versions and select a default version

### Native services and tools
- Nginx
- MariaDB / MySQL (NativeDev installs MariaDB from system repositories and shows the detected MariaDB version)
- PostgreSQL
- Redis Server + `redis-cli` as one component (`redis-server` + `redis-tools`)
- Memcached
- Composer
- mkcert
- systemd start/stop/restart and enable/disable controls when the unit supports them
- Database connection cards show the conventional local endpoints `localhost:3306` and `localhost:5432`; NativeDev still uses explicit TCP internally when proving password authentication
- Database uninstall keeps the conservative APT-remove model by default: NativeDev stops/disables the service, removes the installed server/client runtime package family, and forgets NativeDev's saved database credential while preserving database data/accounts. The confirmation dialog has a default-unchecked **Delete all database data and accounts** option; selecting it removes the default MariaDB datadir or PostgreSQL cluster data/config so a later install starts with fresh database accounts. Common/shared packages are still not purged/autoremoved.
- Database removal does not use an arbitrary short wall-clock timeout. APT is run non-interactively and is configured not to wait behind an already-busy dpkg lock; a busy package manager is reported immediately, while a removal that actually starts may take as long as its legitimate package scripts require. The explicit database-data reset follows the same no-artificial-timeout rule.
- After a destructive PostgreSQL reset, NativeDev explicitly repairs the distro cluster lifecycle before account provisioning: it creates/starts a `main` cluster when none exists, or starts the configured port-5432 cluster when it is down. This avoids an installed-but-unusable PostgreSQL state with no local socket/listener.
- After NativeDev installs MariaDB or PostgreSQL, automatically provision a database account named after the logged-in Unix developer (for example `sayed`) with default database password `nativedev`; no second confirmation/setup step is required
- Show the NativeDev-managed database server/port/current-user username/password in Services with Reveal/Copy controls, plus Change password and Reset to default actions (`Reset` restores only the database password to `nativedev`)
- Store NativeDev database credential metadata in `~/.config/nativedev/database-credentials.json` with mode `0600`; a full database uninstall removes that saved credential
- Never silently overwrite a pre-existing unmanaged database account matching the current Unix username during discovery/install reconciliation; legacy database roles named `nativedev` from the earlier model are left untouched
- **Use existing user** requires the current DB password to match. NativeDev verifies it and stores the credential without changing the account password; future Change/Reset operations are self-service and need no database-admin access. A mismatch changes nothing. **Use NativeDev default account** never asks for the old user password; it uses the privileged database-admin path to create/reset the current-user DB account to password `nativedev`, then verifies that password before saving.
- MySQL/MariaDB local access receives development capabilities (database/schema/table/view/routine/trigger CRUD) without `GRANT OPTION` or user administration; PostgreSQL uses `LOGIN CREATEDB` with `NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS`

### Projects / local development
- Park one projects directory (default `~/Code`)
- Scan first-level project directories and expose a dedicated **Projects** page
- Configure one persistent wildcard Nginx router: after one-time setup, creating a lowercase DNS-safe folder such as `~/Code/my-app` makes `my-app.test` available immediately without reopening NativeDev or regenerating Nginx
- Changing **Park directory** or **Local TLD** from Local development → Save settings automatically reconciles existing NativeDev routing. TLD changes update NetworkManager wildcard DNS, existing Nginx routing, and an enabled NativeDev HTTPS certificate; park changes rebuild the wildcard router/ACL for the new location. Failed reconciliation rolls back the saved settings.
- Resolve the project directory dynamically from the hostname and use `public/` automatically when present, otherwise project root
- Use the system default PHP-FPM automatically; no global PHP-FPM field is required
- Per-project PHP dropdown: `Default (X.Y)` plus installed PHP-FPM versions
- Route `*.test` PHP requests to each project's own `/run/php/phpX.Y-fpm-nativedev-UID.sock`, not the distro `www-data` FPM pool
- Grant Nginx a read-only ACL on existing document roots and an inheritable read/traverse ACL on the configured park directory so projects created later work immediately; PHP itself never needs an ACL since it already runs as the developer
- Install the system `acl` package automatically the first time that read grant is needed
- Generate `/etc/nginx/sites-available/nativedev-sites.conf` only for Local Development wildcard park routing. Persistent `*.localhost` Developer Tool routes live separately in `/etc/nginx/conf.d/nativedev-tools.conf` and do not depend on the configured project TLD or park directory
- Quote generated Nginx document-root paths safely, including project directories containing spaces
- Validate with `nginx -t` before reload and restore both the previous site file and enablement state on failure
- Configure `*.test -> 127.0.0.1` using **NetworkManager-managed dnsmasq**
- Apply DNS changes with targeted `nmcli general reload` operations instead of restarting NetworkManager
- Verify wildcard resolution after applying DNS changes and restore NativeDev-owned DNS files if setup fails
- Never overwrite `/etc/resolv.conf`
- Generate a wildcard mkcert certificate and configure HTTPS in NativeDev sites

### Safety / ownership
- GUI runs as the normal user
- Packaged installation places the privileged helper at `/usr/lib/nativedev/privileged_helper.py` as a root-owned, non-user-writable file
- The first privileged action launches that restricted helper through a dedicated installed Polkit action; authorization is reused for the rest of the app session. `./run.sh` explicitly opts into the source-tree helper for development only.
- The privileged helper accepts **structured NativeDev operations**, not client-supplied command argv. Package/service/file targets are validated again on the root side; it is not an arbitrary root shell.
- GUI and helper use privileged RPC protocol **23** in this tree. The native package updates the GUI and root-owned helper together, so stale protocol versions fail closed instead of executing an incompatible privileged request.
- `subprocess` calls use argv lists; no generic `shell=True`
- NVM is the only shell-sourced integration, with shell-quoted arguments
- NativeDev writes distinct, named configuration files instead of editing unrelated user configs
- The GUI confirms system-changing operations
- Long-running operations run off the GTK main thread; read-only probes may run concurrently, while all mutations are serialized through one global queue/lock. Mutations show a GTK spinner instead of a `Working…` message, then surface the final success/error text.

## Screens

The GTK4 UI has seven top-level sidebar destinations:

1. Dashboard
2. Local development
3. Services & tools
4. PHP
5. Node.js
6. Projects
7. Doctor

**PHP Extensions** and **PHP Settings** are contextual subpages opened from PHP rather than separate sidebar destinations. Each subpage provides a direct back action to PHP.

It uses GTK CSS only; no libadwaita, WebView, Node/Electron, Qt, database, or extra Python UI framework.

## Run from the ZIP

Install the runtime dependencies on a Debian/Ubuntu-family desktop:

```bash
sudo apt update
sudo apt install python3 python3-gi gir1.2-gtk-4.0 pkexec
```

Then:

```bash
./run.sh
```

## Native package install

NativeDev is still pre-release, but its install workflow is already native-package based. The current development builder produces one architecture-independent Debian package because the NativeDev application itself is Python/GTK:

```bash
./packaging/build-deb.sh --output-dir dist
```

Install the resulting package with your normal Debian/Ubuntu package manager:

```bash
sudo apt install ./dist/nativedev_0.2.0_all.deb
```

For source-tree convenience, `./install.sh` builds that same `.deb` and installs it. It no longer copies application source into `~/.local`. The package owns `/usr/bin/nativedev`, `/usr/lib/nativedev/app`, the root-owned privileged helper, desktop file and Polkit policy.

Run:

```bash
nativedev
```

Production release builds can embed a signed NativeDev APT repository by supplying `NATIVEDEV_APT_REPO_URL` and `NATIVEDEV_APT_REPO_KEYRING` to `packaging/build-deb.sh`. The generated DEB refuses to configure an unsigned repository.

## Application updates

NativeDev has one release channel. When installed as a native package, the application checks the native package metadata in the background at most once every 24 hours. The background probe is read-only and never opens a Polkit prompt. When a newer package candidate is visible, NativeDev shows an update dialog.

Choosing **Update** performs a fixed semantic privileged operation: NativeDev refreshes APT metadata and upgrades only the `nativedev` package. Package names, repositories, URLs and arbitrary commands are never supplied by the GUI. After the package is replaced, NativeDev offers **Restart NativeDev** so the new application code and privileged helper are loaded together.

Debian/Ubuntu is the first package backend; the update manager is intentionally backend-oriented so DNF/RPM or Pacman support can be added later without changing the 24-hour UI policy.

## Uninstall

```bash
sudo apt remove nativedev
```

or from a source checkout:

```bash
./uninstall.sh
```

Application/package files are removed. The package removal hook removes only NativeDev's two core Local Development integrations: the NetworkManager wildcard DNS snippets and `/etc/nginx/sites-available/nativedev-sites.conf` park router (plus its NativeDev symlink). Park contents and ACLs are preserved.

NativeDev acts as the management UI for standalone services/tools, so uninstall intentionally preserves their working state: PHP versions and NativeDev INI overrides, Nginx/MariaDB/PostgreSQL/Redis/RabbitMQ, Mailpit, phpMyAdmin/Adminer packages and runtime configuration, Adminer SQLite, and `/etc/nginx/conf.d/nativedev-tools.conf`. Reinstalling NativeDev can detect/manage those existing components again. Database data/accounts and user projects are never removed by application uninstall.

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

NativeDev uses explicitly scoped system integration. Debian Multi-PHP uses NativeDev-owned Sury files; Ubuntu-family Multi-PHP delegates the Ondřej PPA source/key entry to `software-properties` with a fixed parent-Ubuntu suite:

```text
/etc/apt/sources.list.d/nativedev-sury-php.sources
/etc/NetworkManager/conf.d/nativedev-dns.conf
/etc/NetworkManager/dnsmasq.d/nativedev-test.conf
/etc/nginx/sites-available/nativedev-sites.conf
/etc/nginx/sites-enabled/nativedev-sites.conf
/etc/nginx/conf.d/nativedev-tools.conf
/etc/nginx/nativedev/nativedev.pem
/etc/nginx/nativedev/nativedev-key.pem
/etc/php/X.Y/fpm/pool.d/nativedev-UID.conf
```

On Ubuntu/Ubuntu derivatives, `software-properties` may create the Ondřej PPA source/key files under `/etc/apt/sources.list.d/` and `/etc/apt/keyrings/`; NativeDev identifies that repository by its fixed Launchpad URI rather than by a hard-coded filename.

User state:

```text
~/.config/nativedev/config.json
~/.config/nativedev/database-credentials.json
~/.local/share/nativedev/
/var/lib/nativedev/phpmyadmin/
/var/lib/nativedev/adminer-sqlite/
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
- Generic Redis/MariaDB/PostgreSQL configuration editors are not implemented yet; the current development build installs/detects/controls their native services and manages a local database login matching the current Unix developer user, but does not yet expose server tuning forms. Nginx/local DNS/HTTPS configuration is implemented.
- Site scanning is refresh-based, not a persistent filesystem daemon.
- PHP requests for `*.test` run as the logged-in developer, which avoids CLI-vs-FPM ownership conflicts for cache/uploads/rate-limit directories. Nginx still needs read/traverse permission to serve static files directly under the project's document root; NativeDev grants that automatically via a read-only ACL scoped to the document root only, and never broadens permissions on the rest of the project or the home directory.
- Project ACL management assumes normal development projects are owned by the desktop user; files owned by another account may require ownership repair outside NativeDev.
- NativeDev exposes one **MariaDB / MySQL** service and installs MariaDB from system repositories; it does not separately provision Oracle MySQL.

## Architecture

```text
GTK4 GUI / future CLI
   |
   +-- AppContext
          |
          +-- NativeDevController -- serialized mutations + cross-manager reconciliation
          |        |
          |        +-- PhpManager ------ APT / systemd / Multi-PHP
          |        +-- LocalDevManager - wildcard DNS / park Nginx / mkcert
          |        +-- DeveloperToolManager - persistent localhost Nginx tools
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

The core managers do not depend on GTK. Cross-manager invariants (for example, PHP default/pin changes requiring Nginx regeneration) live in `NativeDevController`, so a future CLI can reuse the same mutation semantics.

## Recommended next milestones

- Managed configuration forms for Redis, Memcached, MariaDB and PostgreSQL
- `.nvmrc` project detection
- Uninstall/reset screen for NativeDev-owned system integration
- `.deb` packaging and GitHub Actions CI
- Automated integration tests in Debian/Ubuntu VMs

## License

MIT.
