#!/usr/bin/env bash
# safe-capture.sh — bounded log/device capture that can't exhaust RAM or disk.
#
# Why this exists: a raw `idevicesyslog -u <udid> > /tmp/foo.txt` once filled
# ~4 GB. /tmp here is tmpfs (RAM-backed), the iPhone streams syslog forever,
# and nothing ever stopped it. This wrapper enforces the three guards that
# would have prevented it:
#   1. Output goes to a DISK-backed dir (never /tmp/tmpfs).
#   2. A hard size cap (head -c) — the producer gets SIGPIPE when reached.
#   3. A hard time cap (timeout) — the capture self-terminates.
# It also traps Ctrl-C so the child dies with it.
#
# Usage:
#   scripts/safe-capture.sh <name> <seconds> <max-size> -- <command...>
# Examples:
#   scripts/safe-capture.sh ios-vad 60 200M -- idevicesyslog -u "$UDID"
#   scripts/safe-capture.sh srv-journal 30 50M -- journalctl --user -u clarp.service -f
set -euo pipefail

if [ "$#" -lt 4 ]; then
  echo "usage: $0 <name> <seconds> <max-size> -- <command...>" >&2
  exit 2
fi

name="$1"; secs="$2"; maxsize="$3"; shift 3
[ "${1:-}" = "--" ] && shift
if [ "$#" -eq 0 ]; then echo "no command given after --" >&2; exit 2; fi

# Disk-backed, NOT /tmp. Override with CLAUDE_CAPTURE_DIR if desired.
outdir="${CLAUDE_CAPTURE_DIR:-$HOME/.cache/clarp/captures}"
mkdir -p "$outdir"
stamp="$(date +%Y%m%d-%H%M%S)"
out="$outdir/${name}-${stamp}.log"

# Kill the whole child process group if we're interrupted.
trap 'kill 0 2>/dev/null || true' INT TERM

echo "capturing '$*'"
echo "  -> $out  (<= $maxsize, <= ${secs}s)"
# timeout bounds wall-clock; head -c bounds bytes (closes pipe → SIGPIPE).
# `|| true` so a non-zero exit from timeout/SIGPIPE isn't treated as failure.
timeout "${secs}s" "$@" 2>&1 | head -c "$maxsize" > "$out" || true

echo "done: $(du -h "$out" | cut -f1) captured at $out"
echo "(remember to delete it when finished: rm '$out')"
