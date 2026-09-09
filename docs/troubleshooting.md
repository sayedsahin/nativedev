# Troubleshooting

## "Could not get lock /var/lib/dpkg/lock-frontend. It is held by process ..."

**What it means:** another package manager process (a background `apt-get`/`apt`/`unattended-upgrades` run, or another terminal) is currently holding the dpkg lock. NativeDev's APT operations are deliberately configured to fail immediately in this situation (`DPkg::Lock::Timeout=0`) rather than hang silently behind another process for an unpredictable amount of time. See [Security model](security.md) for why this is intentional.

**What to do:** wait for the other package-manager process to finish (check `ps aux | grep -i apt` if you're not sure what's running), then retry the same action in NativeDev.

**A note on partial state:** for a whole-PHP-version uninstall specifically, NativeDev detaches its own FPM pool and stops/disables the `phpX.Y-fpm` service *before* calling APT, so that a lock failure at the APT step can leave that version's FPM service stopped/disabled even though the packages themselves are still installed. If you hit the lock error during a PHP uninstall, re-enabling and starting `phpX.Y-fpm` yourself (or simply retrying the uninstall once the lock clears) returns things to a consistent state.

## "PHP X.Y is currently used by: Adminer, phpMyAdmin. Choose another PHP version for these Developer Tools before uninstalling it."

**What it means:** the PHP version you're trying to uninstall is the one a [developer tool](features/developer-tools.md) (phpMyAdmin and/or Adminer) is currently bound to. NativeDev blocks the uninstall here rather than leaving that tool pointed at a PHP-FPM socket that's about to disappear.

**What to do:** open the affected tool's settings and switch its PHP-FPM version selector to a different installed version, then retry the PHP uninstall.

## "NativeDev will not uninstall PHP X.Y because APT would also remove manually installed package(s): adminer, phpmyadmin"

**What it means:** this is a deeper, independent safety check than the one above. On Debian/Ubuntu, phpMyAdmin and Adminer's own `.deb` packages depend on **generic, version-agnostic PHP packages** (for example `php-cli`, `php-mbstring`) that APT itself pins to one specific PHP version at the packaging level — this pin exists **independently of** which PHP-FPM version NativeDev's Developer Tools page currently shows the tool as using. Removing that specific version's packages can make APT propose removing phpMyAdmin/Adminer as a side effect, even when NativeDev's own tracked binding for the tool points somewhere else. NativeDev simulates every PHP-version removal with `apt-get -s remove` first and blocks rather than letting APT silently take the tool package down.

**What to do:** if you actually want to remove this PHP version, uninstall the affected developer tool package(s) first (or accept that they will need reinstalling afterward), then uninstall the PHP version. Keeping the PHP version installed is the only way to keep the tool installed without touching it, since this dependency is set by the `.deb` packaging itself rather than by anything NativeDev can repoint.

## "Install and start a PHP-FPM version before configuring wildcard *.test routing"

**What it means:** Local Development's wildcard router reconciliation ran and found zero installed PHP-FPM versions on the system. This typically shows up right after uninstalling the last remaining PHP version while wildcard `*.test` routing is already enabled.

**What to do:** install at least one PHP version from the PHP page, then retry whatever triggered the reconciliation (saving Local Development settings, or simply reopening the page). Existing project files and the parked directory are not affected by this error — only the Nginx routing step failed.

## The Doctor page reports "no PHP-FPM runtime available"

**What it means:** the Doctor screen's read-only checks found a service or route that needs a working PHP-FPM version, but none is currently installed and enabled. This is the same underlying condition as the wildcard-routing error above, surfaced proactively instead of only at the point something tries to use it.

**What to do:** install and start at least one PHP version.

## A PHP extension won't uninstall: "NativeDev will not uninstall ... because APT would also remove manually installed package(s): ..."

**What it means:** the same APT removal-impact simulation described above also runs before uninstalling an individual PHP extension, not just a whole version. Some other manually installed package depends on the extension package you're trying to remove.

**What to do:** the message names the package(s) at risk; uninstall those first if you're sure you want to, or leave the extension installed.

## Still stuck?

Check the **Doctor** page first — many issues show up there with a plain-language description before they block a specific action. If that doesn't explain what you're seeing, open a GitHub issue on `sayedsahin/nativedev` with the exact error text and which page/action produced it.
