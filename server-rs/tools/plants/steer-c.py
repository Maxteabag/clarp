#!/usr/bin/env python3
"""Planted differences for Claude follow-ups in issue 491, part C."""

import runner

RUNS = {
    "shim": ["test", "-p", "clarp-shim", "--test", "shim", "--", "streaming_input_closes_at_the_first_top_level_stdout_result"],
    "ack": ["test", "-p", "clarp-shim", "--test", "shim", "--", "stdin_refuses_a_write_when_the_tool_has_closed_its_read_end"],
    "stream": ["test", "-p", "clarp-kernel", "--lib", "only_the_live_tool_window_takes_new_input"],
    "journal": ["test", "-p", "clarp-kernel", "--lib", "a_follow_up_enters_on_its_echo_and_an_unread_one_still_obeys_stop"],
    "reveal": ["test", "-p", "clarp-actor", "--lib", "revealing_a_turn_sends_its_old_rows_through_a_delta"],
    "host": ["test", "-p", "clarp-host", "--test", "host", "--", "a_claude_follow_up_enters_at_the_echo_and_is_answered_once"],
}
S = "clarp-shim/src/main.rs"
K = "clarp-kernel/src/steering.rs"
A = "clarp-kernel/src/agent.rs"
J = "clarp-kernel/src/journal.rs"
R = "clarp-actor/src/lib.rs"
C = "clarp-actor/src/steering.rs"
L = "clarp-host/src/launcher.rs"
H = "clarp-host/src/agent.rs"
D = "clarp-actor/src/conversation.rs"

PLANTS = [
    (S, "shim", "the initial prompt closes streaming stdin", "if args.stdin_until.is_none() {", "if args.stdin_until.is_some() {"),
    (S, "shim", "no result closes stdin", 'event.get("type").and_then(serde_json::Value::as_str) == Some(kind)', 'event.get("type").and_then(serde_json::Value::as_str) != Some(kind)'),
    (S, "ack", "queue admission is success even with a broken pipe", " && receipt.recv() == Ok(true)", ""),
    (K, "stream", "text-only turns also take input", "!self.input_ended && !self.tools.is_empty()", "!self.input_ended"),
    (K, "stream", "the input fence can be reopened", "!self.input_ended && !self.tools.is_empty()", "!self.tools.is_empty()"),
    (K, "stream", "the first tool result clears every running tool", "self.tools.remove(id);", "self.tools.remove(id); self.tools.clear();"),
    (K, "stream", "a tool still runs after its result", "self.tools.remove(id);", "let _ = id;"),
    (K, "stream", "user replays are ordinary output", 'if event["type"] == "user" && event["isReplay"] == true {', 'if event["type"] == "user" && event["isReplay"] == false {'),
    (K, "stream", "the wrong field identifies a replay", 'nonempty(&event["uuid"])', 'nonempty(&event["session_id"])'),
    (K, "stream", "results lose their prompt UUIDs", 'event["user_message_uuids"]', 'event["uuid"]'),
    (A, "journal", "the first echo records the same read twice", "join.stage = Stage::Read(*at_ms);", "join.stage = Stage::Pending;"),
    (A, "journal", "a finished tool still holds its unread follow-ups", "Turn::Settled(ended) => {\n                    self.joins.clear();", "Turn::Settled(ended) => {"),
    (J, "journal", "the joined user row is never entered", "self.enter(*turn, join.message_seq, now_ms);", "let _ = (turn, join);"),
    (J, "journal", "the host lets a user edit the text already offered to stdin", "if self.agent.joins().contains_key(&turn) {", "if !self.agent.joins().contains_key(&turn) {"),
    (C, "host", "a different UUID is taken as this echo", "follow.join.uuid == uuid && follow.stage == Stage::Pending", "follow.join.uuid != uuid && follow.stage == Stage::Pending"),
    (C, "host", "every echo joins the original answer", "if self.parent.is_some() {\n                        if self.separate.is_none()", "if self.parent.is_none() {\n                        if self.separate.is_none()"),
    (C, "host", "no matching UUID can close the original answer", "prompt.as_str() == Some(uuid)", "prompt.as_str() != Some(uuid)"),
    (C, "host", "a read receipt need not make the row due", "batch.command(Command::JoinRead { turn }, now_ms)", "batch.command(Command::JoinRead { turn: TurnId(0) }, now_ms)"),
    (C, "host", "the queue is never told which follow-up was read", "out.joined_reads.push(JoinedRead", "drop(JoinedRead"),
    (H, "host", "several echoes in one batch all get its last queue revision", 'event["queue_revision"] = json!(read.queue.revision);', ''),
    (C, "host", "a read user follow-up leaves the original turn hidden", "let revealed = !self.mixed && conversation::loosen(batch, into, now_ms)?;", "let revealed = false;"),
    (D, "reveal", "revealed old rows are missing from the next delta", "if stamped.insert(id.to_owned()) {", "if !stamped.insert(id.to_owned()) {"),
    (R, "host", "the running turn stays hidden after its audience changes", ".set_turn_audience(clarp_kernel::coordination::USER, at)", ".set_turn_audience(clarp_kernel::coordination::AGENTS, at)"),
    (R, "host", "a separate reply keeps the trace of the first prompt", "reply.trace_id.clone_from(&separate.message.trace_id);", ""),
    (L, "host", "opting into replay does not keep stdin open", 'command.args(["--stdin-until", "result"]);', ""),
]

if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
