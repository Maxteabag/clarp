#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

Who stops or repairs an agent and why (contract 61, `turn_slot_repair`): the kernel's rule of who acts, why, the
goals' pause and the provenance on a state (`control.rs`, held by the Python Host's own functions in
`turn_slot_repair_vectors.rs`); the log that keeps it (`turn.rs`, `agent.rs`, `journal.rs`: today's lines read as
they were, a note written as a kind of its own); the Host that finds out whose turn asks and answers in the Python
Host's words (`controls.rs`, `edge.rs`, the agents' tool in `admin_tool.rs`, the state and activity in `wire.rs`).
Held by the Host's test of Stops and repairs by a claim, a phone and an agent's own turn, and by the test that
nothing here holds an agent busy with no process behind it.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'turn_slot_repair_vectors'],
    'lines': ['test', '-p', 'clarp-kernel', '--lib', 'journal::tests::a_stop_written_by'],
    'host': ['test', '--test', 'host', '--', 'a_stop_and_a_repair_say_who_acted_and_why'],
    'nobody': ['test', '--test', 'host', '--', 'nothing_holds_an_agent_busy_with_no_process_behind_it'],
    'hush': ['test', '--test', 'host', '--', 'only_the_users_stop_silences_what_an_agent_had_yet_to_say'],
}
K = 'clarp-kernel/src/control.rs'
A = 'clarp-kernel/src/agent.rs'
T = 'clarp-kernel/src/turn.rs'
J = 'clarp-kernel/src/journal.rs'
C = 'clarp-host/src/controls.rs'
E = 'clarp-host/src/edge.rs'
W = 'clarp-host/src/wire.rs'
D = 'clarp-host/src/admin_tool.rs'
L = 'clarp-host/src/launcher.rs'
S = 'clarp-actor/src/lib.rs'
# NOT PLANTED, each with its reason:
#   that a Stop written before who stopped was kept tells nothing on the activity (`provenance` asks for
#     `stop_actor`): such a detail has none of the keys, so it tells nothing either way.
#   that `--from` makes the tool's claim `agent:<it>`: the tool asks from an agent's own turn, whose token
#     replaces any claim, and the Host test asks it so.
#   that a Stop or a new state clears a repair's note (`Paused`, `StopRequested`, `StartedAgain`): the Host test
#     holds the clearing by the agent's next turn; no story stops an agent right after it was repaired.
#   that the goals are paused in the words of who stopped: the words are the kernel's (`paused_by`, held by the
#     vectors), and no Host test pauses a goal by a Stop with a reason.
PLANTS = [
    # Who acts and why.
    (K, 'vectors', 'nobody named is nobody, not the user', '        "" => USER.to_string(),', '        "" => String::new(),'),
    (K, 'vectors', 'a claim keeps its blanks', '    match python::first(python::strip(&actor), ACTOR_MOST) {', '    match python::first(&actor, ACTOR_MOST) {'),
    (K, 'vectors', 'a claim is cut at 100 signs', 'const ACTOR_MOST: usize = 80;', 'const ACTOR_MOST: usize = 100;'),
    (K, 'vectors', 'a reason keeps its blanks', '    python::first(python::strip(&reason), REASON_MOST).to_string()', '    python::first(&reason, REASON_MOST).to_string()'),
    (K, 'vectors', 'a reason is cut at 250 signs', 'const REASON_MOST: usize = 200;', 'const REASON_MOST: usize = 250;'),
    (K, 'vectors', 'what is no text is read as JSON writes it', '        Some(value) if python::truthy(value) => python::repr(value),', '        Some(value) if python::truthy(value) => value.to_string(),'),
    (K, 'host', "an agent's own turn is not taken for the agent", '        Some(session) => (format!("agent:{session}"), true),', '        Some(_) => (claimed.to_string(), false),'),
    (K, 'host', 'a paired phone acting as the user is not known to be the user', '        None if device && claimed == USER => (USER.to_string(), true),', '        None if device && claimed == USER && claimed.is_empty() => (USER.to_string(), true),'),
    (K, 'vectors', 'a paired phone is believed whatever it claims', '        None if device && claimed == USER => (USER.to_string(), true),', '        None if device => (claimed.to_string(), true),'),
    (K, 'vectors', "an agent's own Stop is called unverified in its goals' pause", '        format!("Stopped by {}", control.actor)', '        format!("Stopped by {} (unverified)", control.actor)'),
    (K, 'vectors', "the goals' pause leaves the reason out", '        "" => by,', '        _ if true => by,\n        "" => by,'),
    (K, 'host', 'a state says no Stop is known to be so', '            "stop_actor_verified": control.verified,', '            "stop_actor_verified": false,'),
    (K, 'host', "a repair's note names no turn it gave back", '            "released_trace",\n        ],', '        ],'),
    (K, 'vectors', "a claim to be an agent makes a Stop no user's", '        !(self.verified && self.actor.starts_with("agent:"))', '        !self.actor.starts_with("agent:")'),
    (K, 'vectors', "an agent's own Stop is the user's", '        !(self.verified && self.actor.starts_with("agent:"))', '        !(self.verified && self.actor.starts_with("user"))'),
    # The log.
    (T, 'lines', "a Stop that is the user's claim is written with more than before", '        #[serde(default, skip_serializing_if = "is_false")]\n        verified: bool,', '        #[serde(default)]\n        verified: bool,'),
    (A, 'host', 'who asked a running turn to stop is no longer known when it has gone', '                self.stopping = Some(Control {\n                    actor: by.clone(),\n                    verified: *verified,', '                self.stopping = Some(Control {\n                    actor: by.clone(),\n                    verified: false && *verified,'),
    (A, 'host', 'a repair keeps no note', '                self.note = Some(Note {\n                    by,', '                let _skipped = Some(Note {\n                    by,'),
    (A, 'host', "a turn leaves a repair's note standing", '        if !matches!(change, TurnEvent::Accepted { .. }) {\n            self.note = None;\n        }', '        if !matches!(change, TurnEvent::Accepted { .. }) {\n            let _ = &self.note;\n        }'),
    (J, 'lines', 'a note is written as an event a build that does not know it would refuse', '        AgentEvent::Repaired { .. } => kind::NOTE,', '        AgentEvent::Repaired { .. } => kind::AGENT_EVENT,'),
    (J, 'lines', 'a note is not read back', '                    kind::AGENT_EVENT | kind::NOTE => {', '                    kind::AGENT_EVENT => {'),
    # Nothing holds an agent busy with no process behind it.
    (L, 'nobody', 'a shim that is gone is not told of', '    tell(Told::SpoolChanged);\n    tell(Told::Gone);\n}', '    tell(Told::SpoolChanged);\n}'),
    (S, 'nobody', 'a turn whose shim is gone without a word is left running', '        let command = Command::TurnEnded {\n            turn,\n            outcome: RunOutcome::Interrupted {\n                reason: "the turn\'s process is gone and did not say how it went".to_string(),', '        let _ = turn;\n        let turn = TurnId(u64::MAX);\n        let command = Command::TurnEnded {\n            turn,\n            outcome: RunOutcome::Interrupted {\n                reason: "the turn\'s process is gone and did not say how it went".to_string(),'),
    # The Host.
    (C, 'host', 'the token of a turn is not asked whose it is', '        None => None,\n        Some(token) => {', '        None => None,\n        Some(_) if true => None,\n        Some(token) => {'),
    (C, 'host', 'a token whose agent cannot be said is taken for nobody', '            let found =\n                found.map_err(|why| ApiError::retryable(StatusCode::SERVICE_UNAVAILABLE, why.0))?;', '            let found = found.unwrap_or(None);'),
    (C, 'host', "a repair's agent is looked for with the blanks around its name", '    let session = python::strip(&session).to_string();', '    let session = session.clone();'),
    (C, 'host', 'a repair needs no reason', '    if by.reason.is_empty() {', '    if by.reason.is_empty() && by.reason.len() > 1 {'),
    (C, 'host', 'a repair does not ask the agent', '    member.agent.repair(by.clone()).await?;', '    let _ = &member;'),
    (C, 'host', 'a repair says it started a queue', '        "queue_recovered": 0,', '        "queue_recovered": 1,'),
    (E, 'host', 'a paired phone is not passed on as such', '        matches!(principal, Principal::Device { .. }),', '        matches!(principal, Principal::Administrator),'),
    (E, 'host', 'a repair takes a body that is no object for an empty one', '    let Ok(Value::Object(data)) = serde_json::from_slice::<Value>(&body) else {', '    let Ok(Value::Object(data)) = serde_json::from_slice::<Value>(&body).map(|value| if value.is_array() { Value::Object(Default::default()) } else { value }) else {'),
    (W, 'host', "a repair's note is not on the activity", '        (Some(note), _) => Some(control::marked(control::REPAIRED, &note.by, &note.released)),', '        (Some(_), _) => None,'),
    (W, 'host', "a repair's note is not on the state", '        _ if status.note.is_some() => {', '        _ if status.note.is_some() && false => {'),
    (D, 'host', "the agents' tool asks without its turn's token", '    let token = token.map(String::as_str).filter(|token| !token.is_empty());', '    let token = token.map(String::as_str).filter(|token| token.is_empty());'),
    (D, 'host', "the agents' tool prints the answer in another order", '                        "ok": stopped.ok, "terminated": stopped.terminated,', '                        "terminated": stopped.terminated, "ok": stopped.ok,'),
    # Speech not yet made: only the user's Stop drops it.
    (S, 'hush', "every Stop silences the agent, an agent's own included", '        let silenced = by.is_the_users();', '        let silenced = { let _ = &by; true };'),
    (S, 'hush', 'no Stop silences the agent', '        let silenced = by.is_the_users();', '        let silenced = { let _ = &by; false };'),
    # The pause keeps the Stop's Control: the one line of an idle agent's Stop.
    (A, 'lines', 'the pause forgets whether the Host knew who stopped', '                work.record(AgentEvent::Paused {\n                    by: by.actor,\n                    verified: by.verified,', '                work.record(AgentEvent::Paused {\n                    by: by.actor,\n                    verified: false,'),
    (A, 'lines', 'the pause forgets why', '                    verified: by.verified,\n                    reason: by.reason,\n                    at_ms: now_ms,\n                });\n            }\n            Command::Repair', '                    verified: by.verified,\n                    reason: String::new(),\n                    at_ms: now_ms,\n                });\n                let _ = by.reason;\n            }\n            Command::Repair'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
