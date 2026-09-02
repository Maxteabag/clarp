#!/usr/bin/env bash
# Pull the dSYMs for a TestFlight build into the local symbolication store so
# scripts/symbolicate_crash.py can name the frames in a MetricKit crash payload.
#
#   scripts/fetch_dsyms.sh                # newest successful testflight.yml run
#   scripts/fetch_dsyms.sh 33428943594    # a specific run id
#
# dSYMs land in ~/.local/share/clarp-dsyms/<run-id>/ and stay there; the store
# is indexed by binary UUID at symbolication time, so old builds keep working.
set -euo pipefail

STORE="${CLARP_DSYM_STORE:-$HOME/.local/share/clarp-dsyms}"
RUN_ID="${1:-}"

if ! command -v gh >/dev/null; then
  echo "error: gh CLI not found" >&2
  exit 2
fi

if [[ -z "$RUN_ID" ]]; then
  echo "Looking up the newest testflight.yml run..."
  RUN_ID="$(gh run list --workflow=testflight.yml --limit 1 \
    --json databaseId --jq '.[0].databaseId')"
  if [[ -z "$RUN_ID" || "$RUN_ID" == "null" ]]; then
    echo "error: could not resolve a run id. If gh reports 404, check" >&2
    echo "       'gh auth status' and the token's repo scope." >&2
    exit 2
  fi
fi

DEST="$STORE/$RUN_ID"
mkdir -p "$DEST"
echo "Fetching dSYMs from run $RUN_ID into $DEST"

# The artifact name carries the run number, not the run id, so glob for it.
gh run download "$RUN_ID" --pattern 'clarp-dsyms-*' --dir "$DEST"

if [[ -f "$DEST"/*/dsym-uuids.txt ]] 2>/dev/null; then
  cat "$DEST"/*/dsym-uuids.txt
fi

echo
echo "Store now holds:"
find "$STORE" -name '*.dSYM' -maxdepth 4 2>/dev/null | sed 's|^|  |'
echo
echo "Symbolicate the newest crash with:"
echo "  scripts/symbolicate_crash.py --latest"
