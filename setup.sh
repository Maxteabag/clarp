#!/usr/bin/env bash
# Bootstrap Clarp's locked Python runtime, then run the product wizard.
set -euo pipefail
umask 077

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

show_help() {
    cat <<'EOF'
Clarp setup

All setup interfaces call the same clarp-admin setup engine and produce the
same versioned installation, configuration, managed tools, skills, and service.

USAGE
  ./setup.sh                  Choose the TUI or interactive CLI wizard
  ./setup.sh --tui            Open the Textual setup wizard
  ./setup.sh --cli            Open the interactive terminal-question wizard
  ./setup.sh --non-interactive OPTIONS
                              Install without prompts (automation/AI friendly)
  ./setup.sh --help           Show this guide

INTERACTIVE EXAMPLES
  ./setup.sh --tui
  ./setup.sh --cli

NON-INTERACTIVE EXAMPLE
  ./setup.sh --non-interactive \
    --backend both \
    --toolchain managed \
    --transcription recommended \
    --tts cartesia \
    --tts-fallback none \
    --network tailscale \
    --optional-skill clarp-calendar \
    --optional-skill clarp-location

IMPORTANT OPTIONS
  --backend claude|codex|both
  --toolchain managed|existing|none
  --transcription recommended|none|MODEL_ID
  --tts clarp|cartesia|elevenlabs|deepgram|none
  --tts-fallback clarp|cartesia|elevenlabs|deepgram|none
  --network tailscale|lan|manual|off
  --public-url https://HOST       Required with --network manual
  --optional-skill SKILL_ID       Repeat once per optional skill
  --channel stable|development

Run `clarp-admin setup --help` after installation for the underlying command
reference. API keys can be entered privately in the TUI or later with:
  clarp-admin tts configure cartesia
  clarp-admin tts configure elevenlabs
  clarp-admin tts configure deepgram
EOF
}

MODE=""
if [[ $# -gt 0 && "$1" == "--help" ]]; then
    show_help
    exit 0
elif [[ $# -gt 0 && "$1" == "--tui" ]]; then
    MODE="tui"
    shift
    if [[ $# -gt 0 ]]; then
        echo "ERROR: --tui does not accept setup flags." >&2
        show_help >&2
        exit 2
    fi
elif [[ $# -gt 0 && "$1" == "--cli" ]]; then
    MODE="cli"
    shift
    if [[ $# -gt 0 && "$1" == "--help" ]]; then
        show_help
        exit 0
    fi
elif [[ $# -eq 0 && -t 0 && -t 1 ]]; then
    echo "Clarp can be configured through either wizard."
    echo
    echo "  1. Textual TUI wizard (recommended for people)"
    echo "  2. Interactive CLI wizard (terminal questions)"
    echo "  3. Show commands for automation / AI agents"
    echo
    read -r -p "Choose setup interface [1]: " choice
    case "${choice:-1}" in
        1) MODE="tui" ;;
        2) MODE="cli" ;;
        3) show_help; exit 0 ;;
        *) echo "ERROR: choose 1, 2, or 3." >&2; exit 2 ;;
    esac
elif [[ $# -eq 0 ]]; then
    echo "ERROR: non-terminal setup requires explicit --non-interactive options." >&2
    show_help >&2
    exit 2
else
    MODE="cli"
fi

UV="${UV:-$(command -v uv || true)}"
[[ -n "$UV" ]] || {
    echo "ERROR: uv is required. Install it from https://docs.astral.sh/uv/" >&2
    exit 1
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        echo "ERROR: sha256sum or shasum is required" >&2
        return 1
    fi
}

HOST_PLATFORM="$(uname -s)"
PLATFORM="$HOST_PLATFORM"
[[ "${CLARP_PLATFORM_OVERRIDE:-}" != "macos" ]] || PLATFORM="Darwin"
if [[ "$PLATFORM" == "Darwin" ]]; then
    if [[ "$HOST_PLATFORM" == "Darwin" ]]; then
        MACOS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
        if [[ ! "$MACOS_MAJOR" =~ ^[0-9]+$ || "$MACOS_MAJOR" -lt 14 ]]; then
            echo "ERROR: Clarp requires macOS 14 (Sonoma) or newer." >&2
            exit 1
        fi
    fi
    SHARE="${CLARP_SHARE_DIR:-$HOME/Library/Application Support/Clarp}"
else
    XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
    SHARE="${CLARP_SHARE_DIR:-$XDG_DATA_HOME/clarp}"
fi
export CLARP_SHARE_DIR="$SHARE"

"$UV" lock --check --project "$REPO_DIR" >/dev/null
ENV_ID="$(sha256_file "$REPO_DIR/uv.lock")"
ENV_ID="${ENV_ID:0:20}"
ENV_DIR="$SHARE/environments/$ENV_ID"

echo ">> syncing Clarp Python environment $ENV_ID"
UV_PROJECT_ENVIRONMENT="$ENV_DIR" "$UV" sync --frozen --no-dev \
    --project "$REPO_DIR"

export UV PYTHON="$ENV_DIR/bin/python"
if [[ "$MODE" == "tui" ]]; then
    exec "$PYTHON" "$REPO_DIR/bin/clarp-tui.py" --first-run
fi
exec "$PYTHON" "$REPO_DIR/bin/clarp-admin.py" setup "$@"
