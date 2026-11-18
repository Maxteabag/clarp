#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The tools of the fleet as the Oracle's router uses them: what each thing a router wants means for a Host, what it is
answered, and what an agent said read within its bounds. The kernel's rules.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'oracle_tools_vectors'],
}
# NOT PLANTED, each with its reason:
#   a number of messages too large for a machine to count (`unwrap_or(TRANSCRIPT_MOST)`): every number a router can
#     write fits, and one that is too large is cut to the most all the same.
K = 'clarp-kernel/src/oracle/tools.rs'
NAMED = '        name == python::casefold(agent.session) || name == python::casefold(agent.persona)'
OF_TALK = '        |row: &&Message| (row.role == "user" || row.role == "assistant") && !row.text.is_empty();'
PLANTS = [
    # ---- the agents work can be handed to
    (K, 'vectors', 'an agent that was put away can be handed work', '        .filter(|agent| !agent.archived && !agent.janitor)', '        .filter(|agent| !agent.janitor)'),
    (K, 'vectors', 'a janitor can be handed work', '        .filter(|agent| !agent.archived && !agent.janitor)', '        .filter(|agent| !agent.archived)'),
    (K, 'vectors', 'an agent is meant by its session alone', NAMED, '        name == python::casefold(agent.session)'),
    (K, 'vectors', 'an agent is meant by what it is called alone', NAMED, '        name == python::casefold(agent.persona)'),
    (K, 'vectors', 'the case of a name matters', '    let name = python::casefold(python::strip(&said(name)));', '    let name = python::strip(&said(name)).to_string();'),
    (K, 'vectors', 'a name is made small as Rust makes letters small', '    let name = python::casefold(python::strip(&said(name)));', '    let name = python::strip(&said(name)).to_lowercase();'),
    (K, 'vectors', 'the blanks around a name are part of it', '    let name = python::casefold(python::strip(&said(name)));', '    let name = python::casefold(&said(name));'),
    (K, 'vectors', 'of two agents of a name the first is meant', '        (Some(agent), None) => Ok(agent),', '        (Some(agent), _) => Ok(agent),'),
    # ---- the agents there are
    (K, 'vectors', 'a roster names an agent by its session', '                "name": agent.persona,\n                "session": agent.session,\n                "backend": agent.backend,', '                "name": agent.session,\n                "session": agent.session,\n                "backend": agent.backend,'),
    (K, 'vectors', 'a roster does not say what tool an agent runs', '                "backend": agent.backend,\n', ''),
    (K, 'vectors', 'a personality is told a sign longer', '                "personality": python::first(agent.personality, PERSONALITY_SIGNS),', '                "personality": python::first(agent.personality, PERSONALITY_SIGNS + 1),'),
    (K, 'vectors', 'a contact that is none is told as an empty name', '    let contact = Some(contact).filter(|contact| !contact.is_empty());', '    let contact = Some(contact);'),
    (K, 'vectors', 'a roster comes without its note', '    json!({ "agents": shown, "oracle_contact": contact, "note": ROSTER_NOTE })', '    json!({ "agents": shown, "oracle_contact": contact })'),
    # ---- a request
    (K, 'vectors', 'a request is handed on with the blanks around it', '    let asked = python::strip(&asked);\n    if asked.chars().count() > REQUEST_SIGNS {', '    let asked = asked.as_str();\n    if asked.chars().count() > REQUEST_SIGNS {'),
    (K, 'vectors', 'a request may be as long as it likes', '    if asked.chars().count() > REQUEST_SIGNS {\n        return Err(NO_REQUEST);\n    }\n', ''),
    (K, 'vectors', 'a request of 16000 signs is too long', '    if asked.chars().count() > REQUEST_SIGNS {', '    if asked.chars().count() >= REQUEST_SIGNS {'),
    (K, 'vectors', 'a request is measured in bytes', '    if asked.chars().count() > REQUEST_SIGNS {', '    if asked.len() > REQUEST_SIGNS {'),
    # ---- what a tool means
    (K, 'vectors', 'the agents there are cannot be asked for', '    if name == "list_agents" {\n        return Ok(Plan::Answer(list_agents(agents, contact)));\n    }\n', ''),
    (K, 'vectors', 'work for the contact goes to the agent that is named', '    let agent = if name == "investigate_with_oracle" {', '    let agent = if false {'),
    (K, 'vectors', 'work for a contact that is none is work for nobody', '        if contact.is_empty() {\n            return Ok(Plan::Answer(json!({ "error": NO_CONTACT })));\n        }\n', ''),
    (K, 'vectors', 'a look at an agent is a reading of it', '        "get_agent_status" => Ok(Plan::Status(agent)),', '        "get_agent_status" => Ok(Plan::Messages(agent)),'),
    (K, 'vectors', 'a reading of an agent is a look at it', '        "read_agent_messages" => Ok(Plan::Messages(agent)),', '        "read_agent_messages" => Ok(Plan::Status(agent)),'),
    (K, 'vectors', 'no message at all may be asked for', '            let wanted = arguments["limit"].as_i64().filter(|limit| *limit >= 1);', '            let wanted = arguments["limit"].as_i64().filter(|limit| *limit >= 0);'),
    (K, 'vectors', 'one message alone may not be asked for', '            let wanted = arguments["limit"].as_i64().filter(|limit| *limit >= 1);', '            let wanted = arguments["limit"].as_i64().filter(|limit| *limit > 1);'),
    (K, 'vectors', 'the usual number of messages is the most', '            let wanted = wanted.map_or(TRANSCRIPT_USUAL, |limit| {', '            let wanted = wanted.map_or(TRANSCRIPT_MOST, |limit| {'),
    (K, 'vectors', 'as many messages may be asked for as one likes', '            Ok(Plan::Transcript(agent, wanted.min(TRANSCRIPT_MOST)))', '            Ok(Plan::Transcript(agent, wanted))'),
    (K, 'vectors', 'a stop always hands other work on', '                follow: Some(follow)\n                    .filter(|follow| !follow.is_empty())\n                    .map(|follow| (named(), follow)),', '                follow: Some(follow).map(|follow| (named(), follow)),'),
    (K, 'vectors', 'a stop never hands other work on', '                    .filter(|follow| !follow.is_empty())\n', '                    .filter(|_| false)\n'),
    (K, 'vectors', 'the work in the place of what was stopped is named as any work', '            let named = || operation_id(principal, call_id, true);', '            let named = || operation_id(principal, call_id, false);'),
    (K, 'vectors', 'work handed on is named as work in the place of other work', '                operation_id: operation_id(principal, call_id, false),', '                operation_id: operation_id(principal, call_id, true),'),
    (K, 'vectors', 'work that says nothing is handed on', '            if request.is_empty() {\n                return Err(NO_REQUEST);\n            }\n', ''),
    (K, 'vectors', 'work for an agent cannot be handed on', '        "delegate_to_agent" | "investigate_with_oracle" => {', '        "investigate_with_oracle" => {'),
    (K, 'vectors', 'work for the contact cannot be handed on', '        "delegate_to_agent" | "investigate_with_oracle" => {', '        "delegate_to_agent" => {'),
    # ---- what a tool answers
    (K, 'vectors', 'work that was handed on is told without its name', '    json!({ "status": status, "operation_id": operation_id, "session": session, "note": HANDED })', '    json!({ "status": status, "session": session, "note": HANDED })'),
    (K, 'vectors', 'a stop is told as a stop that did not happen', '    json!({ "cancelled": true, "note": STOPPED })', '    json!({ "cancelled": false, "note": STOPPED })'),
    # ---- how long ago
    (K, 'vectors', 'a moment that is not known was just now', '    let Some(then_ms) = then_ms else {\n        return String::new();\n    };', '    let then_ms = then_ms.unwrap_or(now_ms);'),
    (K, 'vectors', 'what is to come was long ago', '    let seconds = (now_ms - then_ms).max(0) / 1000;', '    let seconds = (now_ms - then_ms).abs() / 1000;'),
    (K, 'vectors', 'a minute is not a minute yet', '        if seconds >= size {\n            let count = seconds / size;\n            let many = if count == 1 { "" } else { "s" };\n            return format!("{count} {unit}{many} ago");\n        }\n    }\n    "just now".to_string()', '        if seconds > size {\n            let count = seconds / size;\n            let many = if count == 1 { "" } else { "s" };\n            return format!("{count} {unit}{many} ago");\n        }\n    }\n    "just now".to_string()'),
    (K, 'vectors', 'one hour is hours', '            let many = if count == 1 { "" } else { "s" };\n            return format!("{count} {unit}{many} ago");\n        }\n    }\n    "just now".to_string()', '            let many = "s";\n            return format!("{count} {unit}{many} ago");\n        }\n    }\n    "just now".to_string()'),
    (K, 'vectors', 'less than a minute ago is said in other words', '    "just now".to_string()', '    "less than a minute ago".to_string()'),
    # ---- what an agent is doing
    (K, 'vectors', 'an agent of no state is of no state', '    let kind = if kind.is_empty() { "idle" } else { kind };', '    let kind = kind;'),
    (K, 'vectors', 'an agent that thinks is not at work', '    let busy = matches!(kind, "thinking" | "tool" | "spawned");', '    let busy = matches!(kind, "tool" | "spawned");'),
    (K, 'vectors', 'an agent that uses a tool is not at work', '    let busy = matches!(kind, "thinking" | "tool" | "spawned");', '    let busy = matches!(kind, "thinking" | "spawned");'),
    (K, 'vectors', 'an agent that was started is not at work', '    let busy = matches!(kind, "thinking" | "tool" | "spawned");', '    let busy = matches!(kind, "thinking" | "tool");'),
    (K, 'vectors', 'what an agent is about is its tool before its summary', '        let about = ["summary", "tool", "message"].map(|name| &state.detail[name]);', '        let about = ["tool", "summary", "message"].map(|name| &state.detail[name]);'),
    (K, 'vectors', 'what an agent is about is never its message', '        let about = ["summary", "tool", "message"].map(|name| &state.detail[name]);', '        let about = ["summary", "tool", "tool"].map(|name| &state.detail[name]);'),
    (K, 'vectors', 'what an agent is about is the first thing written, though it says nothing', '            .find(|about| python::truthy(about))', '            .find(|about| !about.is_null())'),
    (K, 'vectors', 'what an agent last said is what its user last said', '        .find(|row| row.role == "assistant" && !python::strip(row.text).is_empty());', '        .find(|row| !python::strip(row.text).is_empty());'),
    (K, 'vectors', 'what an agent last said may be blank', '        .find(|row| row.role == "assistant" && !python::strip(row.text).is_empty());', '        .find(|row| row.role == "assistant");'),
    (K, 'vectors', 'what an agent last said is what it said first', '    let said_last = rows\n        .iter()\n        .rev()\n', '    let said_last = rows\n        .iter()\n'),
    (K, 'vectors', 'an agent at work is told by its state', '        "state": if busy { "working" } else { kind },', '        "state": kind,'),
    (K, 'vectors', 'what an agent does is told a sign longer', '        "current_step": python::first(&step.unwrap_or_default(), STEP_SIGNS),', '        "current_step": python::first(&step.unwrap_or_default(), STEP_SIGNS + 1),'),
    (K, 'vectors', 'what an agent last said is told a sign longer', '        "last_said": said_last.map_or("", |row| python::first(row.text, LAST_SAID_SIGNS)),', '        "last_said": said_last.map_or("", |row| python::first(row.text, LAST_SAID_SIGNS + 1)),'),
    (K, 'vectors', 'since when an agent does what it does is since it last spoke', '        "since": age_text(latest.and_then(|state| state.at_ms), now_ms),', '        "since": age_text(said_last.map(|row| row.at_ms), now_ms),'),
    (K, 'vectors', 'how long ago an agent spoke is not told', '        "last_said_age": age_text(said_last.map(|row| row.at_ms), now_ms),', '        "last_said_age": "",'),
    # ---- what an agent said, within its bounds
    (K, 'vectors', "what the Host's routine said is part of the talk", OF_TALK, '        |row: &&Message| !row.text.is_empty();'),
    (K, 'vectors', 'what the user said is no part of the talk', OF_TALK, '        |row: &&Message| row.role == "assistant" && !row.text.is_empty();'),
    (K, 'vectors', 'a message that says nothing is part of the talk', OF_TALK, '        |row: &&Message| (row.role == "user" || row.role == "assistant");'),
    (K, 'vectors', 'the oldest messages are read', '    let selected = &talk[talk.len().saturating_sub(limit)..];', '    let selected = &talk[..limit.min(talk.len())];'),
    (K, 'vectors', 'more is never said to be there', '    let mut cut = talk.len() > selected.len();', '    let mut cut = false;'),
    (K, 'vectors', 'a reading that runs out of room does not say so', '        if room == 0 {\n            cut = true;\n            break;\n        }', '        if room == 0 {\n            break;\n        }'),
    (K, 'vectors', 'a reading goes on without room', '        if room == 0 {\n            cut = true;\n            break;\n        }\n', ''),
    (K, 'vectors', 'a message is read whole whatever room is left', '        let text = python::first(row.text, message_signs.min(room));', '        let text = python::first(row.text, message_signs);'),
    (K, 'vectors', 'a message is read as long as there is room', '        let text = python::first(row.text, message_signs.min(room));', '        let text = python::first(row.text, room);'),
    (K, 'vectors', 'what was read takes no room', '        room -= text.chars().count();\n', ''),
    (K, 'vectors', 'a message is read without when it was said', '        message.insert("timestamp".to_string(), json!(row.timestamp));\n', ''),
    (K, 'vectors', 'a message that was cut never says so', '        if mark_cuts && text.len() < row.text.len() {', '        if false && text.len() < row.text.len() {'),
    (K, 'vectors', 'a message that was cut always says so', '        if mark_cuts && text.len() < row.text.len() {', '        if text.len() < row.text.len() {'),
    (K, 'vectors', 'a whole message says that it was cut', '        if mark_cuts && text.len() < row.text.len() {', '        if mark_cuts {'),
    (K, 'vectors', 'the messages are told the newest first', '    read.reverse();\n', ''),
    (K, 'vectors', 'the age of the newest message is the age of the oldest', '    let newest = stamps.max().unwrap_or("");', '    let newest = stamps.min().unwrap_or("");'),
    (K, 'vectors', 'a sentence about what an agent said reads as many messages as its own words', '    let bounds = (MESSAGES_READ, MESSAGE_SIGNS, MESSAGES_SIGNS);', '    let bounds = (TRANSCRIPT_MOST, MESSAGE_SIGNS, MESSAGES_SIGNS);'),
    (K, 'vectors', 'a sentence about what an agent said reads messages as long as its own words', '    let bounds = (MESSAGES_READ, MESSAGE_SIGNS, MESSAGES_SIGNS);', '    let bounds = (MESSAGES_READ, TRANSCRIPT_MESSAGE_SIGNS, MESSAGES_SIGNS);'),
    (K, 'vectors', 'a sentence about what an agent said has the room of its own words', '    let bounds = (MESSAGES_READ, MESSAGE_SIGNS, MESSAGES_SIGNS);', '    let bounds = (MESSAGES_READ, MESSAGE_SIGNS, TRANSCRIPT_SIGNS);'),
    (K, 'vectors', 'a sentence about what an agent said marks what was cut', '    let (read, _) = recent(rows, bounds, false);', '    let (read, _) = recent(rows, bounds, true);'),
    (K, 'vectors', "an agent's own words are cut as for a sentence about them", '    let bounds = (limit, TRANSCRIPT_MESSAGE_SIGNS, TRANSCRIPT_SIGNS);', '    let bounds = (limit, MESSAGE_SIGNS, TRANSCRIPT_SIGNS);'),
    (K, 'vectors', "an agent's own words have the room of a sentence about them", '    let bounds = (limit, TRANSCRIPT_MESSAGE_SIGNS, TRANSCRIPT_SIGNS);', '    let bounds = (limit, TRANSCRIPT_MESSAGE_SIGNS, MESSAGES_SIGNS);'),
    (K, 'vectors', "an agent's own words are read without their cuts marked", '    let (read, cut) = recent(rows, bounds, true);', '    let (read, cut) = recent(rows, bounds, false);'),
    (K, 'vectors', "an agent's own words are never said to be an excerpt", '        "truncated": cut,', '        "truncated": false,'),
    (K, 'vectors', "an agent's own words come without its session", '        "agent": agent.persona,\n        "session": agent.session,\n        "messages": read,', '        "agent": agent.persona,\n        "messages": read,'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
