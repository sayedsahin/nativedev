#!/usr/bin/env bash
set -euo pipefail
rm -rf "$HOME/.local/share/nativedev"
rm -f "$HOME/.local/bin/nativedev"
rm -f "$HOME/.local/share/applications/io.github.nativedev.Manager.desktop"
echo "NativeDev application files removed."
echo "System services and NativeDev-managed /etc configuration were intentionally left untouched."
echo "Review /etc/nginx/sites-available/nativedev-sites.conf and /etc/NetworkManager/*/nativedev* if you want to remove system integration manually."
