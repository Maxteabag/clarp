#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

A prompt's copy in its tool's stream is never a row of its own (what the Python Host's PR 446 matches has no
counterpart here): the readers of Claude Code and of Antigravity make no row of the user's, a message is filed
under its app's id and not under its words, and an answer is written under its own turn.
"""
import runner

RUNS = {
    'host': ['test', '--test', 'host', '--', 'a_prompts_copy_in_its_tools_stream_is_never_a_row_of_its_own'],
}
C = 'clarp-backend/src/claude.rs'
G = 'clarp-backend/src/agy.rs'
E = 'clarp-host/src/edge.rs'
A = 'clarp-actor/src/lib.rs'
# NOT PLANTED, each with its reason:
#   that a reader names the turn its answer belongs to (`turn: self.turn` in Claude's reader; planted on its first
#     run, MISSED): the actor writes every answer under the trace of the turn it is running, whatever the reader
#     names; that is planted instead.
PLANTS = [
    (C, 'host', "Claude's copy of the user's words is read as an answer", 'self.on_user(&event["message"], &event["toolUseResult"], t, out);', 'self.on_assistant(&event, t, out);'),
    (G, 'host', "Antigravity's step of the user's input is read as an answer", '            "agent_response" => {', '            "agent_response" | "user_input" => {'),
    (E, 'host', "a message is filed under its words", '    let (id, marked) = Message::filed(minted.unwrap_or(&trace_id));', '    let (id, marked) = Message::filed(&text);'),
    (A, 'host', "an answer is written without its turn's trace", '                            reply.trace_id = live.trace_id.clone();', '                            reply.trace_id = None;'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
