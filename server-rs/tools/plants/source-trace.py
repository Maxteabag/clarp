#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The turn an artifact names (`source_trace_id`, the Python Host's PR 470, R102): the trace of the
running turn whose token the request presents, when that turn is the artifact's own agent's; the
empty text otherwise; kept by a new artifact only. Each rule is caught by the Host test that holds
every case to the Python Host's vectors (`tools/source-trace-vectors.py`), or by the actor's test.
"""
import runner

RUNS = {
    'host': ['test', '--test', 'host', 'an_artifact_names_the_running_turn_that_published_it'],
    'actor': ['test', '--test', 'actor', 'source_trace_a_turn_names_its_trace_only_while_it_runs'],
}
K = 'clarp-kernel/src/artifacts.rs'
S = 'clarp-kernel/src/artifacts/render_spec.rs'
T = 'clarp-host/src/turn_identity.rs'
A = 'clarp-actor/src/lib.rs'
L = 'clarp-host/src/library.rs'
F = 'clarp-host/src/artifact_tool.rs'
# NOT PLANTED, each with its reason:
#   turn_identity.rs: a key or a turn that cannot be read answered as nobody (the 503): a Host test cannot make
#     the key or an agent's log unreadable; the answer is the one every reader of a turn's token gives.
#   decisions.rs: a request to the user naming its turn: it is made by its own route, which takes no token for it;
#     nothing there could name one.
PLANTS = [
    (K, 'host', 'a new artifact keeps no trace', 'source_trace_id: new.source_trace_id.clone(),', 'source_trace_id: String::new(),'),
    (K, 'host', 'the trace kept is not shown', '"source_trace_id": self.source_trace_id,', '"source_trace_id": "",'),
    (S, 'host', 'a new render spec keeps no trace', 'source_trace_id: asked.source_trace_id.clone(),', 'source_trace_id: String::new(),'),
    (S, 'host', "a render spec's revision names the turn that revised it",
     '            updated_at: ctx.now_ms.max(old.updated_at + 1),\n            ..old.clone()',
     '            updated_at: ctx.now_ms.max(old.updated_at + 1),\n            source_trace_id: asked.source_trace_id.clone(),\n            ..old.clone()'),
    (T, 'host', "another agent's turn names the artifact", 'if claim.agent_id != agent_id {', 'if false {'),
    (A, 'host', "whatever turn runs is named, not the token's",
     'Some(Turn::Running(live)) if live.id.0 == turn => live.message_seq,', 'Some(Turn::Running(live)) => live.message_seq,'),
    (A, 'actor', 'a turn that does not run is named', '            _ => return Ok(None),\n        };\n        let message = self.message_at(message_seq)?;',
     '            _ => return Ok(Some("a turn that ran".to_string())),\n        };\n        let message = self.message_at(message_seq)?;'),
    (A, 'host', 'a settled turn still names its trace',
     '            _ => return Ok(None),\n        };\n        let message = self.message_at(message_seq)?;',
     '            _ if matches!(self.standing_of(turn)?, Standing::Ended { .. }) => {\n                return Ok(Some("settled".to_string()));\n            }\n            _ => return Ok(None),\n        };\n        let message = self.message_at(message_seq)?;'),
    (A, 'actor', "a running turn without a trace is named by something", 'Ok(Some(message.trace_id.unwrap_or_default()))',
     'Ok(Some(message.trace_id.unwrap_or_else(|| "untraced".to_string())))'),
    (L, 'host', "the request's token is not read", '    let turn = headers\n        .get(crate::turn_identity::HEADER)\n        .and_then(|value| value.to_str().ok());\n    let (status, answer) = posted(',
     '    let turn: Option<&str> = headers\n        .get(crate::turn_identity::HEADER)\n        .and(None);\n    let (status, answer) = posted('),
    (F, 'host', "the tool presents no turn's token", '    call.env\n        .get(crate::turn_identity::TURN_ENV)\n        .map(String::as_str)', '    call.env.get(crate::turn_identity::TURN_ENV).and(None)'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
