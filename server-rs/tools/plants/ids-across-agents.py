#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

An id that two agents both use is each one's own (R62 and R63 of the Python Host): an app's message id, the ids a
tool's answer is named by, a waiting message named by its id, rows taken over from another Host, and the rows a
taken-up transcript becomes.
"""
import runner

RUNS = {
    'host': ['test', '--test', 'host', '--', 'an_id_two_agents_both_use_is_each_ones_own'],
    'past': ['test', '-p', 'clarp-host', '--lib', 'the_turns_of_a_transcript_become_rows'],
}
E = 'clarp-host/src/edge.rs'
F = 'clarp-host/src/fleet.rs'
P = 'clarp-host/src/past.rs'
# NOT PLANTED, each with its reason:
#   that a row is written only into its own agent's log: every write goes to the log the agent opened in its own
#     directory (planted below as one directory for all), and there is no other way into a log.
PLANTS = [
    (E, 'host', 'a waiting message is acted on by an id two agents hold', '            if named.is_some() {\n                return Err(ApiError::new(StatusCode::CONFLICT, AMBIGUOUS_WAITING));\n            }\n', ''),
    (E, 'host', 'an id one agent alone holds waiting is refused as well', '            if named.is_some() {', '            if true {'),
    (F, 'host', 'every agent keeps its conversation in one log', '        dir: works.config.data_dir.join("agents").join(&record.agent_id),', '        dir: works.config.data_dir.join("agents").join("all"),'),
    (P, 'past', 'a taken-up row is named whoever took it up', '        let digest = Sha256::digest(format!("{agent_id}\\0{session}\\0{number}").as_bytes());', '        let digest = Sha256::digest(format!("{session}\\0{number}").as_bytes());'),
    (P, 'past', 'a taken-up row is named whatever session it came from', '        let digest = Sha256::digest(format!("{agent_id}\\0{session}\\0{number}").as_bytes());', '        let digest = Sha256::digest(format!("{agent_id}\\0{number}").as_bytes());'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
