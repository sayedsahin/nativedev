# Changelog

## 0.1.3 - 2026-09-01

- Add a dedicated Projects sidebar/page with one row per parked `*.test` project.
- Remove the global PHP-FPM version field; projects set to `Default` follow the system default PHP version when matching FPM is installed.
- Add per-project PHP-FPM dropdowns with `Default (X.Y)` plus installed FPM versions and live Nginx regeneration/rollback.
- Add per-project permission dropdowns with `Safe write` as the default and `Full write` as an explicit local-development option.
- Add ACL-based project permissions for `www-data`; Safe mode keeps source read-only while allowing common runtime/cache/upload directories to be written.
- Automatically install the Debian `acl` package when permission management first needs `setfacl`.

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
