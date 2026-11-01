#!/usr/bin/env bash
# DIAGNOSTIC order replay for .github/workflows/basefix-diag.yml (branch diag/basefix-s4). Metadata only; no fix.
# Sequence A = frozen gw0 prefix (184 ids, MAIN run 41298914107 @a6d4012a) + 6 inotify targets + control; sequence B (ONLY if A did not
# reproduce) = frozen gw0 prefix (362 ids, run 41299140082, server_di excluded) + 6 streamer/hook targets + control.
set -uo pipefail
OUT=diag-results; mkdir -p "$OUT"; IDX=$OUT/index.tsv
log() { printf '%s\t%s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$IDX"; }
D=.github/basefix-diag
mkdir -p .diag && cp "$D/diagplugin.py" .diag/
VT="$(dirname "$PWD")"
log "env ImageOS=${ImageOS:-?} ImageVersion=${ImageVersion:-?} kernel=$(uname -r) python=$(.venv/bin/python -V 2>&1) (compare with this run's 'Runner Image' block; the failing runs' Runner Image record is ubuntu-26.04 20261025.179.1)"
log "lists sha256 A=$(sha256sum $D/s2a-main-a6d4-nodeids.txt | cut -c1-16) B=$(sha256sum $D/s2b-nodeids.txt | cut -c1-16)"
I=tests/integration
TA=( $I/test_inotify_dispatcher.py::test_inotify_dispatcher_fires_when_file_grows
     $I/test_inotify_dispatcher.py::test_inotify_dispatcher_multiplexes_multiple_agents
     $I/test_inotify_dispatcher.py::test_inotify_dispatcher_fires_per_text_block_not_per_turn
     $I/test_inotify_dispatcher.py::test_inotify_dispatcher_unwatch_stops_emitting
     $I/test_inotify_dispatcher.py::test_inotify_dispatcher_stop_is_clean
     $I/test_inotify_dispatcher.py::test_inotify_dispatcher_wakeup_latency_under_budget )
CA=tests/unit/test_transcript_watcher.py::test_watcher_fires_callback_for_new_assistant_text
TB=( $I/test_transcript_streamer.py::test_streamer_enqueues_text_block_when_transcript_grows
     $I/test_transcript_streamer.py::test_streamer_fires_per_text_block
     $I/test_transcript_streamer.py::test_a_held_import_for_one_agent_does_not_delay_another_agents_speech
     $I/test_transcript_streamer.py::test_streamer_broadcasts_transcript_updated_for_text_blocks
     $I/test_transcript_streamer.py::test_streamer_broadcasts_for_tool_use_lines_too
     $I/test_streamer_hook_coordination.py::test_streamer_advances_cursor_so_hook_sees_no_new_text )
CB=$I/test_transcript_streamer.py::test_streamer_skips_local_source_turns
unexpected() {
  grep -q 'network access attempted' "$1" && { log "STOP: guard reported a network/CLI attempt"; exit 1; }
  grep -qE 'EMFILE|ENFILE|Too many open files' "$1" && { log "STOP: EMFILE/ENFILE"; exit 1; }
  grep -qE 'INTERNALERROR|PluginValidationError|Error while loading conftest|diagplugin.*(Error|Traceback)' "$1" && { log "STOP: pytest internal/plugin error"; exit 1; }
  return 0
}
snap() { ls -d "$VT"/.ci-vartmp.* 2>/dev/null | sort; }
# seq NAME CAP LISTFILE TARGET_REGEX CONTROL TARGETS... -> sets REPRO
seq_run() {
  local name=$1 cap=$2 list=$3 tgt=$4 ctl=$5; shift 5
  local o=$OUT/$name; mkdir -p "$o"; local -a pre; mapfile -t pre < "$D/$list"
  # de-duplicate, keeping the FIRST occurrence (its real gw0 position): pytest collects a repeated node id once
  local -a ids; mapfile -t ids < <(printf '%s\n' "${pre[@]}" "$@" "$ctl" | awk 'NF && !seen[$0]++')
  printf '%s\n' "${ids[@]}" > "$o/requested.txt"
  log "$name start prefix=${#pre[@]} targets=$# control=1 total=${#ids[@]} cap=${cap}s"
  local before; before=$(snap)
  timeout --kill-after=10 "$cap" bash scripts/ci_contained.sh env PYTHONPATH="$PWD/.diag" PYTHONDONTWRITEBYTECODE=1 \
    python -m pytest -p diagplugin -p no:cacheprovider -n 0 -rfE --tb=long --junitxml="/var/tmp/junit-$name.xml" "${ids[@]}" \
    2>&1 | head -c 10000000 > "$o/out.log"
  local rc=${PIPESTATUS[0]}
  local new; new=$(comm -13 <(echo "$before") <(snap) | grep -v '^$')
  [ "$(printf '%s\n' "$new" | grep -c .)" -eq 1 ] || { log "STOP: expected one new .ci-vartmp, got: $(echo $new)"; exit 1; }
  [ -d "$new/diag-obs" ] && cp -r "$new/diag-obs" "$o/"; [ -f "$new/junit-$name.xml" ] && cp "$new/junit-$name.xml" "$o/"
  grep -E '^(FAILED|ERROR) ' "$o/out.log" | sed 's/ - .*//' | sort -u > "$o/failed.txt"
  log "$name rc=$rc failed=$(wc -l < "$o/failed.txt")"
  case $rc in 0|1) ;; 124|137) log "STOP: $name time cap"; exit 1;; *) log "STOP: $name rc=$rc"; exit 1;; esac
  unexpected "$o/out.log"
  # proof pytest ran: the round's own junit with exactly the requested count, all target names present, and a
  # non-tty summary line ('N passed ... in Xs', '=' padding optional: the S3 regex wrongly required it)
  python3 - "$o/junit-$name.xml" "$o/requested.txt" "$o/order-check.txt" <<'PY' || { log "STOP: $name junit count/identity/ORDER check failed (see order-check.txt)"; exit 1; }
import sys, xml.etree.ElementTree as ET
got = [(c.get("classname"), c.get("name")) for c in ET.parse(sys.argv[1]).iter("testcase")]
def key(nodeid):   # tests/unit/test_x.py::TestC::test_n[p] -> ("tests.unit.test_x.TestC", "test_n[p]")
    parts = nodeid.split("::"); mod = parts[0][:-3].replace("/", ".")
    return (".".join([mod] + parts[1:-1]), parts[-1])
want = [key(l) for l in open(sys.argv[2]).read().splitlines() if l.strip()]
out = open(sys.argv[3], "w")
if len(got) != len(want):
    out.write(f"COUNT got={len(got)} want={len(want)}\n")
for i, (g, w) in enumerate(zip(got, want)):
    if g != w:
        out.write(f"FIRST DIVERGENCE at {i}: got={g} want={w}\n"); sys.exit(1)
out.write("ORDER OK\n" if len(got) == len(want) else ""); sys.exit(0 if len(got) == len(want) else 1)
PY
  grep -qE '^(=+ )?[0-9]+ (passed|failed|error|errors)[^=]* in [0-9.]+s( \([0-9:]+\))?( =+)?$' "$o/out.log" || { log "STOP: $name no pytest summary line"; exit 1; }
  grep -qF "${ctl##*::}" "$o/failed.txt" && { log "STOP: $name control failed"; exit 1; }
  while read -r kind id; do [ -z "${id:-}" ] && continue
    [[ "$id" =~ $tgt ]] || { log "STOP: $name failure outside targets (prefix test): $id"; exit 1; }; done < "$o/failed.txt"
  REPRO=$(grep -cE "$tgt" "$o/failed.txt")
  log "$name target failures=$REPRO"
}
REPRO=0
seq_run A 300 s2a-main-a6d4-nodeids.txt 'test_inotify_dispatcher' "$CA" "${TA[@]}"
if [ "$REPRO" -gt 0 ]; then log "A reproduced; B not run (minimum sequence reached)"; log "done"; exit 0; fi
seq_run B 600 s2b-nodeids.txt 'test_transcript_streamer|test_streamer_hook_coordination' "$CB" "${TB[@]}"
log "done (diagnostic only; a pass is not green; partial order, single process, newer image)"
