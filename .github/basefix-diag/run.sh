#!/usr/bin/env bash
# DIAGNOSTIC runner for .github/workflows/basefix-diag.yml. Metadata only; no fix. One attempt per push.
set -uo pipefail
OUT=diag-results; mkdir -p "$OUT"; IDX=$OUT/index.tsv
log() { printf '%s\t%s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$IDX"; }
D=.github/basefix-diag
mkdir -p .diag && cp "$D/diagplugin.py" "$D/probe_inotify.py" .diag/
log "env ImageOS=${ImageOS:-?} ImageVersion=${ImageVersion:-?} kernel=$(uname -r) python=$(.venv/bin/python -V 2>&1) reference_failing_ImageVersion=20261025.179.1 (logged for comparison only)"
VT="$(dirname "$PWD")"            # ci_contained puts its private /var/tmp in .ci-vartmp.* here
I=tests/integration
IDS=( $I/test_inotify_dispatcher.py::test_inotify_dispatcher_fires_when_file_grows
      $I/test_inotify_dispatcher.py::test_inotify_dispatcher_multiplexes_multiple_agents
      $I/test_inotify_dispatcher.py::test_inotify_dispatcher_fires_per_text_block_not_per_turn
      $I/test_inotify_dispatcher.py::test_inotify_dispatcher_unwatch_stops_emitting
      $I/test_inotify_dispatcher.py::test_inotify_dispatcher_stop_is_clean
      $I/test_inotify_dispatcher.py::test_inotify_dispatcher_wakeup_latency_under_budget
      $I/test_transcript_streamer.py::test_streamer_enqueues_text_block_when_transcript_grows
      $I/test_transcript_streamer.py::test_streamer_fires_per_text_block
      $I/test_transcript_streamer.py::test_a_held_import_for_one_agent_does_not_delay_another_agents_speech
      $I/test_transcript_streamer.py::test_streamer_broadcasts_transcript_updated_for_text_blocks
      $I/test_transcript_streamer.py::test_streamer_broadcasts_for_tool_use_lines_too
      $I/test_streamer_hook_coordination.py::test_streamer_advances_cursor_so_hook_sees_no_new_text
      $I/test_server_di.py::test_server_home_terminal_runs_shell_in_home_without_resuming_an_agent
      tests/unit/test_transcript_watcher.py::test_watcher_fires_callback_for_new_assistant_text
      $I/test_transcript_streamer.py::test_streamer_skips_local_source_turns
      tests/unit/test_terminal_ws.py::test_bridge_forwards_keystrokes_resize_ping_and_closes )
CONTROLS='test_watcher_fires_callback_for_new_assistant_text|test_streamer_skips_local_source_turns|test_bridge_forwards_keystrokes_resize_ping_and_closes'
TGT='test_inotify_dispatcher|test_transcript_streamer|test_streamer_hook_coordination|test_server_home_terminal'
unexpected() {  # $1 = log: any of these on CI is unexpected => STOP (never counted as a target repro)
  grep -q 'network access attempted' "$1" && { log "STOP: guard reported a network/CLI attempt"; exit 1; }
  grep -qE 'EMFILE|ENFILE|Too many open files' "$1" && { log "STOP: EMFILE/ENFILE"; exit 1; }
  grep -qE 'INTERNALERROR|PluginValidationError|Error while loading conftest|diagplugin.*(Error|Traceback)' "$1" && { log "STOP: pytest internal/plugin error"; exit 1; }
  return 0
}
snap() { ls -d "$VT"/.ci-vartmp.* 2>/dev/null | sort; }
collect() {  # $1 = round dir, $2 = snapshot taken before the round: copy junit + diag-obs from the ONE new entry only
  local new; new=$(comm -13 <(echo "$2") <(snap) | grep -v '^$')
  [ "$(printf '%s\n' "$new" | grep -c .)" -eq 1 ] || { log "STOP: expected one new .ci-vartmp, got: $(echo $new)"; exit 1; }
  [ -d "$new/diag-obs" ] && cp -r "$new/diag-obs" "$1/"; [ -f "$new/junit-r$r.xml" ] && cp "$new/junit-r$r.xml" "$1/"; return 0
}
log "S0 probe start"; mkdir -p "$OUT/S0"
timeout --kill-after=10 90 bash scripts/ci_contained.sh env PYTHONPATH="$PWD/.diag" PYTHONDONTWRITEBYTECODE=1 python .diag/probe_inotify.py 2>&1 | head -c 1000000 > "$OUT/S0/out.log"
rc=${PIPESTATUS[0]}; log "S0 rc=$rc"
{ [ "$rc" = 0 ] && grep -q '"summary"' "$OUT/S0/out.log"; } || { log "STOP: S0 rc=$rc"; exit 1; }
unexpected "$OUT/S0/out.log"
for r in 1 2 3; do
  o=$OUT/S1-r$r; mkdir -p "$o"; log "S1-r$r start"; before=$(snap)
  timeout --kill-after=10 140 bash scripts/ci_contained.sh env PYTHONPATH="$PWD/.diag" PYTHONDONTWRITEBYTECODE=1 \
    python -m pytest -p diagplugin -p no:cacheprovider -n 0 -rA --tb=long --junitxml=/var/tmp/junit-r$r.xml "${IDS[@]}" \
    2>&1 | head -c 10000000 > "$o/out.log"
  rc=${PIPESTATUS[0]}; collect "$o" "$before"
  grep -E '^(FAILED|ERROR) ' "$o/out.log" | sed 's/ - .*//' | sort -u > "$o/failed.txt"; log "S1-r$r rc=$rc failed=$(wc -l < "$o/failed.txt")"
  case $rc in 0|1) ;; 124|137) log "STOP: S1-r$r time cap"; exit 1;; *) log "STOP: S1-r$r rc=$rc"; exit 1;; esac
  unexpected "$o/out.log"
  python3 - "$o/junit-r$r.xml" "${IDS[@]}" <<'PY' || { log "STOP: junit identity/count check failed"; exit 1; }
import sys, xml.etree.ElementTree as ET
cases = list(ET.parse(sys.argv[1]).iter("testcase")); names = {c.get("name") for c in cases}
want = [i.split("::")[-1] for i in sys.argv[2:]]
sys.exit(0 if len(cases) == len(want) and set(want) <= names else 1)
PY
  [ -s "$o/junit-r$r.xml" ] && grep -qE '^=+ .*[0-9]+ (passed|failed|error|errors).* in [0-9.]+s' "$o/out.log" || { log "STOP: pytest not proven to run"; exit 1; }
  grep -qE "$CONTROLS" "$o/failed.txt" && { log "STOP: a control failed"; exit 1; }
  while read -r kind id; do [ -z "${id:-}" ] && continue; [[ "$id" =~ $TGT ]] || { log "STOP: failure outside targets $id"; exit 1; }; done < "$o/failed.txt"
  if grep -qE "$TGT" "$o/failed.txt"; then log "S1-r$r reproduced (stop repeating)"; break; fi
done
log "done (diagnostic only; a pass after a fail is not green)"
