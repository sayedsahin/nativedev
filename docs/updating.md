# Updating NativeDev

NativeDev has one release channel: GitHub Releases on `sayedsahin/nativedev`.

## Background check

When installed as a native package, NativeDev checks the public GitHub Releases API in the background **at most once every 24 hours**:

```text
Application starts
       |
  check timestamp
       |
    >= 24h?
       |
GitHub latest release API
       |
  compare version
       |
 show update dialog
```

This check is unprivileged, read-only, and does not run APT or open a Polkit prompt. If no release has been published yet, NativeDev simply records that the check happened and shows nothing. A newer `vX.Y.Z` release is only offered when it contains the exact expected Debian asset `nativedev_X.Y.Z_all.deb` **and** GitHub reports a SHA-256 digest for that asset — a release missing either is treated as not yet ready to offer.

## Update flow

```text
GitHub Release
      |
nativedev_X.Y.Z_all.deb
      |
   download
      |
verify SHA-256 digest
      |
  Polkit action
      |
 install package
      |
restart NativeDev
```

Clicking **Update** crosses the privilege boundary as the fixed semantic `application.update` action — there is no client-controlled URL, path, package name, repository, or command involved. The root helper:

1. Independently re-queries the same fixed GitHub repository (it does not trust a URL from the GUI process).
2. Downloads only the expected NativeDev `.deb` asset.
3. Verifies GitHub's reported SHA-256 digest against the downloaded file.
4. Verifies the `.deb`'s own `Package`, `Version`, and `Architecture` fields.
5. Installs the verified local package with APT, using the same immediate dpkg-lock-failure semantics used everywhere else (see [Security model](security.md)).

After a successful install, NativeDev offers **Restart NativeDev** so the new application code and the new root-owned helper are loaded together — the two are always shipped and updated as a matched pair (see the protocol-versioning note in the [Security model](security.md)).

## Distro independence

Release discovery itself is distro-independent. Debian/Ubuntu via APT is the currently implemented package installer backend; future RPM/DNF or Pacman backends can select their own release asset without changing the 24-hour check/update UI policy described above.

## Manually checking for a release

If you don't want to wait for the periodic check, the [GitHub Releases page](https://github.com/sayedsahin/nativedev/releases) for the repository always reflects the latest published `.deb`, if any has been published yet.
