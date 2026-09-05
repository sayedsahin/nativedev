#!/usr/bin/env bash
set -euo pipefail

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
  # Backward-compatible cleanup for the old source-copy installer.
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
echo "User projects, databases, database accounts, and NativeDev-managed system service configuration were intentionally preserved."
