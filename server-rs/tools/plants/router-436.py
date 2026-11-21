#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The router's holds, commands and names as the Python Host has them since its PR 436 (18 Nov 2026): a command is
never held; a held message belongs to the session it was made in (the packet shows it, a confirmation claims it and
a renewal keeps it, only there); a name given in `target_session` or in the payload is the target, names that
disagree are asked about and a name nobody has makes no agent; names resolve as voice names, case and accents aside,
agents that were put away and janitors counted. And as it has them since its PR 443 (19 Nov 2026, R83 and R84): what
nothing was delivered for answers the session spoken to and names the session its target resolved to, or none; an
app is told nothing aloud; a run's receipt keeps what its first answer said, and a message sent again is answered
that. The kernel's rules (`router/flow.rs`, `router/packet.rs`, the demand history's `janitors/demand.rs`), held by
`tests/router_flow_vectors.rs`, the least receipt's own test and `tests/janitor_demand_vectors.rs`.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'router_flow_vectors'],
    'least': ['test', '-p', 'clarp-kernel', '--lib', 'the_least_receipt_keeps'],
    'demand': ['test', '--test', 'janitor_demand_vectors'],
    'host': ['test', '--test', 'host', '--', 'the_router_shows_twenty_agents_in_detail_and_says_a_start_the_host_refuses'],
}
H = 'clarp-host/src/router.rs'
K = 'clarp-kernel/src/router/flow.rs'
P = 'clarp-kernel/src/router/packet.rs'
D = 'clarp-kernel/src/janitors/demand.rs'
DETAIL = '''        detailed.contains(&agent.session.as_str())
            || others.iter().any(|other| other.agent_id == agent.agent_id)'''
REFUSED_START = '''            Done::Refused {
                code: why.unwrap_or("the agent could not be made").to_string(),
            }'''
SAID_THEN_TOLD = '''                    if let Some(words) = &outcome.say {
                        say(app, words).await;
                    }
                    outcome
                }
            }
        }
    };
    if let Some(told) = &outcome.told {
        app.hub.publish(told.clone(), app.clock.now_ms());
    }'''
TOLD_THEN_SAID = '''                    outcome
                }
            }
        }
    };
    if let Some(told) = &outcome.told {
        app.hub.publish(told.clone(), app.clock.now_ms());
    }
    if let Some(words) = &outcome.say {
        say(app, words).await;
    }'''
RANKED = '''            .filter(|agent| !detailed.contains(&agent.session.as_str()))
            .collect();
        others.sort_by_key(|agent| std::cmp::Reverse(active(agent)));'''
# NOT PLANTED, each with its reason:
#   that a message sent again while its first routing is still out is answered the session asked for: no story
#     sends a message again while its first routing runs (a story is one message at a time, and the Python Host's
#     own test of it needs two threads).
#   that a run whose receipt is refused keeps what its first answer said: in the kernel's book a receipt is refused
#     only where it does not fit the history's bound, and then its least receipt fails on the same field and is the
#     action alone (the story of an agent whose id is too long); the two steps before that are held by the least
#     receipt's own test.
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
#   the keys of a refusal of a status question and of a start the Host refused (`cannot_check`, `command_refused`):
#     neither is kept with a decision, and no phrase is kept ready-spoken on this Host.
#   that an agent that was put away takes no command: no packet shows one, and the command is refused as for an agent
#     of no call (the reference also asks `archived_at`, which it never finds there).
CHECKED = '''    if !sure {
        return (Some(UNSURE_COMMAND), named_session(decision, agents));
    }
    let of_agents = decision.kind == AGENT_CONTROL;
    if of_agents && asked == "fork" {
        // No source of a fork is taken from the router; a real agent it names is kept.
        return (Some(FORK_IN_APP), named_session(decision, agents));
    }
    if of_agents && asked == "create" {
        // A name the voice already gives an agent (one that was put away too) is taken: the
        // Host compares names as they are written only. (A blank name names nobody.)
        let payload = decision.control_payload.as_ref();
        let given = payload.and_then(|payload| payload.get("name"));
        let given = given.and_then(Value::as_str).unwrap_or_default();
        if !agents_named(agents, given).is_empty() {
            return (Some(NAME_TAKEN), String::new());
        }
        return (None, String::new());
    }
    let names = names_given(decision);
    let agent = if names.is_empty() {
        lookup(agents, requested)
    } else {
        let mut found: Vec<&Member> = Vec::new();
        for name in names {
            match agents_named(agents, name).as_slice() {
                [] => return (Some(NO_SUCH_AGENT), String::new()),
                [one] => found.push(one),
                _ => return (Some(WHICH_AGENT), String::new()),
            }
        }
        // The two names given disagree.
        if found
            .iter()
            .any(|agent| agent.agent_id != found[0].agent_id)
        {
            return (Some(WHICH_AGENT), String::new());
        }
        found.first().copied()
    };
    let Some(agent) = agent else {
        return (Some(NO_SUCH_AGENT), String::new());
    };'''
LATE_BAR = '''    let of_agents = decision.kind == AGENT_CONTROL;
    if of_agents && asked == "fork" {
        // No source of a fork is taken from the router; a real agent it names is kept.
        return (Some(FORK_IN_APP), named_session(decision, agents));
    }
    if of_agents && asked == "create" {
        // A name the voice already gives an agent (one that was put away too) is taken: the
        // Host compares names as they are written only. (A blank name names nobody.)
        let payload = decision.control_payload.as_ref();
        let given = payload.and_then(|payload| payload.get("name"));
        let given = given.and_then(Value::as_str).unwrap_or_default();
        if !agents_named(agents, given).is_empty() {
            return (Some(NAME_TAKEN), String::new());
        }
        return (None, String::new());
    }
    let names = names_given(decision);
    let agent = if names.is_empty() {
        lookup(agents, requested)
    } else {
        let mut found: Vec<&Member> = Vec::new();
        for name in names {
            match agents_named(agents, name).as_slice() {
                [] => return (Some(NO_SUCH_AGENT), String::new()),
                [one] => found.push(one),
                _ => return (Some(WHICH_AGENT), String::new()),
            }
        }
        // The two names given disagree.
        if found
            .iter()
            .any(|agent| agent.agent_id != found[0].agent_id)
        {
            return (Some(WHICH_AGENT), String::new());
        }
        found.first().copied()
    };
    if !sure {
        return (Some(UNSURE_COMMAND), named_session(decision, agents));
    }
    let Some(agent) = agent else {
        return (Some(NO_SUCH_AGENT), String::new());
    };'''
NO_AGENT = '''    let Some(agent) = agent else {
        return (Some(NO_SUCH_AGENT), String::new());
    };'''
REFUSED = '''                decision.kind = CLARIFY.to_string();
                decision.phrase_key = refusal.key.to_string();
                action = Some(CLARIFY);
                if aloud {
                    let said = self.as_agent(requested, refusal.text, refusal.key, around);'''
GONE = '''                        action = Some(CLARIFY);
                        decision.kind = CLARIFY.to_string();
                        decision.phrase_key = HELD_MESSAGE_GONE.key.to_string();'''
SAID_GONE = '                        let said = self.as_agent(requested, gone.text, gone.key, around);'
REFUSED_SAID = '''                    around,
                );
            }
            Done::Broke'''
HOLD_WAITING = '''            held.pending_id == decision.pending_id
                && held.status == "pending"
                && held.expires_at > now
                && held.requested_session == requested'''
CLAIM_WAITING = '''                && held.expires_at > now_ms
                && held.requested_session == requested'''
STATUS_REFUSED = '''                action = Some(CLARIFY);
                let refusal'''
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
    (K, 'vectors', 'the payload of a command to the router is a name', '    if decision.kind == AGENT_CONTROL {\n        let payload', '    if true {\n        let payload'),
    (K, 'vectors', "the payload's name is not looked at", '        names.push(given.and_then(Value::as_str).map_or("", python::strip));', '        names.push(given.and_then(Value::as_str).filter(|_| false).map_or("", python::strip));'),
    (K, 'vectors', "the payload's name keeps its blanks", '        names.push(given.and_then(Value::as_str).map_or("", python::strip));', '        names.push(given.and_then(Value::as_str).unwrap_or_default());'),
    (K, 'vectors', "a payload's name that is no text is a name", '        names.push(given.and_then(Value::as_str).map_or("", python::strip));', '        names.push(given.map_or("", |given| given.as_str().map_or("7", python::strip)));'),
    (K, 'vectors', 'a blank name is a name', '    names.retain(|name| !name.is_empty());', '    names.retain(|_| true);'),
    (K, 'vectors', 'naming none, the command is for nobody', '        lookup(agents, requested)\n    } else {', '        None\n    } else {'),
    (K, 'vectors', 'a name nobody has is asked about', '                [] => return (Some(NO_SUCH_AGENT), String::new()),', '                [] => return (Some(WHICH_AGENT), String::new()),'),
    (K, 'vectors', 'a name two agents have is the first of them', '                _ => return (Some(WHICH_AGENT), String::new()),', '                many => found.push(many[0]),'),
    (K, 'vectors', 'names that disagree are carried out', '            .any(|agent| agent.agent_id != found[0].agent_id)', '            .any(|agent| agent.agent_id.is_empty())'),
    (K, 'vectors', 'a command said to no agent and naming none goes on', NO_AGENT, '    let Some(agent) = agent else {\n        return (None, String::new());\n    };'),
    (K, 'vectors', 'a janitor is refused as one of no call', '    if !agent.voice_target {', '    if !agent.voice_target && agent.archived {'),
    (K, 'vectors', 'an agent the packet does not show takes commands', '    if !shown.contains(&agent.session.as_str()) {', '    if shown.is_empty() {'),
    (K, 'vectors', 'a command acts on the name as it was said', '    (None, agent.session.clone())', '    (None, decision.target_session.clone())'),
    # A new agent under a name there is.
    (K, 'vectors', 'a new agent takes a name there is', '        if !agents_named(agents, given).is_empty() {', '        if agents_named(agents, given).len() > 9 {'),
    (K, 'vectors', 'a new agent takes a name there is in other letters', '        if !agents_named(agents, given).is_empty() {', '        if agents.iter().any(|agent| agent.persona == given) {'),
    (K, 'vectors', 'a new agent takes the name of one put away', '        if !agents_named(agents, given).is_empty() {', '        if agents_named(agents, given).iter().any(|agent| !agent.archived) {'),
    (K, 'vectors', 'a taken name is refused as one nobody has', '            return (Some(NAME_TAKEN), String::new());', '            return (Some(NO_SUCH_AGENT), String::new());'),
    (K, 'vectors', 'a taken name is said in other words', 'const NAME_TAKEN: Fixed = fixed("name_taken", "I already have an agent by that name.");', 'const NAME_TAKEN: Fixed = fixed("name_taken", "I have an agent by that name.");'),
    # The keys a refusal is kept with.
    (K, 'vectors', 'the key of no such agent', 'const NO_SUCH_AGENT: Fixed = fixed("no_such_agent",', 'const NO_SUCH_AGENT: Fixed = fixed("no_agent",'),
    (K, 'vectors', 'the key of which agent', 'const WHICH_AGENT: Fixed = fixed("which_agent",', 'const WHICH_AGENT: Fixed = fixed("which",'),
    (K, 'vectors', 'the key of a command from here', '    "cannot_command",\n    "I cannot do', '    "cannot_check",\n    "I cannot do'),
    (K, 'vectors', 'the key of an unsure command', 'const UNSURE_COMMAND: Fixed = fixed("unsure_command",', 'const UNSURE_COMMAND: Fixed = fixed("clarify_target",'),
    (K, 'vectors', 'the key of no voice commands', '    "no_voice_commands",\n    "That agent', '    "no_voice",\n    "That agent'),
    (K, 'vectors', 'the key of a fork in the app', 'const FORK_IN_APP: Fixed = fixed("fork_in_app",', 'const FORK_IN_APP: Fixed = fixed("fork",'),
    (K, 'vectors', 'the key of a taken name', 'const NAME_TAKEN: Fixed = fixed("name_taken",', 'const NAME_TAKEN: Fixed = fixed("name_in_use",'),
    (K, 'vectors', 'the key of a message no longer held', '    "held_message_gone",\n    "That message', '    "message_gone",\n    "That message'),
    # A start the Host refused.
    (K, 'vectors', 'a start the Host refused is an error', '                (error, action) = (code, CLARIFY.to_string());', '                (error, action) = (code, ERROR.to_string());'),
    (K, 'vectors', 'a start the Host refused keeps no word of why', '                (error, action) = (code, CLARIFY.to_string());', '                (error, action) = (String::new() + &code[..0], CLARIFY.to_string());'),
    (K, 'vectors', 'a start the Host refused is not said', REFUSED_SAID, REFUSED_SAID.replace('                );', '                )\n                .filter(|_| false);')),
    (K, 'vectors', 'a start the Host refused is said in other words', 'const COMMAND_REFUSED: Fixed = fixed("command_refused", "I could not do that.");', 'const COMMAND_REFUSED: Fixed = fixed("command_refused", "I cannot do that.");'),
    # A refused command.
    (K, 'vectors', 'a refused command keeps its kind', REFUSED, REFUSED.replace('                decision.kind = CLARIFY.to_string();\n', '')),
    (K, 'vectors', 'a refused command is held while its user is asked', REFUSED, REFUSED.replace('                action = Some(CLARIFY);\n', '')),
    (K, 'vectors', 'a refusal is said where an app asked', REFUSED, REFUSED.replace('if aloud {', 'if aloud || true {')),
    (K, 'vectors', 'a refusal is said as the agent the command was for', '                    let said = self.as_agent(requested, refusal.text, refusal.key, around);', '                    let said = self.as_agent(&decision.target_session, refusal.text, refusal.key, around);'),
    (K, 'vectors', 'a refused command keeps the phrase key of its model', REFUSED, REFUSED.replace('                decision.phrase_key = refusal.key.to_string();\n', '')),
    (K, 'vectors', 'a command carried out keeps the name as the model said it', '            decision.target_session = resolved;\n', '            let _ = resolved;\n'),
    # A confirmation.
    (K, 'vectors', "a hold is claimed from another agent's conversation", CLAIM_WAITING, '                && held.expires_at > now_ms'),
    (K, 'vectors', 'a confirmation of what is no longer held is given in its place', '                    if held.is_none() && !decision.pending_id.is_empty() {', '                    if held.is_none() && decision.pending_id.is_empty() && false {'),
    (K, 'vectors', 'a confirmation that names no hold is said to be too late', '                    if held.is_none() && !decision.pending_id.is_empty() {', '                    if held.is_none() {'),
    (K, 'vectors', 'a confirmation too late is held as a question', GONE, GONE.replace('                        action = Some(CLARIFY);\n', '')),
    (K, 'vectors', 'a confirmation too late keeps its kind', GONE, GONE.replace('                        decision.kind = CLARIFY.to_string();\n', '')),
    (K, 'vectors', 'a confirmation too late keeps the phrase key of its model', GONE, GONE.replace('\n                        decision.phrase_key = HELD_MESSAGE_GONE.key.to_string();', '')),
    (K, 'vectors', 'a confirmation too late is said as the agent it was for', SAID_GONE, SAID_GONE.replace('(requested,', '(&agent,')),
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
    # Everyone in view: the twenty most recently active in detail.
    (P, 'vectors', 'everyone in view is shown in detail', DETAIL, DETAIL.replace('agent.agent_id)', 'agent.agent_id)\n            || view.scope_all')),
    (P, 'vectors', 'twenty-one are shown in detail', '        others.truncate(BROAD_DETAIL_MOST);', '        others.truncate(BROAD_DETAIL_MOST + 1);'),
    (P, 'vectors', 'nineteen are shown in detail', '        others.truncate(BROAD_DETAIL_MOST);', '        others.truncate(BROAD_DETAIL_MOST - 1);'),
    (P, 'vectors', 'the least recently active are shown in detail', '        others.sort_by_key(|agent| std::cmp::Reverse(active(agent)));', '        others.sort_by_key(|agent| active(agent));'),
    (P, 'vectors', 'of the equally recent, the last listed', RANKED, RANKED.replace('            .collect();\n', '            .collect();\n        others.reverse();\n')),
    (P, 'vectors', 'the agent spoken to and the one open count among the twenty', RANKED, RANKED.replace('            .filter(|agent| !detailed.contains(&agent.session.as_str()))\n', '')),
    (P, 'vectors', 'the packet of the first asking shows the twenty too', '    if view.scope_all {\n        others = listed', '    if true {\n        others = listed'),
    # The Host's side: a start it refuses, said before the apps are told; whose last words it reads.
    (H, 'host', 'a start the Host refuses is a failure', REFUSED_START, '            Done::Broke(why.unwrap_or("the agent could not be made").to_string())'),
    (H, 'host', 'a start the Host refuses is not said', '                    if let Some(words) = &outcome.say {', '                    if let Some(words) = outcome.say.as_ref().filter(|_| false) {'),
    (H, 'host', 'a start the Host refuses is said after the apps are told', SAID_THEN_TOLD, TOLD_THEN_SAID),
    (H, 'host', 'with everyone in view the Host reads only the first asking\'s words', '        scope_all: whole,', '        scope_all: false,'),
    # How an agent is doing.
    (K, 'vectors', 'the name a status is asked of is looked up as it is', '                agents_named(agents, named)\n            };', '                Vec::from_iter(lookup(agents, named))\n            };'),
    (K, 'vectors', 'of two agents of a name, the first is told of', '                [one] => valid(agents, &shown, &one.session),', '                [one, ..] => valid(agents, &shown, &one.session),'),
    (K, 'vectors', 'a status of no name is refused', '            let text = if named.is_empty() || agent.is_some() {', '            let text = if agent.is_some() {'),
    (K, 'vectors', 'an agent out of reach is answered for by the one spoken to', '            let text = if named.is_empty() || agent.is_some() {', '            let text = if named.is_empty() || agent.is_some() || found.len() == 1 {'),
    (K, 'vectors', 'the status told is of the agent spoken to', '                target = agent.unwrap_or(requested).to_string();', '                target = requested.to_string();'),
    (K, 'vectors', "the model's words of a status are not said", '                match decision.status_text.as_str() {', '                match decision.status_text.get(..0).unwrap_or_default() {'),
    (K, 'vectors', 'a status refused is a status told', STATUS_REFUSED, STATUS_REFUSED.replace('                action = Some(CLARIFY);\n', '')),
    (K, 'vectors', 'a name nobody has is out of reach', '                    0 => NO_SUCH_AGENT,', '                    0 => CANNOT_CHECK,'),
    (K, 'vectors', 'an agent out of reach is nobody', '                    1 => CANNOT_CHECK,', '                    1 => NO_SUCH_AGENT,'),
    (K, 'vectors', 'a name two have is out of reach', '                    _ => WHICH_AGENT,', '                    _ => CANNOT_CHECK,'),
    (K, 'vectors', 'a status refused is said as the agent spoken to', '            effects.push(router_says(text));\n        }\n        // The agent a command acts on', '            effects.extend(self.as_agent(requested, text, "", around).map(Effect::Say));\n        }\n        // The agent a command acts on'),
    # Commands carried out, on the agent named.
    # What nothing was delivered for, and a message sent again (PR 443).
    (K, 'vectors', 'what a command does is done to the name as it was said', '        let on = decision.target_session.clone();', '        let on = raw.get("target_session").and_then(Value::as_str).unwrap_or_default().to_string();'),
    (K, 'vectors', 'a question answers the session its target named', '        let nothing_done = action == CLARIFY || (command && action != CONTROL);', '        let nothing_done = command && action != CONTROL;'),
    (K, 'vectors', 'a command that failed after it was taken answers the session it named', '        let nothing_done = action == CLARIFY || (command && action != CONTROL);', '        let nothing_done = action == CLARIFY;'),
    (K, 'vectors', "a question keeps its target as the model said it", '            target = resolved(around.agents, &decision.target_session);', '            target.clone_from(&decision.target_session);'),
    (K, 'vectors', "a question's decision keeps the name as the model said it", '            decision.target_session.clone_from(&target);\n        }\n        let message = routing.message();', '        }\n        let message = routing.message();'),
    (K, 'vectors', "a command's row keeps the name as it was said", '            target.clone_from(&resolved);\n', ''),
    (K, 'vectors', 'a name that stands for two agents is resolved to the first', '        [one] if !name.is_empty() => one.session.clone(),', '        [one, ..] if !name.is_empty() => one.session.clone(),'),
    (K, 'vectors', 'a command refused for no voice names nothing', '        return (Some(NO_VOICE_COMMANDS), agent.session.clone());', '        return (Some(NO_VOICE_COMMANDS), String::new());'),
    (K, 'vectors', 'a command refused out of reach names nothing', '        return (Some(CANNOT_COMMAND), agent.session.clone());', '        return (Some(CANNOT_COMMAND), String::new());'),
    (K, 'vectors', 'a command not sure enough names nothing', '        return (Some(UNSURE_COMMAND), named_session(decision, agents));', '        return (Some(UNSURE_COMMAND), String::new());'),
    (K, 'vectors', 'names that disagree name the first', '        [one] => one.clone(),\n        _ => String::new(),\n    }\n}', '        [one, ..] => one.clone(),\n        _ => String::new(),\n    }\n}'),
    (K, 'vectors', 'an agent made is not named in its decision', '                decision.target_session.clone_from(&session);\n', ''),
    (K, 'vectors', 'a start the Host refused stays a command', '                decision.kind = CLARIFY.to_string();\n                decision.spoken_text = refused.text.to_string();', '                decision.spoken_text = refused.text.to_string();'),
    (K, 'vectors', 'a start the Host refused keeps the phrase key of its model', '                decision.phrase_key = refused.key.to_string();\n', ''),
    (K, 'vectors', 'an app is told aloud that a message is no longer held', '                        if aloud {\n                            let said = self.as_agent(requested, gone.text, gone.key, around);', '                        if aloud || true {\n                            let said = self.as_agent(requested, gone.text, gone.key, around);'),
    (K, 'vectors', 'the receipt keeps no answer', '        if demand::fits(&merged(&receipt, &said)) {', '        if demand::fits(&merged(&receipt, &said)) && said.is_null() {'),
    (K, 'vectors', 'the receipt keeps a session that does not fit', '            if demand::fits(&merged(&receipt, &session)) {', '            if !session.is_null() {'),
    (K, 'vectors', 'the receipt names the session answered, not the target resolved', '        let recipient = outcome.target.as_deref().unwrap_or(&outcome.session);', '        let recipient = outcome.session.as_str();'),
    (K, 'vectors', 'a run whose receipt is refused is left claimed', '            let _ = janitors.complete_run(run, ended, Some(&least), why, None, world, now);', '            let _ = least;'),
    (K, 'vectors', 'a message sent again is answered by its recipient though its answer was kept', '        if let Some(status) = said_status.filter(|_| readable) {', '        if let Some(status) = said_status.filter(|_| !readable && readable) {'),
    (K, 'vectors', 'a message sent again is answered any status kept', '    ((200..=599).contains(&status) && ![204, 304].contains(&status)).then_some(status)', '    Some(status)'),
    (K, 'vectors', 'a message sent again is answered 204', '    ((200..=599).contains(&status) && ![204, 304].contains(&status)).then_some(status)', '    ((200..=599).contains(&status) && ![304].contains(&status)).then_some(status)'),
    (K, 'vectors', 'a message sent again is answered a status kept as a fraction', '    let status = status.as_i64()?;', '    let status = status.as_f64().map(|status| status as i64)?;'),
    (K, 'vectors', 'a message sent again is answered an empty session', '                .filter(|session| !session.is_empty())\n', ''),
    (K, 'vectors', 'a message sent again is answered the requested session though an odd one was kept', '        let readable = said_session.as_ref().is_none_or(Option::is_some);', '        let readable = true;'),
    (K, 'vectors', 'a message sent again is answered a tool kept as a number in words', '            outcome.dispatch = dispatched.unwrap_or_default().to_string();', '            outcome.dispatch = dispatched.map_or_else(|| receipt["answer_dispatch"].to_string(), str::to_string);'),
    (K, 'vectors', "an older receipt's status of failure is taken whatever it is", '            let said = receipt.get("http_status").and_then(answerable);', '            let said = receipt.get("http_status").and_then(Value::as_u64).and_then(|status| u16::try_from(status).ok());'),
    (K, 'least', 'the least receipt keeps what a retry needs', '    const KEPT: [&str; 9] = [\n', '    const KEPT: [&str; 10] = [\n        "reason",\n'),
    (K, 'least', 'the least receipt is the action alone at once', '    let mut least = Value::Object(kept.collect());', '    let mut least = Value::Object(kept.filter(|_| false).collect());'),
    (K, 'least', 'the least receipt keeps a session that does not fit', '        && let Some(least) = least.as_object_mut()\n', '        && let Some(least) = least.as_object_mut().filter(|_| false)\n'),
    (D, 'demand', "the history does not take the session a routing run answered", '    "answer_session",\n', '    "answer_sessions",\n'),
    (D, 'demand', "the history does not take the status a routing run answered", '    "answer_status",\n', '    "answer_statuses",\n'),
    (D, 'demand', "the history does not take the tool a routing run answered", '    "answer_dispatch",\n', '    "answer_dispatches",\n'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
