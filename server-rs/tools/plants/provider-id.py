#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The id the provider gave an agent's message, as a rollout of Codex names it (R107; the Python Host's
`codex_transcript._handle_modern_item` and `_append_visible_message`): read off a modern item of the agent's,
never off a user's, kept from the first record of one message that names one, and none where it is empty.
"""
import runner

RUNS = {
    'vectors': ['test', '-p', 'clarp-backend', '--test', 'codex_history'],
}
R = 'clarp-backend/src/history/rollout.rs'
# NOT PLANTED: that this Host reads no transcript into rows beside what a tool's stream said. There is no such
#   reading to plant a wrong rule in: rows of a transcript are written only when an agent begins with a session,
#   and refused once it has a history. The Host test `an_agent_takes_up_a_session_codex_kept` shows it whole.
PLANTS = [
    (R, 'vectors', "an agent's message names no id", '                    text_of(&[of("id")])\n',
     '                    String::new()\n'),
    (R, 'vectors', "a user's message names the id of its item", '                let provider_id = if kind == "agent_message" {',
     '                let provider_id = if true {'),
    (R, 'vectors', 'a later record of one message replaces the id of the first',
     '            if !provider_id.is_empty() && !python::truthy(&last["provider_id"]) {',
     '            if !provider_id.is_empty() {'),
    (R, 'vectors', 'a later record of one message never gives it its id',
     '            if !provider_id.is_empty() && !python::truthy(&last["provider_id"]) {', '            if false {'),
    (R, 'vectors', 'an empty id is named', '    if !provider_id.is_empty() {\n        turn["provider_id"]',
     '    if true {\n        turn["provider_id"]'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
