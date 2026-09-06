#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  elif command -v pkexec >/dev/null 2>&1; then
    pkexec "$@"
  else
    echo "sudo or pkexec is required to uninstall NativeDev" >&2
    exit 1
  fi
}

if command -v dpkg-query >/dev/null 2>&1 && dpkg-query -W -f='${db:Status-Abbrev}' nativedev 2>/dev/null | grep -q '^ii '; then
  run_root apt-get remove -y nativedev
else
  # Backward-compatible cleanup for the old source-copy installer. Remove only
  # the two core Local Development integrations, matching the native package.
  if [ -f "$ROOT/src/nativedev/package_lifecycle.py" ]; then
    run_root env PYTHONPATH="$ROOT/src" python3 -m nativedev.package_lifecycle cleanup-localdev || true
  fi
  rm -rf "$HOME/.local/share/nativedev/src"
  if [ -f "$HOME/.local/bin/nativedev" ] && grep -q '/\.local/share/nativedev' "$HOME/.local/bin/nativedev" 2>/dev/null; then
    rm -f "$HOME/.local/bin/nativedev"
  fi
  rm -f "$HOME/.local/share/applications/io.github.nativedev.Manager.desktop"
  if [ -f /usr/share/polkit-1/actions/io.github.nativedev.policy ]; then
    run_root rm -f /usr/share/polkit-1/actions/io.github.nativedev.policy
  fi
  if [ -f /usr/lib/nativedev/privileged_helper.py ]; then
    run_root rm -f /usr/lib/nativedev/privileged_helper.py
  fi
fi

echo "NativeDev application package removed."
echo "NativeDev wildcard DNS and park-directory Nginx routing are removed by the package lifecycle hook."
echo "Projects, database data/accounts, standalone services/tools, localhost tool routes, Mailpit, and NativeDev PHP INI configuration are intentionally preserved."
