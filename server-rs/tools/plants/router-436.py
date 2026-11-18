#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The router's holds, commands and names as the Python Host has them since its PR 436 (18 Nov 2026): a command is
never held; a held message belongs to the session it was made in (the packet shows it, a confirmation claims it and
a renewal keeps it, only there); a name given in `target_session` or in the payload is the target, names that
disagree are asked about and a name nobody has makes no agent; names resolve as voice names, case and accents aside,
agents that were put away and janitors counted. The kernel's rules (`router/flow.rs`, `router/packet.rs`), held by
`tests/router_flow_vectors.rs`.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'router_flow_vectors'],
}
K = 'clarp-kernel/src/router/flow.rs'
P = 'clarp-kernel/src/router/packet.rs'
# NOT PLANTED, each with its reason:
#   that of two names given the first is the agent (`found.first()`): where two are given and the command goes on,
#     they are the same agent.
#   the words a command is refused in where no name is given and the agent spoken to is none: they are said as that
#     agent, and there is none to say them as (the rule that it is refused is planted).
#   the phrase key a refusal or "no longer waiting" is said under (`self.as_agent(.., "", ..)`): no phrase is kept
#     ready-spoken on this Host, and what is said aloud is held by its words, its voice and whom it is said as.
#   that a command to start an agent again or to make one leaves whom the decision is kept for as it was: the agent
#     started is told by the Host (`Done::Started`), and a start that fails is no story of a name.
#   that the model's words of a status are said without the blanks at their ends, and that a command naming none
#     acts on the agent spoken to: both are so as the decision is read and checked (`Decision::read` strips its
#     texts; `command_check` names the agent of every command it lets through), and are not asked again after.
#   that an agent that was put away takes no command: no packet shows one, and the command is refused as for an agent
#     of no call (the reference also asks `archived_at`, which it never finds there).
CHECKED = '''    if !sure {
        return Err(UNSURE_COMMAND);
    }
    let of_agents = decision.kind == AGENT_CONTROL;
    if of_agents && asked == "fork" {
        // No source of a fork is taken from the router.
        return Err(FORK_IN_APP);
    }
    if of_agents && asked == "create" {
        return Ok(None);
    }
    let mut names = vec![python::strip(&decision.target_session)];
    if of_agents {
        let payload = decision.control_payload.as_ref();
        let given = payload.and_then(|payload| payload.get("name"));
        names.push(given.and_then(Value::as_str).map_or("", python::strip));
    }
    names.retain(|name| !name.is_empty());
    let agent = if names.is_empty() {
        lookup(agents, requested)
    } else {
        let mut found: Vec<&Member> = Vec::new();
        for name in names {
            match agents_named(agents, name).as_slice() {
                [] => return Err(NO_SUCH_AGENT),
                [one] => found.push(one),
                _ => return Err(WHICH_AGENT),
            }
        }
        // The two names given disagree.
        if found
            .iter()
            .any(|agent| agent.agent_id != found[0].agent_id)
        {
            return Err(WHICH_AGENT);
        }
        found.first().copied()
    };
    let Some(agent) = agent else {
        return Err(NO_SUCH_AGENT);
    };'''
LATE_BAR = '''    let of_agents = decision.kind == AGENT_CONTROL;
    if of_agents && asked == "fork" {
        // No source of a fork is taken from the router.
        return Err(FORK_IN_APP);
    }
    if of_agents && asked == "create" {
        return Ok(None);
    }
    let mut names = vec![python::strip(&decision.target_session)];
    if of_agents {
        let payload = decision.control_payload.as_ref();
        let given = payload.and_then(|payload| payload.get("name"));
        names.push(given.and_then(Value::as_str).map_or("", python::strip));
    }
    names.retain(|name| !name.is_empty());
    let agent = if names.is_empty() {
        lookup(agents, requested)
    } else {
        let mut found: Vec<&Member> = Vec::new();
        for name in names {
            match agents_named(agents, name).as_slice() {
                [] => return Err(NO_SUCH_AGENT),
                [one] => found.push(one),
                _ => return Err(WHICH_AGENT),
            }
        }
        // The two names given disagree.
        if found
            .iter()
            .any(|agent| agent.agent_id != found[0].agent_id)
        {
            return Err(WHICH_AGENT);
        }
        found.first().copied()
    };
    if !sure {
        return Err(UNSURE_COMMAND);
    }
    let Some(agent) = agent else {
        return Err(NO_SUCH_AGENT);
    };'''
NO_AGENT = '''    let Some(agent) = agent else {
        return Err(NO_SUCH_AGENT);
    };'''
REFUSED = '''                    decision.kind = CLARIFY.to_string();
                    action = Some(CLARIFY);
                    if !routing.fallback_request {
                        let said = self.as_agent(requested, refusal, "", around);'''
GONE = '''                        action = Some(CLARIFY);
                        decision.kind = CLARIFY.to_string();
                        let said = self.as_agent(requested, HELD_MESSAGE_GONE, "", around);'''
HOLD_WAITING = '''            held.pending_id == decision.pending_id
                && held.status == "pending"
                && held.expires_at > now
                && held.requested_session == requested'''
CLAIM_WAITING = '''                && held.expires_at > now_ms
                && held.requested_session == requested'''
STATUS_REFUSED = '''                action = Some(CLARIFY);
                target = requested.to_string();
                let refusal'''
SWITCHED = '''                        session: on.clone(),
                    }));
                    target = on;'''
STOPPED = '''                        agent_id: agent.agent_id.clone(),
                    }));
                    target = on;
                }
                "create"'''
UNKNOWN = '''                    effects.extend(self.as_agent(requested, words, "", around).map(Effect::Say));
                    target = on;'''
PLANTS = [
    # Names as voice names: the fold.
    (K, 'vectors', 'a name is compared in its case as it is', '    let folded = python::casefold(name);', '    let folded = name.to_string();'),
    (K, 'vectors', "a name's case is lowered, not folded", '    let folded = python::casefold(name);', '    let folded = name.to_lowercase();'),
    (K, 'vectors', 'a name is taken apart canonically only', '        .nfkd()', '        .nfd()'),
    (K, 'vectors', 'accents count', '        .filter(|sign| canonical_combining_class(*sign) == 0)', '        .filter(|sign| canonical_combining_class(*sign) == 0 || true)'),
    (K, 'vectors', 'a blank the decomposition leaves at the end of a name stays', '    python::strip(&bare).to_string()', '    bare'),
    # The agents a name stands for.
    (K, 'vectors', 'a session as it is is not looked up first', '    if let Some(exact) = lookup(agents, name) {', '    if let Some(exact) = lookup(agents, name).filter(|_| false) {'),
    (K, 'vectors', 'a name that folds to nothing names the agents with no name', '    if key.is_empty() {\n        return Vec::new();\n    }\n', ''),
    (K, 'vectors', 'a name is not compared with sessions', '    let named = |agent: &&Member| fold(&agent.session) == key || fold(&agent.persona) == key;', '    let named = |agent: &&Member| fold(&agent.persona) == key;'),
    (K, 'vectors', 'a name is not compared with names', '    let named = |agent: &&Member| fold(&agent.session) == key || fold(&agent.persona) == key;', '    let named = |agent: &&Member| fold(&agent.session) == key;'),
    (K, 'vectors', 'agents that were put away are not counted', '    agents.iter().filter(named).collect()', '    agents.iter().filter(|agent| !agent.archived).filter(named).collect()'),
    (K, 'vectors', 'janitors are not counted', '    agents.iter().filter(named).collect()', '    agents.iter().filter(|agent| agent.voice_target).filter(named).collect()'),
    # Whether a command is carried out.
    (K, 'vectors', 'a command not sure enough is carried out', '    if !sure {', '    if !sure && decision.confidence < 0.0 {'),
    (K, 'vectors', 'names are checked before the bar', CHECKED, LATE_BAR),
    (K, 'vectors', 'a fork the router is told is not refused first', '    if of_agents && asked == "fork" {', '    if of_agents && asked == "fork" && asked.is_empty() {'),
    (K, 'vectors', 'fork, as a command to the router, is refused as a fork', '    if of_agents && asked == "fork" {', '    if asked == "fork" {'),
    (K, 'vectors', 'create, as a command to the router, acts on nobody', '    if of_agents && asked == "create" {', '    if asked == "create" {'),
    (K, 'vectors', 'a new agent must be named as one there is', '    if of_agents && asked == "create" {', '    if of_agents && asked == "create" && asked.is_empty() {'),
    (K, 'vectors', 'the payload of a command to the router is a name', '    if of_agents {\n        let payload', '    if true {\n        let payload'),
    (K, 'vectors', "the payload's name is not looked at", '        names.push(given.and_then(Value::as_str).map_or("", python::strip));', '        names.push(given.and_then(Value::as_str).filter(|_| false).map_or("", python::strip));'),
    (K, 'vectors', "the payload's name keeps its blanks", '        names.push(given.and_then(Value::as_str).map_or("", python::strip));', '        names.push(given.and_then(Value::as_str).unwrap_or_default());'),
    (K, 'vectors', "a payload's name that is no text is a name", '        names.push(given.and_then(Value::as_str).map_or("", python::strip));', '        names.push(given.map_or("", |given| given.as_str().map_or("7", python::strip)));'),
    (K, 'vectors', 'a blank name is a name', '    names.retain(|name| !name.is_empty());', '    names.retain(|_| true);'),
    (K, 'vectors', 'naming none, the command is for nobody', '        lookup(agents, requested)\n    } else {', '        None\n    } else {'),
    (K, 'vectors', 'a name nobody has is asked about', '                [] => return Err(NO_SUCH_AGENT),', '                [] => return Err(WHICH_AGENT),'),
    (K, 'vectors', 'a name two agents have is the first of them', '                _ => return Err(WHICH_AGENT),', '                many => found.push(many[0]),'),
    (K, 'vectors', 'names that disagree are carried out', '            .any(|agent| agent.agent_id != found[0].agent_id)', '            .any(|agent| agent.agent_id.is_empty())'),
    (K, 'vectors', 'a command said to no agent and naming none goes on', NO_AGENT, '    let Some(agent) = agent else {\n        return Ok(None);\n    };'),
    (K, 'vectors', 'a janitor is refused as one of no call', '    if !agent.voice_target {', '    if !agent.voice_target && agent.archived {'),
    (K, 'vectors', 'an agent the packet does not show takes commands', '    if !shown.contains(&agent.session.as_str()) {', '    if shown.is_empty() {'),
    (K, 'vectors', 'a command acts on the name as it was said', '    Ok(Some(agent.session.clone()))', '    Ok(Some(decision.target_session.clone()))'),
    # A refused command.
    (K, 'vectors', 'a refused command keeps its kind', REFUSED, REFUSED.replace('                    decision.kind = CLARIFY.to_string();\n', '')),
    (K, 'vectors', 'a refused command is held while its user is asked', REFUSED, REFUSED.replace('                    action = Some(CLARIFY);\n', '')),
    (K, 'vectors', 'a refusal is said where an app asked', REFUSED, REFUSED.replace('if !routing.fallback_request {', 'if routing.fallback_request || true {')),
    (K, 'vectors', 'a refusal is said as the agent the command was for', '                        let said = self.as_agent(requested, refusal, "", around);', '                        let said = self.as_agent(&decision.target_session, refusal, "", around);'),
    (K, 'vectors', 'a refused command is given a phrase key', REFUSED, REFUSED.replace('                    action = Some(CLARIFY);\n', '                    action = Some(CLARIFY);\n                    decision.phrase_key = "clarify_target".to_string();\n')),
    (K, 'vectors', 'a command carried out keeps the name as the model said it', '                Ok(Some(session)) => decision.target_session = session,', '                Ok(Some(_)) => {}'),
    # A confirmation.
    (K, 'vectors', "a hold is claimed from another agent's conversation", CLAIM_WAITING, '                && held.expires_at > now_ms'),
    (K, 'vectors', 'a confirmation of what is no longer held is given in its place', '                    if held.is_none() && !decision.pending_id.is_empty() {', '                    if held.is_none() && decision.pending_id.is_empty() && false {'),
    (K, 'vectors', 'a confirmation that names no hold is said to be too late', '                    if held.is_none() && !decision.pending_id.is_empty() {', '                    if held.is_none() {'),
    (K, 'vectors', 'a confirmation too late is held as a question', GONE, GONE.replace('                        action = Some(CLARIFY);\n', '')),
    (K, 'vectors', 'a confirmation too late keeps its kind', GONE, GONE.replace('                        decision.kind = CLARIFY.to_string();\n', '')),
    (K, 'vectors', 'a confirmation too late is not said where an app asked', '                        let said = self.as_agent(requested, HELD_MESSAGE_GONE, "", around);', '                        let said = self.as_agent(requested, HELD_MESSAGE_GONE, "", around).filter(|_| !routing.fallback_request);'),
    (K, 'vectors', 'a confirmation too late is said as the agent it was for', '                        let said = self.as_agent(requested, HELD_MESSAGE_GONE, "", around);', '                        let said = self.as_agent(&agent, HELD_MESSAGE_GONE, "", around);'),
    # A hold, and a renewal.
    (K, 'vectors', "a hold is renewed from another agent's conversation", HOLD_WAITING, HOLD_WAITING.replace('\n                && held.requested_session == requested', '')),
    (K, 'vectors', 'a hold that was given is renewed', HOLD_WAITING, HOLD_WAITING.replace('\n                && held.status == "pending"', '')),
    (K, 'vectors', 'a hold whose time is up is renewed', HOLD_WAITING, HOLD_WAITING.replace('\n                && held.expires_at > now', '')),
    (K, 'vectors', 'a renewal keeps the candidate where none is named', '            held.candidate_session = candidate.to_string();', '            if !candidate.is_empty() {\n                held.candidate_session = candidate.to_string();\n            }'),
    (K, 'vectors', 'a renewal takes the words of the answer', '            held.candidate_session = candidate.to_string();', '            held.candidate_session = candidate.to_string();\n            held.utterance = routing.packet["utterance"].as_str().unwrap_or_default().to_string();'),
    (K, 'vectors', 'a renewal keeps the reason it was asked for', '            held.reason.clone_from(&decision.reason);\n', ''),
    (K, 'vectors', 'a renewal keeps the time it ends', '            held.expires_at = now + HELD_FOR_MS;\n        } else {', '        } else {'),
    (K, 'vectors', 'a renewal moves the time it was held', '            held.expires_at = now + HELD_FOR_MS;\n        } else {', '            held.expires_at = now + HELD_FOR_MS;\n            held.created_at = now;\n        } else {'),
    (K, 'vectors', 'a new hold takes the id the model named', '                pending_id: hold_as.to_string(),', '                pending_id: if decision.pending_id.is_empty() {\n                    hold_as.to_string()\n                } else {\n                    decision.pending_id.clone()\n                },'),
    (P, 'vectors', "the packet shows what was held in another agent's conversation", '            && held.requested_session == shown.requested_session', '            && !held.requested_session.is_empty()'),
    # How an agent is doing.
    (K, 'vectors', 'the name a status is asked of is looked up as it is', '                agents_named(agents, named)\n            };', '                Vec::from_iter(lookup(agents, named))\n            };'),
    (K, 'vectors', 'of two agents of a name, the first is told of', '                [one] => valid(agents, &shown, &one.session),', '                [one, ..] => valid(agents, &shown, &one.session),'),
    (K, 'vectors', 'a status of no name is refused', '            let text = if named.is_empty() || agent.is_some() {', '            let text = if agent.is_some() {'),
    (K, 'vectors', 'an agent out of reach is answered for by the one spoken to', '            let text = if named.is_empty() || agent.is_some() {', '            let text = if named.is_empty() || agent.is_some() || found.len() == 1 {'),
    (K, 'vectors', 'the status told is of the agent spoken to', '                target = agent.unwrap_or(requested).to_string();', '                target = requested.to_string();'),
    (K, 'vectors', "the model's words of a status are not said", '                match decision.status_text.as_str() {', '                match decision.status_text.get(..0).unwrap_or_default() {'),
    (K, 'vectors', 'a status refused is a status told', STATUS_REFUSED, STATUS_REFUSED.replace('                action = Some(CLARIFY);\n', '')),
    (K, 'vectors', 'a status refused is kept for the name as said', STATUS_REFUSED, STATUS_REFUSED.replace('                target = requested.to_string();\n', '')),
    (K, 'vectors', 'a name nobody has is out of reach', '                    0 => NO_SUCH_AGENT,', '                    0 => CANNOT_CHECK,'),
    (K, 'vectors', 'an agent out of reach is nobody', '                    1 => CANNOT_CHECK,', '                    1 => NO_SUCH_AGENT,'),
    (K, 'vectors', 'a name two have is out of reach', '                    _ => WHICH_AGENT,', '                    _ => CANNOT_CHECK,'),
    (K, 'vectors', 'a status refused is said as the agent spoken to', '            effects.push(router_says(text));\n        }\n        // The agent a command acts on', '            effects.extend(self.as_agent(requested, text, "", around).map(Effect::Say));\n        }\n        // The agent a command acts on'),
    # Commands carried out, on the agent named.
    (K, 'vectors', 'what is played again is of the name as said', '                        session: on.clone(),\n                        or:', '                        session: target.clone(),\n                        or:'),
    (K, 'vectors', 'a command for an agent looks up the name as said', '            let agent = lookup(agents, &on);', '            let agent = lookup(agents, &target);'),
    (K, 'vectors', 'the agent opened is told by the name as said', SWITCHED, SWITCHED.replace('session: on.clone(),', 'session: target.clone(),')),
    (K, 'vectors', 'an agent opened is kept for the name as said', SWITCHED, SWITCHED.replace('\n                    target = on;', '')),
    (K, 'vectors', 'an agent stopped is kept for the name as said', STOPPED, STOPPED.replace('\n                    target = on;', '')),
    (K, 'vectors', 'what nobody knows how to do is kept for the name as said', UNKNOWN, UNKNOWN.replace('\n                    target = on;', '')),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
