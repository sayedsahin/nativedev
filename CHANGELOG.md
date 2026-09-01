# Changelog

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
