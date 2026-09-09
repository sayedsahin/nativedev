# Local Development

The **Local development** and **Projects** pages turn a plain folder of project directories into working `*.test` sites with no per-project Nginx editing.

## Parking a projects directory

Point NativeDev at one projects directory (default `~/Code`). NativeDev scans its first-level subdirectories and lists them on the **Projects** page. After one-time setup, creating a new lowercase, DNS-safe folder such as `~/Code/my-app` makes `my-app.test` available immediately — no need to reopen NativeDev or manually regenerate Nginx configuration.

For each project, NativeDev resolves the document root dynamically from the hostname: it uses `public/` automatically when that subdirectory exists, otherwise the project root itself.

## Changing settings later

Changing **Park directory** or **Local TLD** (the `.test`-style suffix) from Local development → Save settings automatically reconciles all existing NativeDev routing:

- Changing the TLD updates NetworkManager's wildcard DNS configuration, existing Nginx routing, and an already-enabled NativeDev HTTPS certificate.
- Changing the park directory rebuilds the wildcard router and filesystem ACL for the new location.

If reconciliation fails partway through, NativeDev rolls back the settings it just saved rather than leaving a half-applied configuration.

## PHP version per project

- By default, projects use the system's default PHP-FPM version automatically — there is no global "PHP-FPM path" field to configure.
- Each project also has its own dropdown: `Default (X.Y)` plus every currently installed PHP-FPM version, for projects that need to pin to something specific.
- `*.test` PHP requests are routed to that project's own per-user socket, `/run/php/phpX.Y-fpm-nativedev-UID.sock` — never the distro's shared `www-data` FPM pool. See [PHP management](php.md) for how these pools are created.

## Filesystem permissions

PHP requests for `*.test` run as the logged-in developer (not `www-data`), which avoids CLI-vs-FPM ownership conflicts for cache, upload, and rate-limit directories that a project writes to at runtime. Nginx, however, still needs read/traverse permission to serve static files directly from the project's document root. NativeDev grants that automatically through:

- A read-only ACL scoped to each project's existing document root
- An inheritable read/traverse ACL on the configured park directory itself, so projects created *after* the initial setup work immediately without a manual permission step

NativeDev installs the system `acl` package automatically the first time this grant is needed, and never broadens permissions beyond the document root or the park directory — the rest of a project, and your home directory in general, are untouched.

Project ACL management assumes projects are owned by the desktop user; files owned by a different account may need ownership repaired outside NativeDev first.

## Nginx configuration files

- `/etc/nginx/sites-available/nativedev-sites.conf` (symlinked into `sites-enabled`) is generated only for the Local Development wildcard park router, and is regenerated whenever park/TLD/PHP-version state changes.
- Persistent `*.localhost` [developer tool](developer-tools.md) routes (phpMyAdmin, Adminer) live in a **separate** file, `/etc/nginx/conf.d/nativedev-tools.conf`, and do not depend on the configured project TLD or park directory.
- Generated document-root paths are quoted safely, including project directories that contain spaces.
- Every generated configuration is validated with `nginx -t` before reload; on failure, NativeDev restores both the previous site file and its previous enabled/disabled state.

## Wildcard DNS

`*.test -> 127.0.0.1` is configured using **NetworkManager-managed dnsmasq** specifically — this is the one resolver layout NativeDev automates. Other resolver setups are detected as unsupported rather than having their configuration rewritten. Changes are applied with targeted `nmcli general reload` calls, not a full NetworkManager restart, and `/etc/resolv.conf` is never overwritten directly. After applying a DNS change, NativeDev verifies wildcard resolution actually works and restores its own DNS files if setup failed.

## HTTPS

NativeDev generates a wildcard `mkcert` certificate and configures HTTPS for NativeDev-managed sites, so `*.test` projects can be served over TLS locally without a manually issued certificate per project.

## Related pages

- [PHP management](php.md) — installing the PHP-FPM versions a project can select
- [Developer tools](developer-tools.md) — the separate, non-project `*.localhost` routing model
- [Troubleshooting](../troubleshooting.md) — what happens if wildcard routing is reconciled with no PHP-FPM version installed
