# FAQ

**Is NativeDev a container/VM tool like Docker or Laravel Sail?**
No. NativeDev deliberately does not bundle PHP, Nginx, a database engine, Redis, Node.js, containers, VMs, or a private server stack. It manages the native services your Linux distribution already provides. See [Architecture](architecture.md).

**Does NativeDev replace my distro's PHP the moment I install it?**
No. NativeDev detects and manages your existing System PHP without changing it. Multi-PHP (multiple side-by-side versions) is an explicit, one-way migration you opt into from the PHP page. See [PHP management](features/php.md).

**What happens to Composer and other tools that depend on PHP when I enable Multi-PHP?**
The migration moves existing System PHP package names to the equivalent Multi-PHP candidates in place rather than uninstalling PHP first, specifically to avoid unnecessarily removing reverse dependents such as Composer.

**Can I use MySQL instead of MariaDB?**
NativeDev exposes one **MariaDB / MySQL** service card and installs MariaDB from your distro's repositories; it does not separately provision Oracle MySQL.

**Will uninstalling NativeDev delete my databases or projects?**
No. Database data/accounts and your projects are never removed by application uninstall, and most native services (PHP, Nginx, databases, phpMyAdmin/Adminer, etc.) are left running/installed on purpose. See [Uninstalling](uninstalling.md) for the exact list.

**Why did uninstalling a PHP version get blocked?**
Most likely a developer tool (phpMyAdmin/Adminer) is bound to that version, or APT's own package dependencies would remove one of those tools as a side effect. See [Troubleshooting](troubleshooting.md).

**Does NativeDev support Fedora/Arch?**
Not yet. The current implemented backend targets Debian/Ubuntu families; the architecture is designed so additional distro backends can be added without changing the GUI or update model. See [Architecture](architecture.md) and [Getting started](getting-started.md).

**Why does an APT operation fail immediately instead of waiting?**
NativeDev configures removal operations with `DPkg::Lock::Timeout=0` on purpose: if dpkg is already busy, you get an immediate, accurate error instead of an indefinite silent wait. See [Security model](security.md).

**Is my database password stored in plain text?**
It's stored in `~/.config/nativedev/database-credentials.json` with file mode `0600` (readable only by you). See [Services & databases](features/services-and-databases.md).

**Can the GUI run arbitrary root commands?**
No. Every privileged action is a fixed, named operation re-validated on the root side; there is no generic "run this as root" path. See [Security model](security.md).

**How do I report a bug or request a feature?**
Open a GitHub Issue on `sayedsahin/nativedev`.
