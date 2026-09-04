# Changelog

## 0.1.9 - 2026-09-03

- Simplified PHP Extensions rows: normal states no longer show redundant `Available` / `Installed · Enabled` / `Installed · Disabled` pills. The far-right action buttons communicate those states; only read-only **Built-in** and **Unavailable** rows retain a status pill.
- Added runtime-driven PHP pre-release detection. Alpha/beta/RC/dev runtimes show a compact **Pre-release** indicator in the PHP Extensions version header.
- Replaced the mutation-time `Working…` status text with a GTK spinner in the status bar; success and error messages still appear when the operation finishes.
- Fixed PHP Extensions refresh after changing the global default PHP: an explicit page Refresh now reselects the current `/usr/bin/php` default, while dropdown changes and extension actions preserve the version being edited.
- Reworked extension rows so the versioned package name and action buttons stay together on the left, with the compact state pill aligned at the far right. Preserved the app-wide compact `button { min-height: 15px; }` sizing.
- PHP Extensions now shows the selected runtime's non-package core/common modules (for example JSON, OpenSSL and PDO) as read-only **Built-in** rows with no action buttons. Runtime inventory is detected from `phpX.Y -n -m` plus `phpX.Y-common`, not hard-coded per PHP release.
- Simplified extension rows to the real versioned package name plus compact status/actions on one line; the default PHP version is preselected/marked and extension status pills use reduced padding.
- Expanded the curated optional catalog with Readline, APCu, BZip2, DBA, Enchant, ODBC, Pspell, SNMP, Tidy, AMQP, MongoDB, SSH2, SMB Client, YAML, Igbinary, MessagePack and PCOV where repository candidates exist.
- Privileged RPC advanced to protocol 8 so the root helper and application share the expanded extension allowlist.
- Added a dedicated **PHP Extensions** page with per-version package management. Curated database/common/optional/integration/debugging extensions can be installed, uninstalled, enabled or disabled without changing other PHP versions.
- PHP extension actions treat CLI and FPM as one state: Install enables both, Enable/Disable changes both transactionally, and Uninstall removes the selected version-specific package after an APT manual-dependency safety preflight. FPM configuration is validated and a running FPM service is reloaded (restart fallback) after module changes.
- Added optional SOAP, LDAP, IMAP, GMP, Redis, Memcached, Imagick and Xdebug management alongside the existing framework baseline. Redis/Memcached PHP extensions are labelled separately from their system services. PHP 8.5+ OPcache is shown as runtime-built-in rather than a removable package.
- Privileged RPC introduced curated `php.extension_*` operations in protocol 7. Extension IDs, package mapping and CLI+FPM module changes are validated again by the root helper; refresh remains read-only and never re-enables extensions the user disabled.
- Fixed native service uninstall cleanup: PostgreSQL now removes installed versioned server/client runtimes (for example `postgresql-17` and `postgresql-client-17`) even when the meta-packages were already removed; MariaDB removes its server/client core runtimes; both continue to use APT remove rather than purge so database data/configuration are preserved.
- Merged Redis Server and `redis-cli` into one Redis component. Install/remove now manages `redis-server` and `redis-tools` together.
- Protocol 6 added narrow removal-only authorization for versioned PostgreSQL and MariaDB core runtime packages.
- Replaced per-project Nginx server generation with a persistent wildcard router. After one-time setup, new lowercase directories under the park become `name.test` immediately, `public/` is detected at request time, and only per-project PHP pins require Nginx reconciliation. The park receives an inheritable read/traverse ACL so this works even while NativeDev is closed.
- Added native Debian PHP/Node provider discovery. Existing distro PHP can be configured for NativeDev `*.test` through the per-user FPM pool, and existing Debian Node remains untouched until the user explicitly chooses multi-version management.
- Added one-way provider migrations: **Debian PHP → Sury Multi-PHP** and **Debian Node → NVM Multi-Node**. Sury/NVM mode no longer exposes a switch back to Debian or a second Debian runtime choice. Leftover old-provider state is presented only as an incomplete migration to normalize.
- PHP migration enables Sury first and replaces compatible versioned PHP packages in place, preserving reverse dependencies instead of uninstalling Debian PHP first.
- Node migration performs an APT removal simulation and blocks when unrelated packages would be removed; an approved migration removes Debian `nodejs`/`npm`, installs NVM/LTS, and attempts to restore Debian Node if migration fails.
- PHP version installation now includes a Laravel/Symfony-friendly extension baseline. PHP versions before 8.5 include the separate OPcache package; PHP 8.5+ does not request one.
- Fixed installed-but-disabled/missing PHP modules after reinstall. The explicit PHP Install flow restores missing UCF-managed `mods-available/*.ini` definitions with `UCF_FORCE_CONFFMISS=1`, then enables the baseline for both CLI and FPM. User module choices made later are not changed by normal refresh/service operations.
- PHP uninstall now discovers and removes every installed package scoped to that PHP version, including extensions installed after the original NativeDev install.
- PHP and Node version lists now render installed versions before available versions. PHP FPM Disable now means **Disable & Stop**, while CLI PHP remains independent.
- Privileged RPC is now protocol 8. Protocol 5 introduced restricted PHP package/module operations and Debian Node migration; protocol 6 added removal-only PostgreSQL/MariaDB runtime cleanup; protocol 7 added curated PHP-extension operations; protocol 8 expands the root-side extension allowlist and keeps client/helper catalogs synchronized. Client/helper mismatches fail closed.
- Expanded regression coverage to 63 tests, including one-way provider semantics, migration safeguards, wildcard routing, service-runtime cleanup, installed-first ordering, UCF module restoration, per-version PHP extension management, catalog/helper consistency, and privileged protocol operations.

## 0.1.8 - 2026-09-02

- Added `NativeDevController` as the application/orchestration layer. PHP default/install/uninstall/repair and per-project PHP selection now reconcile already-managed Nginx state instead of leaving `*.test` routing stale.
- Added global mutation serialization: GTK read-only probes keep a small concurrent pool, while every system-changing action uses one mutation queue plus the controller's re-entrant mutation lock.
- Reworked PHP-FPM repair to avoid `apt purge`. Repair now uses `apt-get install --reinstall` with dpkg `--force-confmiss`, restoring missing package-owned conffiles while preserving existing custom configuration. Fresh PHP installs no longer purge retained `rc` package state.
- Replaced raw privileged `argv` RPC with protocol-versioned structured operations. The root helper constructs commands itself and narrows APT access to NativeDev components/PHP packages, service access to NativeDev-managed services, and file access to NativeDev-owned paths.
- Production installs now require the root-owned helper and install a dedicated Polkit action. Source-tree helper execution is available only through the explicit `./run.sh` development opt-in.
- Expanded the regression suite from 22 to 26 tests, including controller reconciliation, mutation serialization, non-destructive PHP repair, semantic privileged RPC, and raw-command rejection.

## 0.1.7 - 2026-09-01

- Fixed the clean-machine PHP page when `/usr/bin/php` is absent by checking CLI PHP availability before invoking it.
- Made the PHP refresh flow explicitly detect Sury first; parallel-version metadata is queried only when Sury is configured.
- Added a package-removal race fallback so PHP disappearing during refresh is treated as "not installed" instead of a GUI error.
- Source installs now remove cached Python bytecode, and release ZIPs no longer ship `__pycache__` or `.pyc` files.
- The window/header now shows the running NativeDev version, making stale desktop installs immediately visible during debugging.

## 0.1.6 - 2026-09-01

- Fixed PHP page crash when the `php` executable is completely absent after uninstall.
- Missing executables now return a normal command result with exit code 127 instead of leaking `FileNotFoundError` into the GUI.
- PHP parallel-version discovery is shown only when Sury is configured; installed PHP versions remain manageable without Sury.
- Added a clear Sury setup prompt when no PHP versions are available.

## 0.1.5 - 2026-09-01

- DNS reliability: stop restarting the entire NetworkManager service when configuring `*.test`; use targeted `nmcli general reload conf` + `dns-full` operations instead. Verify wildcard resolution after apply and rollback NativeDev-owned DNS files if configuration fails.
- Make the **Projects** page refresh strictly read-only. ACL/package mutations now happen only during the explicit Nginx site configuration flow, so opening/refreshing the page cannot trigger `pkexec` or install `acl`.
- Harden generated Nginx site configuration: quote document-root paths safely (including paths with spaces), refuse unexpected objects at the NativeDev enablement path, and restore both site content and prior symlink state when `nginx -t` rejects a new configuration.
- Correct local HTTPS key permissions to root-only mode `0600`; the privileged Nginx master process loads the key, so `www-data` workers do not need world-readable access.
- Normal `install.sh` installs the restricted privileged helper as root-owned `/usr/lib/nativedev/privileged_helper.py`; installed NativeDev prefers that immutable helper while source-tree `./run.sh` remains usable for development.
- Persist `~/.config/nativedev/config.json` atomically with `fsync` + `os.replace` and mode `0600`.
- Update regression tests for targeted DNS reloads, read-only refresh, Nginx path quoting, TLS key mode, and the privileged-helper allowlist.

## 0.1.4 - 2026-09-01

- Add a NativeDev-owned per-user PHP-FPM pool for every PHP version used by local sites; PHP-FPM workers run as the logged-in developer so CLI- and browser-created application files share one Unix owner. Debian/Sury's default `www` pool is never modified.
- Restore the per-project **Projects** page and per-project PHP-FPM version dropdown (`Default (X.Y)` plus installed FPM versions), now routed to the developer-owned socket instead of the distro's `www-data` pool.
- Remove the per-project **Safe write / Full write** file-permission choice and its ACL machinery. It is no longer needed: PHP now runs as the developer, so it can already read and write anywhere it normally could from a terminal. Nginx keeps a minimal, automatic **read-only** ACL scoped to each project's document root (plus ancestor traverse), only so it can serve static files and resolve `try_files` directly.
- Fix: the mkcert HTTPS private key was installed `root:root` mode `0600`, which Nginx's `www-data` worker cannot read; `nginx -t` failed for every HTTPS-enabled site. Installed as mode `0644` now (a local-only, self-signed leaf key; mkcert's actual root CA key is untouched).
- Fix: `*.test` sites are generated per-project again, so a project can pin an older PHP-FPM version instead of every site sharing one globally-typed version.
- Regression tests restored/extended: guard against the Projects page, per-project PHP dropdown, and file-permission ACLs from silently disappearing again; guard against the HTTPS key mode regressing to `0600`.

## 0.1.3 - 2026-09-01

- Add a dedicated Projects sidebar/page with one row per parked `*.test` project.
- Remove the global PHP-FPM version field; projects set to `Default` follow the system default PHP version when matching FPM is installed.
- Add per-project PHP-FPM dropdowns with `Default (X.Y)` plus installed FPM versions and live Nginx regeneration/rollback.
- Add per-project permission dropdowns with `Safe write` as the default and `Full write` as an explicit local-development option.
- Add ACL-based project permissions for `www-data`; Safe mode keeps source read-only while allowing common runtime/cache/upload directories to be written.
- Automatically install the Debian `acl` package when permission management first needs `setfacl`.

> Superseded by 0.1.4: this release still ran `*.test` PHP through Debian/Sury's `www-data` FPM pool, which is a different Unix user than the terminal/Composer/framework CLI. That identity split caused framework cache/rate-limit directories written by the CLI to be unreadable by the browser-facing PHP process. 0.1.4 fixes this at the architecture level instead of patching around it with wider ACLs.

## 0.1.2 - 2026-09-01

- Cache system authorization for the lifetime of the NativeDev app using a persistent, restricted root helper started once through `pkexec`.
- Add install/uninstall symmetry for system components and individual PHP versions.
- Split Redis Server and `redis-cli` management; protect `redis-tools` from removal while Redis Server depends on it.
- Add start/stop/restart and enable/disable controls for systemd-backed components and PHP-FPM versions.
- Replace PHP `Use CLI` with a `Default` button that is disabled for the active CLI version.
- Load all NVM LTS generations and provide per-version Install, Uninstall and Default actions.
- Add privilege-helper allowlist and regression tests.

## 0.1.1 - 2026-08-31

- Fixed GTK4 confirmation dialogs by using the `secondary-text` property instead of the unbound varargs `format_secondary_text()` API.
- Explicitly require GDK 4.0 before importing `Gdk`, removing the PyGI version warning.

## 0.1.0 - 2026-08-31

- Initial GTK4 runnable MVP.
- Debian-family detection and APT/systemd abstraction.
- Sury PHP discovery/setup and PHP version management.
- NVM/Node LTS/current management.
- Nginx, Redis, Memcached, MariaDB, MySQL detection, PostgreSQL, Composer and mkcert.
- NetworkManager `*.test` integration.
- NativeDev-owned Nginx site generation and mkcert HTTPS.
- Read-only Doctor page and asynchronous GTK operations.
