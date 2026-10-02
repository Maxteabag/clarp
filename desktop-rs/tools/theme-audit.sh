#!/usr/bin/env bash
# The reading themes' readability audit (APCA, WCAG 2, colour-vision
# separation) as Markdown: tools/theme-audit.sh [THEME...]
set -euo pipefail
cd "$(dirname "$0")/.."
exec cargo run -q -p clarp-core --example theme_audit -- "$@"
