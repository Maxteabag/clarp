#!/usr/bin/env bash
set -euo pipefail
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
scenario=${1:?scenario required}
output=${2:?absolute output directory required}
mkdir -p "$output"
CLARP_DESKTOP_BIN="$repo/desktop/build/release/clarp-desktop" \
CLARP_DESKTOP_DIR="$repo/desktop" CLARP_BASE_URL=http://127.0.0.1:1 CLARP_TOKEN=offline-fixture \
CLARP_SCREENSHOT_SCENARIO=markdown CLARP_DESKTOP_RESEARCH="$scenario" CLARP_DESKTOP_RESEARCH_OUTPUT="$output" \
CLARP_SCREENSHOT_DELAY_MS=4400 \
/home/peter/dotfiles/skills/qt-clarp-desktop/scripts/clarp-desktop-shot.sh "$output/capture.png" '' '' --no-new-agent
