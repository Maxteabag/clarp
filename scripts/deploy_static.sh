#!/usr/bin/env bash
# Sync client assets into the running release — no service restart.
#
# Why this exists: `make deploy` runs the full install.sh, which cuts a new
# release, copies server/lib/plugin/systemd, and restarts the HTTP service.
# Agent turns continue in clarp-runtime, but deploying the whole worktree still
# ships anything else uncommitted in it. That is not wanted when the change is
# only CSS, HTML, or client JavaScript.
#
# Why it writes into the live release rather than repointing `current`:
# server/lib/context.py resolves its root with
# `pathlib.Path(__file__).resolve()`, so the running process pins itself to
# one concrete release directory at boot. Repointing `current` has no effect
# until a restart. Static files are read per request
# (`_send_file` -> `path.read_bytes()`), so writing into the pinned release
# goes live on the very next request.
#
# Caveats, deliberately:
#   * This mutates a release directory that install.sh treats as immutable.
#     It is a development shortcut. The next real deploy replaces the whole
#     release, which is the intended way to make a change permanent — always
#     run `make deploy` before considering client work shipped.
#   * `clarp-admin rollback` to this release would carry these files. Harmless
#     while the repo is the source of truth, but worth knowing.
#   * Server-side Python changes are NOT picked up. Use `make deploy` for those.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

SHARE="${CLAUDE_PWA_SHARE:-$HOME/.local/share/clarp}"
SRC="static"
DEST="$SHARE/current/static"

if [[ ! -d "$SRC" ]]; then
    echo "error: no ./$SRC in $(pwd) — run this from the repo root" >&2
    exit 1
fi

# static/index.html and static/app/ are Vite output from web/. Build before
# syncing so a source edit that was never built cannot ship as a stale bundle.
# Skipped when node is absent: the committed build output is still valid, and
# the deploy target does not need a toolchain.
if command -v npm >/dev/null 2>&1 && [[ -d node_modules/vite ]]; then
    echo ">> building client (vite)"
    npm run --silent build | sed 's/^/   /'
else
    echo ">> skipping client build (no npm or node_modules) — shipping committed output"
fi

if [[ ! -d "$DEST" ]]; then
    echo "error: $DEST does not exist." >&2
    echo "       Is claude-pwa installed? Run ./install.sh once first." >&2
    exit 1
fi

release="$(readlink -f "$SHARE/current" || true)"
echo ">> syncing $SRC/ -> $DEST"
echo "   release: ${release:-unknown}"

if command -v rsync >/dev/null 2>&1; then
    # --delete so the installed tree matches the repo, the same as a real
    # deploy would. Only ./static is in scope, so nothing outside it moves.
    rsync -a --delete --itemize-changes "$SRC/" "$DEST/" | sed 's/^/   /'
else
    echo "   (rsync not found — falling back to cp -a, stale files will linger)"
    cp -a "$SRC/." "$DEST/"
fi

# The service is deliberately NOT restarted. Report that it is still the same
# process, so an accidental bounce is visible.
if command -v systemctl >/dev/null 2>&1; then
    pid="$(systemctl --user show clarp.service -p MainPID --value 2>/dev/null || true)"
    state="$(systemctl --user is-active clarp.service 2>/dev/null || true)"
    echo ">> service untouched: ${state:-unknown} (PID ${pid:-?})"
fi

cat <<'EOF'
>> done — the next request serves the new assets.

   Open clients: /sw.js is served with the newest static mtime baked into
   its cache name, so the service worker picks the change up on its next
   update check. Ctrl+Shift+R forces it immediately.

   This is a dev shortcut: run `make deploy` to cut a real release.
EOF
