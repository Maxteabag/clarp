#!/usr/bin/env python3
"""Bytes read by transcript imports on a replay of a day's journal.

The Host's journal has one ``transcriptTailDecision`` line per import: the
time, what the import did, why, and the transcript's size bucket. It names no
session, so a day cannot be replayed file for file. This tool replays the
day's WORKLOAD instead, on a scratch store and synthetic transcripts:

* sessions per size bucket, of three kinds: with an automation row (a
  heartbeat was sent to it), with a sub-agent call among its old turns, plain;
* one import per journal line, for a session of the line's bucket and of the
  kind the line's reason implies; the transcript grows before the import, and
  a line the journal marks ``carry_inputs_changed`` has a database row
  written first. The journal does not say WHICH row had changed, so
  ``--inputs`` chooses one model for all of them: ``turn`` (a turn row; the
  default, and what this tool always did), ``send`` (a new message: its
  /send row and its turn's row) or ``live`` (a turn row and the row of the
  reply that turn is streaming). Since the ``transcriptCarryInputsChanged``
  line the journal does say, and ``journal`` goes by it line by line: a live
  row for ``row_live``, and for a turn word also a turn row that began among
  the old turns (lines without the words count as ``row_live``);
* a Host start at each given time: the imports after it run in a new process.

It runs the real import, through the import cache, and reads the process's
own read counter (``/proc/self/io`` ``rchar``) around each one. The first
import of every session (a cold store) is not counted: a day starts warm.

Since the rules of lib.message_carry a tail may find some rows changed, so
the three models differ: ``turn`` and ``send`` are changes a tail now allows,
``live`` is one it does not. A day on a live Host is a mix of them (and of a
turn settling, which is not modelled); run all three for the range.

Two what-ifs, for judging a rule before it is built. ``--live-rows-pass``
takes ``row_live`` out of what a tail's guard names, as if a rule let every
changed live row through (the upper bound for such a rule: it asks none of
the rule's conditions). ``--fence lt16m=0.027,lt64m=0.119`` refuses the
publication of that share of the tails, per size, through the import's own
refusal path, as a foreign commit during a writer yield does on a live Host
(the journal gives the shares: a ``checkpoint_foreign_commit_yield`` line
just before a ``tail_applied`` one); the import after such a tail is full.

What it does not model: imports that fail to publish because another writer
committed meanwhile or the file grew during the parse, transcripts with no
safe boundary, deferred imports, and the Codex and agy parsers (every
transcript here is Claude's). The figure is for comparing one tree with
another on the same workload, not a prediction of a live Host's counter.

    python tools/import_replay.py --journal day.txt --work /var/tmp/replay-1

``--sample N`` replays every Nth import and every Nth Host start and scales
the bytes back. Never point ``--work`` at anything but a scratch directory:
the tool creates its own database there and refuses any other.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]

BUCKET_MB = {"lt1m": 0.3, "lt16m": 6.6, "lt64m": 27.2, "lt256m": 120.8, "lt1024m": 412.7}
# Live sessions per bucket on the day the defaults were taken (12 Nov 2026).
BUCKET_SESSIONS = {"lt1m": 44, "lt16m": 46, "lt64m": 6, "lt256m": 10, "lt1024m": 1}
KINDS = ("automation", "subagent", "plain")
TURN_BYTES = 60 * 1024          # a turn of history: prompt, a tool step with output, answer
STEP_BYTES = 8 * 1024           # what one tick appends
BASE_MS = 1_794_441_600_000     # 12 Nov 2026 00:00 UTC, the replay's clock at second 0


# ---- the plan (parent) ------------------------------------------------------------

def read_journal(path: pathlib.Path) -> list[dict]:
    events = []
    words = None
    for line in path.read_text(errors="replace").splitlines():
        named = re.search(r"transcriptCarryInputsChanged what=(\S+)", line)
        if named:
            words = named[1].split(",")      # logged just before its import's decision
            continue
        if "transcriptTailDecision" not in line:
            continue
        stamp = re.search(r"(?:^|\s)(\d\d):(\d\d):(\d\d) transcriptTailDecision", line)
        fields = dict(re.findall(r"(\w+)=(\S+)", line))
        code, size = fields.get("code", ""), fields.get("size", "")
        if not stamp or code.startswith("deferred") or size not in BUCKET_MB:
            continue
        reason = fields.get("reason", "") if code == "full_no_frontier" else code
        second = int(stamp[1]) * 3600 + int(stamp[2]) * 60 + int(stamp[3])
        kind = {"automation_provenance": "automation", "bootstrap_effect": "subagent"}.get(
            reason, "plain")
        events.append({"at": second, "bucket": size, "kind": kind, "journal": reason,
                       "inputs_changed": code == "full_carry_inputs_changed",
                       # Which rows the journal says stood in the way (None: a
                       # journal from before that line existed).
                       "words": words if code == "full_carry_inputs_changed" else None})
        words = None
    events.sort(key=lambda event: event["at"])
    return events


SLICE_SECONDS = 360


def plan(events: list[dict], starts: list[int], sample: int, session_scale: float) -> dict:
    """Which imports are replayed, for which session, at what time.

    Sampling keeps whole six-minute slices of the day, one in ``sample``, and
    closes the gaps between them on the replay's clock, so the ticks of a
    running turn stay as close together as they were (the hourly audit goes
    by that clock). Every ``sample``-th Host start is kept, at the first
    replayed import after its real time."""
    kept_slices = [number for number in range(24 * 3600 // SLICE_SECONDS)
                   if number % sample == sample // 2]
    position = {number: ordinal for ordinal, number in enumerate(kept_slices)}

    def replay_time(second: int) -> int | None:
        number = second // SLICE_SECONDS
        if number not in position:
            return None
        return position[number] * SLICE_SECONDS + second % SLICE_SECONDS

    kept = []
    for event in events:
        at = replay_time(event["at"])
        if at is not None:
            kept.append({**event, "at": at})
    cells = collections.defaultdict(list)
    for event in kept:
        cells[(event["bucket"], event["kind"])].append(event)
    all_cells = collections.Counter((event["bucket"], event["kind"]) for event in events)
    sessions = {}
    for (bucket, kind), cell in sorted(cells.items()):
        share = all_cells[(bucket, kind)] / sum(
            count for (b, _), count in all_cells.items() if b == bucket)
        count = max(1, round(BUCKET_SESSIONS[bucket] * session_scale * share))
        names = [f"{bucket}-{kind}-{index}" for index in range(count)]
        for name in names:
            sessions[name] = {"bucket": bucket, "kind": kind,
                              "bytes": int(BUCKET_MB[bucket] * 1024 * 1024)}
        for index, event in enumerate(cell):
            event["session"] = names[index % count]
    kept_starts = []
    for start in (sorted(starts)[sample // 2::sample] if sample > 1 else sorted(starts)):
        later = [number for number in kept_slices if number * SLICE_SECONDS >= start]
        if later:
            kept_starts.append(position[later[0]] * SLICE_SECONDS)
    segments, edges = [], [0, *kept_starts, 10 ** 9]
    for begin, end in zip(edges, edges[1:]):
        segments.append([event for event in kept if begin <= event["at"] < end])
    return {"sessions": sessions, "segments": segments, "starts": kept_starts, "sample": sample,
            "journal_events": len(events), "replayed_events": len(kept)}


# ---- synthetic Claude transcripts -------------------------------------------------

def _record(role: str, content, second: float, padding: int = 0) -> bytes:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(BASE_MS / 1000 + second))
    record = {"type": role, "timestamp": f"{stamp}.000Z",
              "message": {"role": role, "content": content}}
    if padding > 0:
        record["synthetic_padding"] = "x" * padding
    return json.dumps(record, separators=(",", ":")).encode() + b"\n"


def turn_bytes(ordinal: int, second: float, *, subagent: bool = False, size: int = TURN_BYTES) -> bytes:
    call = f"call-{ordinal}"
    tool = ({"type": "tool_use", "id": call, "name": "Agent",
             "input": {"description": "look", "prompt": "look", "subagent_type": "general-purpose"}}
            if subagent else
            {"type": "tool_use", "id": call, "name": "Bash", "input": {"command": "printf x"}})
    return b"".join((
        _record("user", f"synthetic request {ordinal}", second),
        _record("assistant", [{"type": "text", "text": f"synthetic step {ordinal}"}, tool], second),
        _record("user", [{"type": "tool_result", "tool_use_id": call, "content": "x"}], second,
                padding=size - 600),
        _record("assistant", [{"type": "text", "text": f"synthetic answer {ordinal}"}], second)))


def step_bytes(ordinal: int, second: float) -> bytes:
    call = f"step-{ordinal}"
    return b"".join((
        _record("assistant", [{"type": "text", "text": f"synthetic step {ordinal}"},
                              {"type": "tool_use", "id": call, "name": "Bash",
                               "input": {"command": "printf x"}}], second),
        _record("user", [{"type": "tool_result", "tool_use_id": call, "content": "x"}], second,
                padding=STEP_BYTES - 500)))


def write_history(path: pathlib.Path, target: int, *, subagent: bool) -> int:
    """A transcript of about ``target`` bytes; the sub-agent call is an old turn."""
    ordinal = 0
    with path.open("wb") as out:
        while out.tell() < target:
            out.write(turn_bytes(ordinal, -86400 + ordinal, subagent=subagent and ordinal == 1,
                                 size=min(TURN_BYTES, max(2048, target // 4))))
            ordinal += 1
    return ordinal


# ---- one process between two Host starts (child) ----------------------------------

def _rchar() -> int:
    for line in pathlib.Path("/proc/self/io").read_text().splitlines():
        if line.startswith("rchar:"):
            return int(line.split()[1])
    raise RuntimeError("no rchar in /proc/self/io")


def run_segment(work: pathlib.Path, index: int) -> dict:
    sys.path[:0] = [str(ROOT), str(ROOT / "server")]
    from lib import agents as agents_db, db, message_store
    from lib import transcript_import_cache as cache, transcript_incremental as incremental

    database = pathlib.Path(db.DB_PATH).resolve()
    if work.resolve() not in database.parents:
        raise SystemExit(f"refusing: the database {database} is not inside {work}")
    db.run_as_service("tests")           # a scratch store of its own, created here
    the_plan = json.loads((work / "plan.json").read_text())
    state_path = work / "sessions.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    clock = [BASE_MS]
    db.now_ms = lambda: clock[0]          # the audit goes by the journal's time of day
    decisions = []
    log = incremental._tail_log

    def recorded(code, *, retention=False, reason=None, **kwargs):
        if not retention:
            decisions.append(code + (f"({reason})" if reason else ""))
        return log(code, retention=retention, reason=reason, **kwargs)

    incremental._tail_log = recorded
    transcripts = work / "transcripts"
    fence = the_plan.get("fence") or {}
    fenced = collections.Counter()
    current = {"bucket": "", "tail": False}
    if the_plan.get("live_rows_pass"):
        from lib import message_carry
        asked = message_carry.changed

        def without_live_rows(*args, **kwargs):
            return ",".join(word for word in asked(*args, **kwargs).split(",")
                            if word and word != "row_live")

        message_carry.changed = without_live_rows
    if fence:
        import random
        from lib import message_import_writer, message_writes
        chance = random.Random(int(the_plan.get("seed") or 0) * 1000 + index)
        store_tail = message_writes.store_transcript_tail
        allowed = message_import_writer._ImportWriter.checkpoint_allowed

        def tail(*args, **kwargs):
            current["tail"] = True
            try:
                return store_tail(*args, **kwargs)
            finally:
                current["tail"] = False

        def publication(writer, status=None):
            if current["tail"] and chance.random() < fence.get(current["bucket"], 0.0):
                current["tail"] = False          # once per tail
                writer._publication_refusals.add("foreign_commit_yield")
                fenced[current["bucket"]] += 1
            return allowed(writer, status)

        message_writes.store_transcript_tail = tail
        message_store.store_transcript_tail = tail
        message_import_writer._ImportWriter.checkpoint_allowed = publication

    def run_import(name: str) -> None:
        session = state[name]
        path = transcripts / f"{name}.jsonl"

        def import_latest():
            return incremental.import_transcript(
                backend="claude", path=path, agent_id=session["agent"],
                backend_session_id=name)

        importer = incremental._completion_import(
            import_latest, backend="claude", path=path, agent_id=session["agent"],
            backend_session_id=name)
        cache.import_if_changed(path, importer, owner=session["agent"])

    if index == 0:                        # yesterday: every session exists and is imported
        transcripts.mkdir(exist_ok=True)
        for name, spec in the_plan["sessions"].items():
            agent = agents_db.create_agent(persona=name, voice_id="", cwd=str(work),
                                           session=name, backend="claude")
            ordinal = write_history(transcripts / f"{name}.jsonl", spec["bytes"],
                                    subagent=spec["kind"] == "subagent")
            if spec["kind"] == "automation":
                message_store.record_user_message(
                    agent_id=agent, backend_session_id=name, client_msg_id=f"hb-{name}",
                    text="synthetic heartbeat", origin="heartbeat", trace_id=f"hb-{name}")
            state[name] = {"agent": agent, "ordinal": ordinal, "ticks": 0}
            run_import(name)
        decisions.clear()

    totals = collections.Counter()
    by_journal = collections.Counter()
    mix = collections.Counter()
    started = time.monotonic()
    for event in the_plan["segments"][index]:
        name = event["session"]
        session = state[name]
        clock[0] = BASE_MS + event["at"] * 1000
        session["ticks"] += 1
        session["ordinal"] += 1
        with (transcripts / f"{name}.jsonl").open("ab") as out:
            out.write(turn_bytes(session["ordinal"], event["at"], size=2048)
                      if session["ticks"] % 4 == 0 else step_bytes(session["ordinal"], event["at"]))
        current["bucket"] = the_plan["sessions"][name]["bucket"]
        model = the_plan.get("inputs", "turn")
        if event["inputs_changed"] and model == "journal":
            trace = f"turn-{name}-{session['ordinal']}"
            named = event.get("words") or ["row_live"]
            agents_db.open_turn(agent_id=session["agent"], source="pwa", trace_id=trace)
            if "row_live" in named and message_store.upsert_live_assistant_message(
                    agent_id=session["agent"], backend_session_id=name, trace_id=trace,
                    text=f"synthetic streamed reply {session['ordinal']}",
                    item_key="replay") is None:
                raise SystemExit("the live row was refused: the model would measure nothing")
            if any(word.startswith("turn_") for word in named):
                # A turn that was running among the old turns: no rule lets it through.
                db.conn().execute(
                    "INSERT INTO turns(agent_id,source,trace_id,started_at) VALUES (?,'pwa',?,?)",
                    (session["agent"], f"old-{trace}", BASE_MS - 86_400_000))
                db.conn().commit()
        elif event["inputs_changed"]:
            trace = f"turn-{name}-{session['ordinal']}"
            # Every model has the turn row: a live row is only written for
            # the agent's running turn.
            agents_db.open_turn(agent_id=session["agent"], source="pwa", trace_id=trace)
            if the_plan.get("inputs") == "send":
                message_store.record_user_message(
                    agent_id=session["agent"], backend_session_id=name,
                    client_msg_id=f"m-{name}-{session['ordinal']}",
                    text=f"synthetic message {name} {session['ordinal']}", trace_id=trace)
            if the_plan.get("inputs") == "live":
                if message_store.upsert_live_assistant_message(
                        agent_id=session["agent"], backend_session_id=name, trace_id=trace,
                        text=f"synthetic streamed reply {session['ordinal']}",
                        item_key="replay") is None:
                    raise SystemExit("the live row was refused: the model would measure nothing")
        decisions.clear()
        before = _rchar()
        run_import(name)
        read = _rchar() - before
        totals["bytes"] += read
        totals["imports"] += 1
        by_journal[event["journal"]] += read
        done = next((d for d in reversed(decisions) if d.startswith(("tail_applied", "full_"))), "none")
        mix[done] += 1
        totals["full" if done.startswith("full_") else "tail" if done == "tail_applied" else "other"] += 1
    state_path.write_text(json.dumps(state))
    db.close_local()
    return {"segment": index, "seconds": round(time.monotonic() - started, 1), **totals,
            "by_journal": dict(by_journal), "mix": dict(mix), "fenced": dict(fenced)}


# ---- the parent --------------------------------------------------------------------

def _clock(text: str) -> int:
    hours, minutes = text.split(":")[:2]
    return int(hours) * 3600 + int(minutes) * 60


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=pathlib.Path)
    parser.add_argument("--work", type=pathlib.Path, required=True)
    parser.add_argument("--starts", default="05:40,06:35,07:06,07:27,07:29,10:35,11:05,14:37,15:45,18:01",
                        help="the day's Host starts, HH:MM, comma separated")
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument("--session-scale", type=float, default=0.3)
    parser.add_argument("--inputs", choices=("turn", "send", "live", "journal"), default="turn",
                        help="which row a line marked carry_inputs_changed finds changed")
    parser.add_argument("--live-rows-pass", action="store_true",
                        help="what-if: a tail's guard does not name row_live")
    parser.add_argument("--seed", type=int, default=1, help="for the tails --fence refuses")
    parser.add_argument("--fence", default="",
                        help="what-if: the share of tails whose publication is refused, "
                             "per size, as lt16m=0.027,lt64m=0.119")
    parser.add_argument("--segment", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    work = args.work.resolve()
    if args.segment is not None:
        print(json.dumps(run_segment(work, args.segment)))
        return 0
    if args.journal is None:
        parser.error("--journal is required")
    if work.exists() and any(work.iterdir()):
        parser.error(f"{work} is not empty; give a new scratch directory")
    work.mkdir(parents=True, exist_ok=True)
    the_plan = plan(read_journal(args.journal), [_clock(s) for s in args.starts.split(",") if s],
                    max(1, args.sample), args.session_scale)
    the_plan["inputs"] = args.inputs
    the_plan["live_rows_pass"] = args.live_rows_pass
    the_plan["seed"] = args.seed
    the_plan["fence"] = {size: float(share) for size, share in
                         (item.split("=") for item in args.fence.split(",") if item)}
    (work / "plan.json").write_text(json.dumps(the_plan))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CLAUDE_PWA_", "CLARP_", "XDG_"))}
    env.update(CLAUDE_PWA_DB=str(work / "state.sqlite"), CLAUDE_PWA_LOG_DIR=str(work / "logs"),
               CLAUDE_PWA_CONFIG=str(work / "config.toml"), CLARP_CONFIG_DIR=str(work / "config"),
               CLARP_SHARE_DIR=str(work / "share"), CLARP_CACHE_DIR=str(work / "cache"),
               CLARP_TELEMETRY_DB=str(work / "telemetry.sqlite"),
               XDG_DATA_HOME=str(work / "xdg-data"), XDG_CACHE_HOME=str(work / "xdg-cache"),
               XDG_CONFIG_HOME=str(work / "xdg-config"), XDG_STATE_HOME=str(work / "xdg-state"))
    totals, by_journal, mix = collections.Counter(), collections.Counter(), collections.Counter()
    fenced = collections.Counter()
    for index in range(len(the_plan["segments"])):
        ran = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()),
                              "--work", str(work), "--segment", str(index)],
                             env=env, capture_output=True, text=True)
        if ran.returncode != 0:
            sys.stderr.write(ran.stderr[-3000:])
            return 1
        result = json.loads(ran.stdout.strip().splitlines()[-1])
        for key in ("bytes", "imports", "full", "tail", "other"):
            totals[key] += result.get(key, 0)
        by_journal.update(result["by_journal"])
        mix.update(result["mix"])
        fenced.update(result.get("fenced") or {})
        print(f"segment {index}: {result.get('imports', 0)} imports in {result['seconds']} s",
              file=sys.stderr, flush=True)
    scale = the_plan["sample"]
    day = totals["bytes"] * scale
    report = {
        "journal_imports": the_plan["journal_events"], "replayed_imports": totals["imports"],
        "sample": scale, "host_starts_replayed": len(the_plan["starts"]),
        "changed_inputs_model": the_plan["inputs"],
        "what_if_live_rows_pass": the_plan["live_rows_pass"],
        "what_if_tails_refused_publication": dict(fenced),
        "sessions": len(the_plan["sessions"]),
        "full": totals["full"], "tail": totals["tail"], "other": totals["other"],
        "bytes_read_replayed": totals["bytes"],
        "bytes_read_per_day_scaled_GB": round(day / 1e9, 1),
        "bytes_read_per_minute_scaled_MB": round(day / 1440 / 1e6, 1),
        "GB_by_journal_reason_scaled": {key: round(value * scale / 1e9, 1)
                                        for key, value in by_journal.most_common()},
        "decisions": dict(mix.most_common()),
    }
    (work / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
