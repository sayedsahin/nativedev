#!/usr/bin/env bash
set -u
status=0
for cmd in python3 apt-get dpkg-query systemctl pkexec; do
  if command -v "$cmd" >/dev/null 2>&1; then
    printf '✓ %s\n' "$cmd"
  else
    printf '✗ %s\n' "$cmd"
    status=1
  fi
done

if python3 - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
PY
then
  echo '✓ PyGObject + GTK4'
else
  echo '✗ PyGObject + GTK4 (install python3-gi gir1.2-gtk-4.0)'
  status=1
fi
exit "$status"
