#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="$ROOT/dist"

while (($#)); do
  case "$1" in
    --output-dir)
      shift
      [ "$#" -gt 0 ] || { echo "--output-dir requires a directory" >&2; exit 2; }
      OUTPUT_DIR="$1"
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
command -v dpkg-deb >/dev/null 2>&1 || { echo "dpkg-deb is required to build the Debian package" >&2; exit 1; }

VERSION="$(PYTHONPATH="$ROOT/src" python3 -c 'from nativedev import __version__; print(__version__)')"
PYPROJECT_VERSION="$(python3 - "$ROOT/pyproject.toml" <<'PY'
import re
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', text)
if not match:
    raise SystemExit("pyproject.toml version not found")
print(match.group(1))
PY
)"
if [ "$VERSION" != "$PYPROJECT_VERSION" ]; then
  echo "Version mismatch: nativedev.__version__=$VERSION, pyproject.toml=$PYPROJECT_VERSION" >&2
  exit 1
fi

STAGE="$(mktemp -d -t nativedev-deb-XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$OUTPUT_DIR"

install -d -m 0755 \
  "$STAGE/DEBIAN" \
  "$STAGE/usr/bin" \
  "$STAGE/usr/lib/nativedev/app" \
  "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/polkit-1/actions" \
  "$STAGE/usr/share/doc/nativedev"

cp -a "$ROOT/src/nativedev" "$STAGE/usr/lib/nativedev/app/"
find "$STAGE/usr/lib/nativedev/app" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE/usr/lib/nativedev/app" -type f -name '*.pyc' -delete 2>/dev/null || true
find "$STAGE/usr/lib/nativedev/app" -type f -exec chmod 0644 {} +
find "$STAGE/usr/lib/nativedev/app" -type d -exec chmod 0755 {} +

install -m 0755 "$ROOT/src/nativedev/privileged_helper.py" "$STAGE/usr/lib/nativedev/privileged_helper.py"
install -m 0644 "$ROOT/data/io.github.nativedev.Manager.desktop" "$STAGE/usr/share/applications/io.github.nativedev.Manager.desktop"
install -m 0644 "$ROOT/data/io.github.nativedev.policy" "$STAGE/usr/share/polkit-1/actions/io.github.nativedev.policy"
install -m 0644 "$ROOT/README.md" "$STAGE/usr/share/doc/nativedev/README.md"
install -m 0644 "$ROOT/CHANGELOG.md" "$STAGE/usr/share/doc/nativedev/CHANGELOG.md"
install -m 0644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/nativedev/copyright"

cat > "$STAGE/usr/bin/nativedev" <<'EOF'
#!/bin/sh
export PYTHONPATH="/usr/lib/nativedev/app${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m nativedev "$@"
EOF
chmod 0755 "$STAGE/usr/bin/nativedev"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: nativedev
Version: $VERSION
Section: devel
Priority: optional
Architecture: all
Maintainer: NativeDev contributors
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, pkexec
Description: Linux-native PHP development environment manager
 NativeDev provides a GTK4 control plane for native PHP, Nginx, Node,
 databases, cache services, local development DNS and HTTPS tooling.
EOF

install -m 0755 "$ROOT/packaging/debian/postinst" "$STAGE/DEBIAN/postinst"
install -m 0755 "$ROOT/packaging/debian/prerm" "$STAGE/DEBIAN/prerm"
install -m 0755 "$ROOT/packaging/debian/postrm" "$STAGE/DEBIAN/postrm"

# Production release builds can embed the signed NativeDev APT source without
# hard-coding a deployment domain/key in the source tree. Both values are
# required together; an unsigned repository is never generated.
if [ -n "${NATIVEDEV_APT_REPO_URL:-}" ] || [ -n "${NATIVEDEV_APT_REPO_KEYRING:-}" ]; then
  if [ -z "${NATIVEDEV_APT_REPO_URL:-}" ] || [ -z "${NATIVEDEV_APT_REPO_KEYRING:-}" ]; then
    echo "NATIVEDEV_APT_REPO_URL and NATIVEDEV_APT_REPO_KEYRING must be supplied together" >&2
    exit 1
  fi
  [ -f "$NATIVEDEV_APT_REPO_KEYRING" ] || { echo "APT repository keyring not found: $NATIVEDEV_APT_REPO_KEYRING" >&2; exit 1; }
  command -v gpg >/dev/null 2>&1 || { echo "gpg is required to validate the release repository keyring" >&2; exit 1; }
  gpg --batch --show-keys "$NATIVEDEV_APT_REPO_KEYRING" >/dev/null 2>&1 || { echo "Invalid APT repository keyring" >&2; exit 1; }
  REPO_SUITE="${NATIVEDEV_APT_REPO_SUITE:-stable}"
  if [[ ! "$REPO_SUITE" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "Invalid NATIVEDEV_APT_REPO_SUITE" >&2
    exit 1
  fi
  install -d -m 0755 "$STAGE/usr/share/keyrings" "$STAGE/etc/apt/sources.list.d"
  install -m 0644 "$NATIVEDEV_APT_REPO_KEYRING" "$STAGE/usr/share/keyrings/nativedev-archive-keyring.gpg"
  cat > "$STAGE/etc/apt/sources.list.d/nativedev.sources" <<EOF
Types: deb
URIs: $NATIVEDEV_APT_REPO_URL
Suites: $REPO_SUITE
Components: main
Signed-By: /usr/share/keyrings/nativedev-archive-keyring.gpg
EOF
  chmod 0644 "$STAGE/etc/apt/sources.list.d/nativedev.sources"
fi

OUTPUT="$OUTPUT_DIR/nativedev_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$STAGE" "$OUTPUT" >/dev/null
printf '%s\n' "$OUTPUT"
