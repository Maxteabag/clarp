#!/usr/bin/env bash
# Screenshots of every reading theme (or those named) over a long reply,
# each from its own headless run against the fake Host:
# tools/theme-shots.sh OUT [THEME...]  (build clarp-slint first)
set -uo pipefail
cd "$(dirname "$0")/.."
out=$(realpath -m "$1"); shift
ids=("$@")
[ ${#ids[@]} -gt 0 ] || mapfile -t ids < <(python3 -c 'import json; [print(t["id"]) for t in json.load(open("core/src/reading_themes.json"))]')
status=0
for id in "${ids[@]}"; do
    CLARP_CHECK_THEMES=$id slint-app/tests/check.sh themes "$out" || status=1
done
exit $status
