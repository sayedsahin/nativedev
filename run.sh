#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
# Explicit development-only opt-in: production installs require the root-owned helper.
export NATIVEDEV_ALLOW_SOURCE_HELPER=1
exec python3 -m nativedev "$@"
