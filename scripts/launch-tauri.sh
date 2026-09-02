#!/usr/bin/env bash
# Launch the Clarp desktop shell in development.
#
# Not the debug binary directly: `tauri dev` also starts Vite (beforeDevCommand)
# and points the window at it, which is what gives hot reload. Running the
# binary on its own leaves it with no dev server to load from.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

exec npm run tauri dev -- "$@"
