# Security Model

## Principle

> The GUI never executes arbitrary root commands. It requests semantic operations.

Allowed, by design:

```text
php.ini.apply
database.postgresql.ensure_cluster
apt.remove
application.update
```

Not allowed, by design:

```text
sudo <run this arbitrary command>
run this arbitrary SQL
write to this arbitrary file path
```

## Process boundary

- The GTK4 GUI runs as the normal desktop user at all times.
- A packaged installation places the privileged helper at `/usr/lib/nativedev/privileged_helper.py`, owned by root and not writable by the user.
- The first privileged action in a session launches that helper through a dedicated installed Polkit action. Authorization is then reused for the rest of the application session, so the user is not re-prompted for every single privileged click.
- Running from a source checkout (`./run.sh`) explicitly opts into the source-tree helper for development only; installed builds never fall back to a user-writable helper.

## What the privileged helper accepts

The helper accepts **structured NativeDev operations**, not client-supplied command argv. Every package name, service unit, file path, or SQL identifier the GUI sends is validated **again independently on the root side** — the helper does not trust that the GUI already validated it. Concretely:

- `subprocess` calls throughout the codebase use argv lists; there is no `shell=True` anywhere in the tree.
- Database usernames and passwords are checked against fixed regular expressions before ever reaching a SQL statement (see below); there is no client-supplied SQL.
- INI directive names are validated with `^[a-zA-Z][a-zA-Z0-9_.]*$`; values containing a newline, carriage return, or NUL byte are rejected outright, which prevents one configuration value from injecting a second directive line.
- NVM shell integration is the one place shell-sourced integration is used at all, and every argument involved there is shell-quoted.

## Protocol versioning

The GUI and the privileged helper communicate using a versioned RPC protocol number. A native package update ships the GUI and the root-owned helper together; if the two ever disagree on protocol version (for example, a GUI process that outlived a helper upgrade), the mismatch **fails closed** — the operation is refused rather than attempting to execute a request the helper might interpret differently than intended.

## APT-specific protections

- **Non-interactive, no artificial timeout.** Every APT invocation runs non-interactively. Removal operations are configured with `DPkg::Lock::Timeout=0`: if dpkg is already busy, the operation fails **immediately** and reports the real lock owner, instead of hanging behind another package manager process for an arbitrary wall-clock window. A removal that does start is allowed to take as long as its package scripts legitimately need.
- **Removal-impact simulation.** Before removing a PHP version, a PHP extension, or migrating away from System Node, NativeDev runs `apt-get -s remove` (a dry run) against exactly the packages it intends to remove, then cross-references the packages APT reports it would remove against `apt-mark showmanual`. If APT would sweep away a manually installed package that was not part of the original request — most commonly because a generic PHP dependency package like `php-cli`/`php-mbstring` is pinned to one specific PHP version at the OS packaging level — the operation is blocked and the affected package(s) are named explicitly, rather than removed as an unannounced side effect.

## Database access

- No client-supplied SQL string ever reaches the database. Usernames and passwords are validated against fixed patterns before any SQL is constructed:
  - Usernames: `^[A-Za-z_][A-Za-z0-9_.-]{0,31}$`
  - Passwords: printable ASCII from a fixed allowed set, explicitly excluding quote characters and backslashes, so a password value can never terminate or extend a SQL statement.
- Local database access is scoped to development-appropriate privileges rather than full administrative control — see [Services & databases](features/services-and-databases.md) for the exact grants.

## Update mechanism

Choosing **Update** in the GUI crosses the privilege boundary as a single fixed semantic action, `application.update`, with no client-controlled URL, path, package name, repository, or command. The root helper independently re-queries the same fixed GitHub repository itself; it does not trust a URL supplied by the GUI process. It downloads only the expected NativeDev `.deb` asset, verifies GitHub's reported SHA-256 digest against the downloaded file, and also verifies the `.deb`'s own `Package`, `Version`, and `Architecture` fields before installing it with APT (again under the same immediate dpkg-lock-failure semantics described above). See [Updating NativeDev](updating.md) for the full check/download/verify/install flow.

## What this model does not cover

- The privileged helper trusts that it is genuinely being launched via Polkit with the packaged, root-owned file — it does not defend against a compromised root account or a modified system package.
- NativeDev does not sandbox the services it manages (Nginx, PHP-FPM, MariaDB, etc.) beyond what the distribution's own packaging already does; NativeDev's job is orchestration, not hardening those services themselves.
- Reporting a security issue: please open a GitHub issue on `sayedsahin/nativedev`, or check the repository for a dedicated security-contact policy if one has been published since this page was written.
