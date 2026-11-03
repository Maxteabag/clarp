"""Synthetic transcript corpus for the read-model goldens (refactor slice S5).

Every transcript here is written by hand, shaped like what each backend CLI
records; none comes from a real session. Each backend has five requests:

  0  "continue"                      (matches client row c0)
  1  an agent-sent request           (client row c1, origin agent, trace tr-1;
                                      its reply has a finalized row to adopt)
  2  the heartbeat prompt            (the reply acknowledges HEARTBEAT_OK)
  3  "continue" again                (client row c3, trace tr-3; its reply
                                      has a live streamed row to adopt)
  4  a plain request                 (its turn ended interrupted)

Each request is a group of records: the prompt, a commentary step with a
shell call, the call's result, the final answer. ``GROWTH`` replays the
transcript as the live Host sees it grow and change:

  grow      groups 0-1 complete
  partial   half of group 2's first record (a partial last line)
  complete  the rest of that record, groups 2-3
  append    group 4
  truncate  back to the end of group 3 (the file shrinks in place)
  regrow    group 4 again
  rotate    a new file (new inode) under the same path, without group 1's
            commentary step: every later turn shifts down by one

OpenCode keeps its sessions in SQLite; there the steps are row inserts, a
running tool part (the partial step), deletes and a replaced database file.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
from dataclasses import dataclass, field

from lib.heartbeat import HEARTBEAT_OK, HEARTBEAT_PROMPT

AGENT = "agent-golden"
SESSION = "bs-golden"
SENDER = "agent-sender"
BACKENDS = ("claude", "codex", "agy", "grok", "muse", "opencode")
REQUESTS = 5
# Unix seconds of request 0; request r starts r minutes later.
BASE_S = 1_791_706_800          # 2026-10-11T08:20:00Z
TRACES = {1: "tr-1", 3: "tr-3", 4: "tr-4"}
GROWTH = ("grow", "partial", "complete", "append", "truncate", "regrow", "rotate")


def request_text(r: int) -> str:
    if r in (0, 3):
        return "continue"
    if r == 2:
        return HEARTBEAT_PROMPT
    return f"request {r} about /redacted/{r}"


def answer_text(r: int) -> str:
    if r == 2:
        return HEARTBEAT_OK
    return f"Done with {r}. <speak>Done {r}.</speak>"


def epoch(r: int, k: int) -> float:
    return BASE_S + r * 60 + k


def iso(r: int, k: int) -> str:
    import datetime as dt
    stamp = dt.datetime.fromtimestamp(epoch(r, k), tz=dt.timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _line(record: dict) -> bytes:
    return json.dumps(record, separators=(",", ":")).encode() + b"\n"


@dataclass
class Jsonl:
    """A JSONL transcript: its records, grouped by request, and which record
    the rotation drops."""
    groups: list[list[dict]]
    rotate_drop: tuple[int, int]          # (group, index within the group)
    header: list[dict] = field(default_factory=list)

    def lines(self, upto: int, *, drop: tuple[int, int] | None = None) -> list[bytes]:
        out = [_line(r) for r in self.header]
        for g, group in enumerate(self.groups[:upto]):
            out += [_line(r) for i, r in enumerate(group) if (g, i) != drop]
        return out


# ---- the JSONL backends ----------------------------------------------------

def claude() -> Jsonl:
    groups = []
    n = 0
    for r in range(REQUESTS):
        def rec(kind, content, k):
            nonlocal n
            n += 1
            return {"type": kind, "uuid": f"uuid-{n}", "parentUuid": f"uuid-{n - 1}" if n > 1 else None,
                    "timestamp": iso(r, k), "sessionId": "redacted",
                    "message": {"role": kind, "content": content}}
        groups.append([
            rec("user", request_text(r), 0),
            rec("assistant", [{"type": "text", "text": f"Looking at {r}."},
                              {"type": "tool_use", "id": f"toolu_{r}", "name": "Bash",
                               "input": {"command": f"ls /redacted/{r}"}}], 2),
            rec("user", [{"type": "tool_result", "tool_use_id": f"toolu_{r}",
                          "content": "a\nb"}], 3),
            rec("assistant", [{"type": "text", "text": answer_text(r)}], 5),
        ])
    return Jsonl(groups, rotate_drop=(1, 1))


def codex() -> Jsonl:
    header = [{"timestamp": iso(0, 0), "type": "session_meta",
               "payload": {"id": "redacted", "cwd": "/redacted"}}]
    groups = []
    for r in range(REQUESTS):
        turn = f"turn-{r}"
        final = answer_text(r)
        groups.append([
            {"timestamp": iso(r, 0), "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": turn}},
            {"timestamp": iso(r, 0), "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": request_text(r)}]}},
            {"timestamp": iso(r, 0), "type": "event_msg",
             "payload": {"type": "user_message", "message": request_text(r)}},
            {"timestamp": iso(r, 2), "type": "event_msg",
             "payload": {"type": "agent_message", "message": f"Looking at {r}.",
                         "phase": "commentary", "turn_id": turn}},
            {"timestamp": iso(r, 2), "type": "response_item",
             "payload": {"type": "function_call", "name": "exec_command",
                         "arguments": json.dumps({"cmd": f"ls /redacted/{r}"}),
                         "call_id": f"call_{r}", "turn_id": turn}},
            {"timestamp": iso(r, 3), "type": "response_item",
             "payload": {"type": "function_call_output", "call_id": f"call_{r}",
                         "output": "Process exited with code 0\nOutput:\na\nb",
                         "turn_id": turn}},
            {"timestamp": iso(r, 5), "type": "event_msg",
             "payload": {"type": "agent_message", "message": final,
                         "phase": "final_answer", "turn_id": turn}},
            {"timestamp": iso(r, 5), "type": "response_item",
             "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                         "content": [{"type": "output_text", "text": final}],
                         "turn_id": turn}},
            {"timestamp": iso(r, 6), "type": "event_msg",
             "payload": {"type": "task_complete", "turn_id": turn}},
        ])
    return Jsonl(groups, rotate_drop=(1, 3), header=header)


def agy() -> Jsonl:
    groups = []
    for r in range(REQUESTS):
        step = r * 4
        groups.append([
            {"step_index": step, "type": "USER_INPUT", "created_at": iso(r, 0),
             "content": f"<USER_REQUEST>\n{request_text(r)}\n</USER_REQUEST>"},
            {"step_index": step + 1, "type": "PLANNER_RESPONSE", "created_at": iso(r, 2),
             "content": f"Looking at {r}.", "thinking": "Plan the listing.",
             "tool_calls": [{"name": "run_command",
                             "args": {"command": f"ls /redacted/{r}",
                                      "toolSummary": "list files"}}]},
            {"step_index": step + 2, "type": "GENERIC", "created_at": iso(r, 3),
             "content": "Created At: x\nCompleted At: y\nOutput:\na\nb", "status": "DONE"},
            {"step_index": step + 3, "type": "PLANNER_RESPONSE", "created_at": iso(r, 5),
             "content": answer_text(r)},
        ])
    return Jsonl(groups, rotate_drop=(1, 1))


def grok() -> Jsonl:
    header = [{"type": "user", "content": "<user_info>redacted</user_info>", "ts": iso(0, 0)}]
    groups = []
    for r in range(REQUESTS):
        groups.append([
            {"type": "user", "content": f"<user_query>{request_text(r)}</user_query>",
             "ts": iso(r, 0)},
            {"type": "assistant", "content": f"Looking at {r}.", "ts": iso(r, 2),
             "tool_calls": [{"id": f"call_{r}", "name": "run_terminal_command",
                             "arguments": json.dumps({"command": f"ls /redacted/{r}"})}]},
            {"type": "tool_result", "tool_call_id": f"call_{r}", "content": "exit: 0\na\nb",
             "ts": iso(r, 3)},
            {"type": "assistant", "content": answer_text(r), "ts": iso(r, 5)},
        ])
    return Jsonl(groups, rotate_drop=(1, 1), header=header)


def muse() -> Jsonl:
    def row(r, k, event):
        return {"payload_type": "runtime.session", "recorded_at": int(epoch(r, k) * 1_000_000),
                "payload": {"kind": "run", "event": event}}
    groups = []
    for r in range(REQUESTS):
        groups.append([
            row(r, 0, {"kind": "started", "prompt": request_text(r)}),
            row(r, 2, {"kind": "assistant_message_committed", "text": f"Looking at {r}."}),
            row(r, 2, {"kind": "assistant_tool_calls_committed", "tool_calls": [
                {"call_id": f"call_{r}", "name": "shell",
                 "args": {"cmd": f"ls /redacted/{r}"}}]}),
            row(r, 3, {"kind": "tool_result_batch_committed", "results": [
                {"tool_call_id": f"call_{r}", "text": "a\nb"}]}),
            row(r, 5, {"kind": "assistant_message_committed", "text": answer_text(r)}),
            row(r, 6, {"kind": "terminal", "terminal": "completed"}),
        ])
    return Jsonl(groups, rotate_drop=(1, 1))


JSONL = {"claude": claude, "codex": codex, "agy": agy, "grok": grok, "muse": muse}


def transcript_path(root: pathlib.Path, backend: str) -> pathlib.Path:
    if backend == "opencode":
        return pathlib.Path(f"{root / 'opencode.db'}#ses_golden")
    return root / f"{backend}.jsonl"


def _write_in_place(path: pathlib.Path, data: bytes) -> None:
    """Grow or shrink the file in place (same inode), as a CLI appending does."""
    mode = "r+b" if path.exists() else "wb"
    with open(path, mode) as f:
        f.seek(0)
        f.write(data)
        f.truncate(len(data))


def apply_jsonl(path: pathlib.Path, script: Jsonl, step: str) -> None:
    """Bring the transcript at ``path`` to growth ``step``."""
    lines = {
        "grow": script.lines(2),
        "complete": script.lines(4),
        "append": script.lines(5),
        "truncate": script.lines(4),
        "regrow": script.lines(5),
    }
    if step == "partial":
        head = b"".join(script.lines(2))
        nxt = _line(script.groups[2][0])
        _write_in_place(path, head + nxt[: len(nxt) // 2])
    elif step == "rotate":
        fresh = path.with_suffix(".rotated")
        fresh.write_bytes(b"".join(script.lines(5, drop=script.rotate_drop)))
        os.replace(fresh, path)
    else:
        _write_in_place(path, b"".join(lines[step]))


# ---- OpenCode (SQLite) -----------------------------------------------------

def _opencode_messages(r: int) -> list[tuple[str, int, dict, list[dict]]]:
    """(message id, created ms, data, parts) for request ``r``."""
    ms = lambda k: int(epoch(r, k) * 1000)  # noqa: E731
    return [
        (f"msg_{r}_u", ms(0), {"role": "user"},
         [{"type": "text", "text": request_text(r)}]),
        (f"msg_{r}_c", ms(2), {"role": "assistant", "finish": "tool-calls"},
         [{"type": "text", "text": f"Looking at {r}."},
          {"type": "tool", "tool": "bash", "callID": f"call_{r}",
           "state": {"status": "completed", "input": {"command": f"ls /redacted/{r}"},
                     "output": "a\nb", "metadata": {"exit": 0, "output": "a\nb"}}}]),
        (f"msg_{r}_a", ms(5), {"role": "assistant", "finish": "stop"},
         [{"type": "text", "text": answer_text(r)}]),
    ]


def _opencode_db(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(
        """CREATE TABLE IF NOT EXISTS session (id text PRIMARY KEY, parent_id text,
               directory text, title text, time_updated integer, time_archived integer);
           CREATE TABLE IF NOT EXISTS message (id text, session_id text,
               time_created integer, data text);
           CREATE TABLE IF NOT EXISTS part (id text, message_id text, session_id text,
               data text);
           INSERT OR IGNORE INTO session VALUES ('ses_golden', NULL, '/redacted', 'Golden', 1, NULL);""")
    return con


def _opencode_insert(con, r: int, *, drop: set[str] = frozenset(), running: bool = False) -> None:
    for message_id, created, data, parts in _opencode_messages(r):
        if message_id in drop:
            continue
        con.execute("INSERT INTO message VALUES (?, 'ses_golden', ?, ?)",
                    (message_id, created, json.dumps(data)))
        for p, part in enumerate(parts):
            if running and part.get("type") == "tool":
                part = {**part, "state": {"status": "running",
                                          "input": part["state"]["input"]}}
            con.execute("INSERT INTO part VALUES (?, ?, 'ses_golden', ?)",
                        (f"{message_id}_p{p}", message_id, json.dumps(part)))
        if running:
            return          # only the prompt and the step with a call in flight


def _opencode_delete(con, r: int) -> None:
    con.execute("DELETE FROM part WHERE message_id LIKE ?", (f"msg_{r}_%",))
    con.execute("DELETE FROM message WHERE id LIKE ?", (f"msg_{r}_%",))


def apply_opencode(pseudo: pathlib.Path, step: str) -> None:
    db = pathlib.Path(str(pseudo).partition("#")[0])
    if step == "rotate":
        fresh = db.with_suffix(".rotated")
        con = _opencode_db(fresh)
        for r in range(REQUESTS):
            _opencode_insert(con, r, drop={"msg_1_c"} if r == 1 else set())
        con.commit()
        con.close()
        os.replace(fresh, db)
        return
    con = _opencode_db(db)
    if step == "grow":
        for r in (0, 1):
            _opencode_insert(con, r)
    elif step == "partial":
        _opencode_insert(con, 2, running=True)
    elif step == "complete":
        _opencode_delete(con, 2)
        for r in (2, 3):
            _opencode_insert(con, r)
    elif step in ("append", "regrow"):
        _opencode_insert(con, 4)
    elif step == "truncate":
        _opencode_delete(con, 4)
    con.commit()
    con.close()


def apply(root_path: pathlib.Path, backend: str, step: str) -> None:
    if backend == "opencode":
        apply_opencode(root_path, step)
    else:
        apply_jsonl(root_path, JSONL[backend](), step)


def jsonl_cuts(backend: str) -> tuple[bytes, list[int]]:
    """The whole transcript and every growth cut: each record boundary and
    the middle of every record (a partial last line)."""
    lines = JSONL[backend]().lines(REQUESTS)
    cuts, offset = [], 0
    for line in lines:
        cuts.append(offset + len(line) // 2)
        offset += len(line)
        cuts.append(offset)
    return b"".join(lines), cuts
