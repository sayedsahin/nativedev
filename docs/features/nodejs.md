# Node.js Management

Node.js follows the same two-provider pattern as PHP: NativeDev starts from whatever is already on the system and offers an explicit, one-way migration for anyone who wants multiple Node versions.

## System Node vs. NVM

- **System Node** — NativeDev detects and manages an existing `nodejs`/`npm` installation without replacing it merely because the application starts.
- **NVM Multi-Node** — an explicit, one-way **Enable NVM Multi-Node** migration. Before migrating, NativeDev simulates removing System Node (the same APT dry-run approach used elsewhere) and blocks the migration if unrelated packages would also be removed. If the migration is approved, System `nodejs`/`npm` is removed, then NVM and an LTS Node runtime are installed and configured. A failed migration attempts to restore System Node rather than leaving neither provider working.
- Once NVM is present, it becomes the Node provider going forward; System Node is no longer offered as a second selectable runtime. If a leftover System Node installation is still detected afterward, it is shown only as an incomplete migration to clean up, not as a usable second runtime.

## NVM details

- NativeDev installs a pinned NVM installer version (`v0.40.6`) rather than always fetching whatever is latest, so the migration behaves the same way across machines and over time.
- Shell integration is added as a clearly marked block in your Bash/Zsh profile:

  ```text
  # >>> NativeDev NVM >>>
  ...
  # <<< NativeDev NVM <<<
  ```

  This is the one place in NativeDev where shell-sourced integration is used, and all arguments involved are shell-quoted.
- All NVM LTS generations are loaded, showing the latest patch release for each LTS codename.
- Installed NVM Node versions are rendered before available (not-yet-installed) LTS versions.
- Individual NVM-managed Node versions can be installed, uninstalled, and selected as the default.

## Related pages

- [Architecture](../architecture.md) — where `NodeManager` sits relative to the controller and the other managers
- [Security model](../security.md) — how the APT removal-impact simulation used by the NVM migration works
