# Uninstalling NativeDev

## Removing the application

```bash
sudo apt remove nativedev
```

or, from a source checkout:

```bash
./uninstall.sh
```

## What is actually removed

Only the application itself and its two core Local Development integrations:

- `/usr/bin/nativedev`, `/usr/lib/nativedev/app`, the privileged helper, the desktop entry, and the Polkit policy
- The NetworkManager wildcard DNS snippets
- `/etc/nginx/sites-available/nativedev-sites.conf` (the park router) and its `sites-enabled` symlink

Your parked project directory's contents and filesystem ACLs are preserved untouched.

## What is intentionally preserved

NativeDev is the management UI for standalone native services and tools — it is not their installer of record in the sense that removing NativeDev should not take those services down. Uninstalling NativeDev intentionally leaves working:

- Installed PHP versions and their NativeDev INI overrides
- Nginx, MariaDB/PostgreSQL, Redis, RabbitMQ, and Mailpit, if installed
- phpMyAdmin/Adminer packages and their runtime configuration, including Adminer's SQLite storage
- `/etc/nginx/conf.d/nativedev-tools.conf` (the persistent developer-tool routing file)
- All database data and accounts
- All of your projects

Reinstalling NativeDev later detects and can manage these existing components again, exactly as it would detect anything installed independently of NativeDev.

## If you also want the underlying services removed

Uninstalling NativeDev does not do this for you by design. Use each service's own package manager commands (`apt remove <package>`), or use NativeDev's own per-service uninstall actions (PHP versions, extensions, and databases each have a dedicated uninstall flow — see [PHP management](features/php.md) and [Services & databases](features/services-and-databases.md)) **before** removing NativeDev itself, since those flows include NativeDev's own safety checks (for example the APT removal-impact simulation described in [Security model](security.md)).
