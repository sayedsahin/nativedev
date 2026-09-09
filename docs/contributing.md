# Contributing

## Running from source

No PyPI dependency is required for the application runtime — NativeDev uses the distro-provided PyGObject/GTK4 bindings.

```bash
sudo apt update
sudo apt install python3 python3-gi gir1.2-gtk-4.0 pkexec
./run.sh
```

`run.sh` explicitly opts into the source-tree privileged helper; this only exists for local development, and installed builds never use a user-writable helper. See [Security model](security.md).

## Running the tests

The core test suite does not import GTK, so it runs anywhere Python 3.10+ is available — no display server required:

```bash
python3 -m unittest discover -s tests -v
```

Syntax/compile check:

```bash
python3 -m compileall -q src
```

Pushing a version tag such as `v0.2.2` triggers the release workflow, which validates the tag against the source version, runs this same test suite, builds the `.deb`, and publishes the GitHub Release — so a change that breaks the test suite also blocks a release.

## Coding conventions this project follows

These are load-bearing conventions, not style preferences — code review and the test suite both assume them:

- **No `shell=True`, ever.** All `subprocess` calls use argv lists. If a change needs to build a command from user-influenced input, build the argv list directly; do not construct a shell string.
- **Root only executes fixed, named operations.** New privileged functionality is added as a new semantic operation in `privileged_helper.py` with its own input validation on the root side — never as a way to let the GUI send an arbitrary path, package name, or SQL fragment. See [Security model](security.md) and `project-summary.md` in the repository root for the existing operation catalog.
- **Validate identifiers with fixed patterns, not blocklists.** Database usernames/passwords and PHP INI directive names are checked against fixed allow-pattern regular expressions (see `managers/database_access.py` and `managers/php.py`), not by trying to strip or escape dangerous characters after the fact.
- **Simulate destructive APT operations before running them.** Removing a PHP version, a PHP extension, or migrating away from System Node all run `apt-get -s remove` first and cross-reference the result against `apt-mark showmanual`, so a manually installed package is never swept away as an unannounced side effect. If you add a new APT removal path, follow the same `removal_impact`-style pattern used in `managers/php_extensions.py`, `managers/node.py`, and `managers/php.py` rather than calling `apt-get remove` directly.
- **Managers stay GTK-free.** Business logic belongs in `managers/`, `controller.py`, and `system.py`, all of which have no GTK import. `gui.py` should only call into that layer and render its results.
- **Cross-manager invariants live in the controller.** If a change to one manager's state needs another manager to react (for example, a PHP default change needing Nginx regenerated), add that sequencing to `NativeDevController` rather than having the GUI call both managers directly.
- **Long-running/mutating operations are serialized.** All mutations go through one global lock so two privileged operations can never race; read-only probes may run concurrently. Keep new mutating operations inside that same serialization point.
- **Fail closed, not silently.** Prefer raising a clear `RuntimeError` with an actionable message over catching an exception and continuing as if nothing happened, especially anywhere close to the privilege boundary.

## Changelog

User-visible changes are recorded in `CHANGELOG.md` at the repository root, newest first. Add an entry there alongside any behavioral change.

## Submitting a change

1. Fork the repository and branch from `main`.
2. Make your change, keeping the conventions above in mind.
3. Add or update tests under `tests/` — `python3 -m unittest discover -s tests -v` should pass with 0 failures.
4. Update `CHANGELOG.md` and any affected page under `docs/`.
5. Open a pull request against `sayedsahin/nativedev` describing the change and, for anything touching the privileged helper or an APT/systemd/database code path, explicitly what you tested it against.

## Where to look first

- [Architecture](architecture.md) — the layered model before touching any manager
- [Security model](security.md) — required reading before touching `privileged_helper.py` or `system.py`
- `project-summary.md` (repository root) — a condensed architecture handoff document with the full privileged-operation catalog
