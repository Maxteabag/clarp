#!/usr/bin/env bash
# One-line Host install. People should not clone the repo.
#
#   curl -fsSL https://maxteabag.github.io/clarp-site/install.sh | bash
#
set -euo pipefail
umask 077

REPO="https://github.com/Maxteabag/clarp"
REF="${CLARP_REF:-main}"
export CLARP_SOURCE_REMOTE="${CLARP_SOURCE_REMOTE:-$REPO.git}"

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "ERROR: '$1' is required." >&2
        exit 1
    }
}

need curl
need tar
need uname

case "$(uname -s)" in
    Linux|Darwin) ;;
    *)
        echo "ERROR: Clarp installs on Linux or macOS." >&2
        exit 1
        ;;
esac

if ! command -v uv >/dev/null 2>&1; then
    echo "→ installing uv"
    curl -fsSL https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || {
    echo "ERROR: uv did not land on PATH. Open a new shell and retry." >&2
    exit 1
}

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/clarp-get.XXXXXX")"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

echo "→ downloading Clarp ($REF)"
ARCHIVE="$WORKDIR/clarp.tar.gz"
if [[ "$REF" == v* ]]; then
    URL="$REPO/archive/refs/tags/${REF}.tar.gz"
else
    URL="$REPO/archive/refs/heads/${REF}.tar.gz"
fi
curl -fsSL "$URL" -o "$ARCHIVE"

mkdir -p "$WORKDIR/src"
tar -xzf "$ARCHIVE" -C "$WORKDIR/src" --strip-components=1
cd "$WORKDIR/src"
chmod +x setup.sh install.sh

echo "→ running setup"
exec ./setup.sh "$@"
