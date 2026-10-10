#!/usr/bin/env bash
# Profiles the frame-budget check's UI thread with perf (CI only, on the
# release build): slint-app/tests/budget-profile.sh OUT. Each scenario's
# measured window (the app's "perf budget window" lines) gets a flat and a
# caller-inclusive report in OUT/profile-<scenario>.txt.
set -euo pipefail
cd "$(dirname "$0")/../.."
out=$(realpath -m "$1"); mkdir -p "$out"
data=/var/tmp/budget-perf.data
CLARP_CHECK_PROFILE=release CLARP_CHECK_WRAP="perf record -k CLOCK_MONOTONIC -F 499 --call-graph dwarf,16384 -o $data --" \
  slint-app/tests/check.sh frame-budget "$out/run" > "$out/run.log" 2>&1 || echo "the profiled run failed (its numbers are perf-inflated): see run.log"
grep "^perf budget window" "$out/run/app.log" | while read -r _ _ _ name start end pid; do
  {
    echo "# $name: $start to $end, UI thread $pid"
    echo "## self"
    perf report -i "$data" --time "$start,$end" --tid "$pid" --no-children --sort symbol --stdio -g none --percent-limit 0.4 2>/dev/null | grep -v '^$' | head -70
    echo "## inclusive (children)"
    perf report -i "$data" --time "$start,$end" --tid "$pid" --children --sort symbol --stdio -g none --percent-limit 1.5 2>/dev/null | grep -v '^$' | head -120
    echo "## call graph (callers of the heaviest)"
    perf report -i "$data" --time "$start,$end" --tid "$pid" --children --sort symbol --stdio -g caller,2,callee,function,percent --percent-limit 8 2>/dev/null | grep -v '^$' | head -400
  } > "$out/profile-$name.txt"
  echo "wrote $out/profile-$name.txt"
done
