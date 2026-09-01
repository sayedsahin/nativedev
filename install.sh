#!/usr/bin/env bash
set -euo pipefail

APP_ID="nativedev"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HOME/.local/share/$APP_ID"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

if ! command -v apt-get >/dev/null 2>&1; then
  echo "NativeDev installer currently supports Debian-family systems with apt-get." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y python3 python3-gi gir1.2-gtk-4.0 pkexec
sudo install -d -m 0755 /usr/lib/nativedev
sudo install -m 0755 "$ROOT/src/nativedev/privileged_helper.py" /usr/lib/nativedev/privileged_helper.py

mkdir -p "$TARGET" "$BIN_DIR" "$DESKTOP_DIR"
rm -rf "$TARGET/src"
cp -a "$ROOT/src" "$TARGET/src"
cp "$ROOT/data/io.github.nativedev.Manager.desktop" "$DESKTOP_DIR/io.github.nativedev.Manager.desktop"

cat > "$BIN_DIR/nativedev" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$TARGET/src\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m nativedev "\$@"
EOF
chmod +x "$BIN_DIR/nativedev"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

printf '\nInstalled NativeDev.\n'
printf 'Run: %s\n' "$BIN_DIR/nativedev"
printf 'If ~/.local/bin is not on PATH, log out/in or add it to your shell PATH.\n'
