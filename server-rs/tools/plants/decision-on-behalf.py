#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

A request answered in the user's place: the owner's list of the sessions that may (`owner.rs`), the route that
hands it and the session of the turn that asks to the kernel's rule (`library.rs`; the turn itself is the
foundation `turn-identity.py` holds), the kernel's refusal of the asker answering itself (`decisions.rs`), and the
agents' own tool that answers so (`artifact_tool.rs`). Held by the vectors of the Python Host's functions
(`artifact_vectors.rs`, the owner's list in `owner.rs`) and by the Host's test of a front desk that answers in
its own turn.
"""
import runner

RUNS = {
    'artifacts': ['test', '--test', 'artifact_vectors'],
    'owner': ['test', '-p', 'clarp-host', '--lib', 'the_sessions_that_answer_in_the_owners_place'],
    'host': ['test', '--test', 'host', '--', 'a_session_the_owner_names_answers_a_request_in_his_place'],
}
L = 'clarp-host/src/library.rs'
O = 'clarp-host/src/owner.rs'
T = 'clarp-host/src/artifact_tool.rs'
D = 'clarp-kernel/src/artifacts/decisions.rs'
# NOT PLANTED, each with its reason:
#   that the janitors and those put away count among the agents, for whether the session that answers is an
#     agent's: no janitor is the owner's front desk in a test, and the session that answers is the caller's own,
#     so it is an agent's whenever it may answer at all (the kernel's vectors hold the refusal of a session
#     nobody has).
#   that the tool sends only the answer of its body: what else the body holds is not read by the route.
PLANTS = [
    # The route hands the kernel's rule what it needs.
    (L, 'host', "the owner's list is not read", '                    let designated = owners.read().as_ref().map(crate::owner::on_behalf_sessions);', '                    let designated = owners.read().as_ref().map(|_| Vec::<String>::new());'),
    (L, 'host', 'the session that says it answers is taken for the turn that asks', '                    let verified = caller.as_ref().map(|member| member.record.session.clone());', '                    let verified = caller.as_ref().map(|_| acting.trim().to_string());'),
    (L, 'host', 'the turn that asks is not told to the rule', '                        verified.unwrap_or_default(),\n', '                        verified.map(|_| String::new()).unwrap_or_default(),\n'),
    # The kernel's rule.
    (D, 'host', 'the asker answers its own request', '            if artifact.session == acting {\n', '            if artifact.session == acting && acting.is_empty() {\n'),
    # The agents' own tool.
    (T, 'host', 'the tool answers without its turn', '                    match library::decide(app, &rest, turn.map(String::as_str), &sent).await {', '                    match library::decide(app, &rest, None, &sent).await {'),
    (T, 'host', 'the tool does not say for whom it answers', '                sent.insert("on_behalf_session".to_string(), json!(session));', '                let _ = session;'),
    # The owner's list.
    (O, 'owner', 'the names of the list keep their blanks', '    for session in listed.iter().filter_map(Value::as_str).map(str::trim) {', '    for session in listed.iter().filter_map(Value::as_str) {'),
    (O, 'owner', 'a name of the list comes as often as it is written', '        if !session.is_empty() && !sessions.iter().any(|known| known == session) {', '        if !session.is_empty() {'),
    (O, 'owner', 'an empty name is a name', '        if !session.is_empty() && !sessions.iter().any(|known| known == session) {', '        if !sessions.iter().any(|known| known == session) {'),
    (O, 'owner', 'a text is a list of one', '    let Some(listed) = listed.and_then(Value::as_array) else {\n        return Vec::new();\n    };', '    let Some(listed) = listed.and_then(Value::as_array) else {\n        return listed.and_then(Value::as_str).map(|one| vec![one.to_string()]).unwrap_or_default();\n    };'),
    (O, 'host', 'the list is read from another table', '    let listed = file.get("agents").and_then(Value::as_table);', '    let listed = file.get("server").and_then(Value::as_table);'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
