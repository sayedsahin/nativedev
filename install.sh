#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "NativeDev's packaged installer currently supports Debian/Ubuntu-family Linux." >&2
  echo "The application update architecture is Linux-backend based; additional distro packages will be added later." >&2
  exit 1
fi

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  elif command -v pkexec >/dev/null 2>&1; then
    pkexec "$@"
  else
    echo "sudo or pkexec is required to install NativeDev" >&2
    exit 1
  fi
}

DEB="$("$ROOT/packaging/build-deb.sh" --output-dir "$ROOT/dist")"
run_root apt-get update
run_root apt-get install -y "$DEB"

# Migrate the old source-copy installer safely. A stale ~/.local/bin/nativedev
# takes PATH precedence over /usr/bin/nativedev, so remove it only when it is
# recognisably NativeDev's generated legacy launcher.
LEGACY_BIN="$HOME/.local/bin/nativedev"
if [ -f "$LEGACY_BIN" ] && grep -q '/\.local/share/nativedev' "$LEGACY_BIN" 2>/dev/null; then
  rm -f "$LEGACY_BIN"
fi
LEGACY_APP="$HOME/.local/share/nativedev/src"
if [ -d "$LEGACY_APP" ]; then
  rm -rf "$LEGACY_APP"
fi
LEGACY_DESKTOP="$HOME/.local/share/applications/io.github.nativedev.Manager.desktop"
if [ -f "$LEGACY_DESKTOP" ]; then
  rm -f "$LEGACY_DESKTOP"
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
  fi
fi

printf '\nInstalled NativeDev %s as a native Debian package.\n' "$(PYTHONPATH="$ROOT/src" python3 -c 'from nativedev import __version__; print(__version__)')"
printf 'Run: nativedev\n'
