# NativeDev Documentation

**NativeDev** is a graphical control plane for a native PHP development stack on Linux — a **Python + PyGObject + GTK4** desktop application that manages the Nginx, PHP-FPM, database, Node.js and developer-tool services already provided by your Linux distribution, instead of replacing them with a private container/runtime stack.

> **Status:** pre-release / active development. Review privileged changes before running NativeDev on an important workstation. See the repository [CHANGELOG](../CHANGELOG.md) for the current version and recent changes.

This `docs/` folder is the detailed reference. The repository root [README.md](../README.md) stays short and links here.

## Contents

| Page | What it covers |
|---|---|
| [Getting started](getting-started.md) | Supported distros, installing the `.deb`, running from source, first launch |
| [Architecture](architecture.md) | GUI → Controller → Managers → privileged helper model, how the pieces fit together |
| [PHP management](features/php.md) | System PHP vs. Multi-PHP, versions, extensions, per-version settings, FPM pools |
| [Node.js management](features/nodejs.md) | System Node vs. NVM, LTS versions, shell integration |
| [Services & databases](features/services-and-databases.md) | Nginx, MariaDB/PostgreSQL, Redis, Memcached, Composer, mkcert, credentials |
| [Local development](features/local-development.md) | Project parking, wildcard `*.test` routing, HTTPS, per-project PHP versions |
| [Developer tools](features/developer-tools.md) | phpMyAdmin, Adminer and other persistent `*.localhost` tools |
| [Security model](security.md) | Privilege separation, the Polkit helper, what the GUI can and cannot ask root to do |
| [Updating NativeDev](updating.md) | The 24-hour release check and the update flow |
| [Uninstalling](uninstalling.md) | What is removed vs. intentionally preserved |
| [Troubleshooting](troubleshooting.md) | Common errors and what they mean |
| [FAQ](faq.md) | Short answers to recurring questions |
| [Contributing](contributing.md) | Running tests, coding conventions, submitting changes |

## Quick links

- Source repository: `sayedsahin/nativedev` on GitHub
- License: MIT (see [LICENSE](../LICENSE))
- Bug reports / feature requests: open a GitHub Issue on the repository

## Reading order

If you are new to the project, read them in this order:

1. [Getting started](getting-started.md) — install and launch NativeDev
2. [Architecture](architecture.md) — understand the mental model before changing settings
3. Whichever [feature page](#contents) matches what you want to configure
4. [Security model](security.md) and [Troubleshooting](troubleshooting.md) as reference material
