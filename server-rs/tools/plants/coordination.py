#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

Coordination between agents (the Python Host's PR 482, contract 108): the kernel's rules of which turn is for the
agents, who made a send and what its origin is, and the note an agent leaves for its user; the actor's mark in a
turn's acceptance, its notes, their count in the turn's end and the trace a send names; and the Host's edge: what a
send presents, the route and the tool of a note, the notification it makes, and `audience` on every row, the live
turn and the working state.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'coordination_vectors'],
    'notes': ['test', '--test', 'agent_note_vectors'],
    'live': ['test', '--test', 'live_vectors'],
    'actor': ['test', '--test', 'actor', '--', 'a_turn_that_answers_another_agent'],
    'wire': ['test', '-p', 'clarp-host', '--lib', '--', 'wire::'],
    'host': ['test', '--test', 'host', '--', 'coordination::'],
}
KC = 'clarp-kernel/src/coordination.rs'
KJ = 'clarp-kernel/src/journal.rs'
KA = 'clarp-kernel/src/agent.rs'
KL = 'clarp-kernel/src/live.rs'
KV = 'clarp-kernel/src/conversation.rs'
AL = 'clarp-actor/src/lib.rs'
AC = 'clarp-actor/src/conversation.rs'
HW = 'clarp-host/src/wire.rs'
HA = 'clarp-host/src/agent.rs'
HE = 'clarp-host/src/edge.rs'
HC = 'clarp-host/src/coordination.rs'
HT = 'clarp-host/src/admin_tool.rs'
HF = 'clarp-host/src/fleet.rs'
# NOT PLANTED, each with its reason:
#   `free_ms` (the millisecond a notification is filed under past one already taken): this Host files a note's
#     notification at the note's own millisecond and keeps no table to move it by (docs/differences.md); the rule is
#     held by the kernel's own test and by the note vectors, and nothing of the Host calls it.
#   `no_conversation` (a note of an agent without a conversation): a turn that runs has its conversation here, so no
#     request reaches it; its words are held by the note vectors.
#   `synthesize_audio`: the send keeps its own rule of when an answer is spoken (`speak` in `send_as`), which the
#     vectors hold apart; the kernel's function is held by them alone.
#   the rebuild of a live turn's note count when the actor opens (`notes_since`): no Host test stops a Host inside a
#     turn that left notes; the actor's own test holds the count over a restart.
#   the costs at 300,000 rows: a timed test, run by hand (`coordination_costs_the_same_...`), not a rule.
PLANTS = [
    # Which turn is for the agents (the kernel).
    (KC, 'vectors', "the oracle's word is another agent's", '    if USER_WORD_ORIGINS.contains(&origin) {\n        return false;', '    if origin == "user" {\n        return false;'),
    (KC, 'vectors', "the user's word that names a sender is the agent's", '    if USER_WORD_ORIGINS.contains(&origin) {\n        return false;', '    if USER_WORD_ORIGINS.contains(&origin) && sender_agent_id.is_none() {\n        return false;'),
    (KC, 'vectors', 'an agent addressing itself is coordination', '        return sender != agent_id;', '        return true;'),
    (KC, 'vectors', 'a sender of this Host is never coordination', '        return sender != agent_id;', '        return sender == agent_id;'),
    (KC, 'vectors', "an agent's origin with no sender is the user's", '    origin == "agent"\n}', '    false\n}'),
    (KC, 'vectors', 'any origin with no sender is an agent', '    origin == "agent"\n}', '    !origin.is_empty()\n}'),
    (KC, 'vectors', "a sender's padding is part of it", '    let sender = python::strip(sender_agent_id.unwrap_or(""));', '    let sender = sender_agent_id.unwrap_or("");'),
    (KC, 'vectors', "an origin's padding is part of it", '    let origin = python::strip(origin.unwrap_or(""));', '    let origin = origin.unwrap_or("");'),
    (KC, 'vectors', 'a note is for the agents in their turn', '    if kind.unwrap_or("") == NOTE_KIND {\n        return USER;', '    if kind.unwrap_or("") == "notice" {\n        return USER;'),
    (KC, 'vectors', 'a marked turn is for the user', '    if turn_audience == Some(AGENTS) {\n        AGENTS', '    if turn_audience == Some("mixed") {\n        AGENTS'),
    (KC, 'vectors', 'Codex cannot leave a note here', 'const NOTE_BACKENDS: [&str; 6] = ["claude", "agy", "grok", "opencode", "deepseek", "codex"];', 'const NOTE_BACKENDS: [&str; 5] = ["claude", "agy", "grok", "opencode", "deepseek"];'),
    (KC, 'vectors', 'Muse can leave a note', 'const NOTE_BACKENDS: [&str; 6] = ["claude", "agy", "grok", "opencode", "deepseek", "codex"];', 'const NOTE_BACKENDS: [&str; 7] = ["claude", "agy", "grok", "opencode", "deepseek", "codex", "muse"];'),
    (KC, 'vectors', 'a Host without its key lets an agent leave a note', '    !agent_id.is_empty() && key_readable && NOTE_BACKENDS', '    !agent_id.is_empty() && NOTE_BACKENDS'),
    (KC, 'vectors', 'an agent with no id can leave a note', '    !agent_id.is_empty() && key_readable && NOTE_BACKENDS', '    key_readable && NOTE_BACKENDS'),
    (KC, 'vectors', 'Antigravity is not known by its long name', '        "antigravity" => "agy",', '        "antigravity" => "antigravity",'),
    (KC, 'vectors', 'a backend is known in capitals', '    let word = python::strip(raw.unwrap_or("")).to_lowercase();', '    let word = python::strip(raw.unwrap_or("")).to_string();'),
    (KC, 'vectors', 'an unknown backend is none', '        .unwrap_or(DEFAULT_BACKEND)', '        .unwrap_or("unknown")'),
    # Who made a send, and its origin (the kernel).
    (KC, 'vectors', "a turn of another agent may name another sender", '        && caller_agent_id != sender_agent_id\n        && let Some(own) = caller_session', '        && caller_agent_id == sender_agent_id\n        && let Some(own) = caller_session'),
    (KC, 'vectors', "a gone agent's turn may not name a sender", '        && caller_agent_id != sender_agent_id\n        && let Some(own) = caller_session\n    {\n        let own = if own.is_empty() { caller_agent_id } else { own };', '        && caller_agent_id != sender_agent_id\n    {\n        let own = caller_session.unwrap_or(caller_agent_id);'),
    (KC, 'vectors', 'a refused turn is named by its agent id', '        let own = if own.is_empty() { caller_agent_id } else { own };', '        let own = caller_agent_id;'),
    (KC, 'vectors', 'an agent waking itself is from another turn', '        made.self_prompt = caller_agent_id == target_agent_id;', '        made.self_prompt = false;'),
    (KC, 'vectors', "a turn's agent is not the sender it did not name", '            if made.sender_agent_id.is_empty() && caller_session.is_some() {\n                made.sender_agent_id = caller_agent_id.to_string();', '            if made.sender_agent_id.is_empty() && caller_session.is_some() {\n                made.sender_agent_id = String::new();'),
    (KC, 'vectors', "a gone agent's turn is the sender", '            if made.sender_agent_id.is_empty() && caller_session.is_some() {', '            if made.sender_agent_id.is_empty() {'),
    (KC, 'vectors', "a turn's agent replaces the sender it named", '            if made.sender_agent_id.is_empty() && caller_session.is_some() {', '            if caller_session.is_some() {'),
    (KC, 'vectors', 'an origin no client may name is kept', '    let origin = if CLIENT_SETTABLE_ORIGINS.contains(&claimed.as_str()) {', '    let origin = if !claimed.is_empty() {'),
    (KC, 'vectors', 'an origin is told in capitals', '    let claimed = python::strip(claimed.unwrap_or("")).to_lowercase();', '    let claimed = python::strip(claimed.unwrap_or("")).to_string();'),
    (KC, 'vectors', 'no origin with a sender is the user', '    } else if principal.sender_agent_id.is_empty() {\n        "user".to_string()\n    } else {\n        "agent".to_string()\n    };', '    } else {\n        "user".to_string()\n    };'),
    (KC, 'vectors', "an agent's turn speaks the user's word", '    if user_word && (principal.own_turn || peer) {', '    if user_word && peer {'),
    (KC, 'vectors', "a peer speaks the user's word", '    if user_word && (principal.own_turn || peer) {', '    if user_word && principal.own_turn {'),
    (KC, 'vectors', "an agent waking itself speaks the user's word", '    } else if user_word && principal.self_prompt {\n        "automation".to_string()', '    } else if user_word && principal.self_prompt {\n        origin'),
    (KC, 'vectors', "the oracle's word is no user's word", 'pub const USER_WORD_ORIGINS: [&str; 2] = ["oracle", "user"];', 'pub const USER_WORD_ORIGINS: [&str; 2] = ["agent", "user"];'),
    (KC, 'vectors', 'a client may name a watcher no more', '    "watcher",\n    "heartbeat",', '    "heartbeat",'),
    # The note: what a request asks, and its refusals (the kernel).
    (KC, 'notes', 'a body that is no object is read', '    let Value::Object(fields) = body else {\n        return Err(refused(400, "bad json"));\n    };', '    let Value::Object(fields) = body.clone() else {\n        return Ok(Asked { session: String::new(), text: String::new(), note_id: None });\n    };'),
    (KC, 'notes', 'no words are asked of before the session', '    if session.is_empty() {\n        return Err(refused(400, "session required"));\n    }\n    if text.is_empty() {', '    if text.is_empty() && !session.is_empty() {'),
    (KC, 'notes', 'a note without words is taken', '    if text.is_empty() {\n        return Err(refused(\n            400,', '    if text.is_empty() && session.is_empty() {\n        return Err(refused(\n            400,'),
    (KC, 'notes', 'a note of the most signs is too long', '    if text.chars().count() > MAX_TEXT {', '    if text.chars().count() >= MAX_TEXT {'),
    (KC, 'notes', 'a note a sign too long is taken', '    if text.chars().count() > MAX_TEXT {', '    if text.chars().count() > MAX_TEXT + 1 {'),
    (KC, 'notes', 'a note is measured in bytes', '    if text.chars().count() > MAX_TEXT {', '    if text.len() > MAX_TEXT {'),
    (KC, 'notes', "a note's words keep their padding", '    let text = python::strip(&said(fields.get("text"))).to_string();', '    let text = said(fields.get("text"));'),
    (KC, 'notes', "a note's id keeps its capitals", '    let note_id = python::strip(&said(fields.get("note_id"))).to_lowercase();', '    let note_id = python::strip(&said(fields.get("note_id"))).to_string();'),
    (KC, 'notes', 'an id of the most signs is refused', '    } else if note_id.chars().count() <= MAX_NOTE_ID', '    } else if note_id.chars().count() < MAX_NOTE_ID'),
    (KC, 'notes', 'an id a sign too long is taken', '    } else if note_id.chars().count() <= MAX_NOTE_ID', '    } else if note_id.chars().count() <= MAX_NOTE_ID + 1'),
    (KC, 'notes', 'an id may have a dot', "            .all(|sign| matches!(sign, 'a'..='z' | '0'..='9' | '-' | '_'))", "            .all(|sign| matches!(sign, 'a'..='z' | '0'..='9' | '-' | '_' | '.'))"),
    (KC, 'notes', 'an id may not have an underscore', "            .all(|sign| matches!(sign, 'a'..='z' | '0'..='9' | '-' | '_'))", "            .all(|sign| matches!(sign, 'a'..='z' | '0'..='9' | '-'))"),
    (KC, 'notes', 'a request without a turn may leave a note', '        Caller::Own => Ok(()),\n        Caller::Nobody => Err(', '        Caller::Own | Caller::Nobody => Ok(()),\n        Caller::Other(None) if false => Err('),
    (KC, 'notes', "another agent's turn is refused in the words of a gone one", '        Caller::Other(Some(own)) => Err(refused(\n            403,\n            format!("this turn belongs to {own}, not to {session}; run {COMMAND} --from {own}"),', '        Caller::Other(Some(_)) => Err(refused(\n            403,\n            format!(\n                "this turn belongs to an agent that no longer exists, not to {session}; \\\n                 {COMMAND} works only from its own turn"\n            ),'),
    (KC, 'notes', 'a turn that does not run is refused as a conflict of words', 'format!("{COMMAND} works only inside a running turn of {session}; this call is not in one"),', 'format!("note_id is already a note of {session} with other words"),'),
    (KC, 'notes', 'a note asked again with the same words is refused', '        Some(held) if held == text => Ok(true),', '        Some(held) if held == text => Err(other_words(session, note_id)),'),
    (KC, 'notes', 'a note asked again with other words is the same note', '        Some(_) => Err(other_words(session, note_id)),', '        Some(_) => Ok(true),'),
    (KC, 'notes', "a note's row is named without its agent", '    format!("note-{agent_id}-{note_id}")', '    format!("note-{note_id}")'),
    (KC, 'notes', "a note's notification is of its row without the mark", '    let mark = Sha256::digest(format!("note\\0{message_id}").as_bytes());', '    let mark = Sha256::digest(message_id.as_bytes());'),
    (KC, 'notes', "a note's notification id is longer", '    format!("pn-{}", &mark[..20])\n}\n\n/// The millisecond', '    format!("pn-{}", &mark[..24])\n}\n\n/// The millisecond'),
    (KC, 'notes', "a quiet agent's note pages its user", '    if agent.quiet || (agent.present && !agent.can_chat) {', '    if agent.present && !agent.can_chat {'),
    (KC, 'notes', "a muted agent's note is pushed", '        push: !agent.muted,', '        push: true,'),
    (KC, 'notes', "a muted agent's note is told as any note", '    let reason = if agent.muted {', '    let reason = if false {'),
    # The mark, the note and the trace in the log (the kernel's journal and the actor).
    (KJ, 'actor', 'a marked acceptance is not keyed', '        item.key = Some(coordination_key(turn.0));', '        item.key = None;'),
    (KJ, 'actor', 'every acceptance is keyed as marked', '            coordination: Some(_),\n            ..', '            ..'),
    (KJ, 'actor', 'a note is no row of the conversation', '    pub const ROWS: Kinds = Kinds::range(MESSAGE, AGENT_NOTE);', '    pub const ROWS: Kinds = Kinds::range(MESSAGE, AGENT_NOTE - 1);'),
    (KA, 'actor', 'the mark is not kept in the acceptance', '                    *mark = coordination;', '                    *mark = None;'),
    (AL, 'actor', 'a trace another message has is taken', '            && owner != trace_owner_of(message)\n        {\n            return Ok(Err(TraceTaken));', '            && owner != trace_owner_of(message)\n            && false\n        {\n            return Ok(Err(TraceTaken));'),
    (AL, 'actor', 'a trace is looked at though the Host named it', '        if marks.trace_named\n            && let Some(owner)', '        if let Some(owner)'),
    (AL, 'actor', 'the first message of a trace does not own it', '            with.push(NewItem::new(kind::TRACE, now_ms, owner).keyed(trace_key(trace)));', '            drop(owner);'),
    (AL, 'actor', 'a sender is no part of the message a trace is of', '            .map(|sender| sender.agent_id.clone())\n            .unwrap_or_default(),\n        origin: message.origin.name().to_string(),', '            .map(|_| String::new())\n            .unwrap_or_default(),\n        origin: message.origin.name().to_string(),'),
    (AL, 'actor', 'the mark is not handed to the acceptance', '                coordination: marks.coordination.clone(),', '                coordination: None,'),
    (AL, 'actor', 'a note is left from a turn that does not run', '        let Some(trace) = self.running_trace(turn)? else {\n            return Ok((Noted::NotRunning, Outputs::default()));\n        };', '        let trace = self.running_trace(turn)?.unwrap_or_default();'),
    (AL, 'actor', 'a note asked again with the same words is other words', '                Some(Entry::Note(note)) if note.text == text => Noted::Again {', '                Some(Entry::Note(note)) if note.text != text => Noted::Again {'),
    (AL, 'actor', "a note's trace is not the turn's", '            trace_id: Some(trace.clone()).filter(|trace| !trace.is_empty()),', '            trace_id: None,'),
    (AL, 'actor', 'a note is not counted', '        self.turn_notes = (turn, notes_of(self.turn_notes, TurnId(turn)) + 1);', '        self.turn_notes = (turn, notes_of(self.turn_notes, TurnId(turn)));'),
    (AL, 'actor', "a turn's end does not carry its notes", '    end.note_count = facts.notes;', '    end.note_count = 0;'),
    (AL, 'actor', "another turn's notes are counted", '    if kept.0 == turn.0 { kept.1 } else { 0 }', '    kept.1'),
    (AL, 'actor', 'a note tells nobody the conversation changed', '            changed: Changed {\n                conversation: true,\n                ..Changed::default()\n            },\n            ..Outputs::default()\n        };\n        Ok((\n            Noted::Written {', '            ..Outputs::default()\n        };\n        Ok((\n            Noted::Written {'),
    (AC, 'actor', "a row does not read its turn's mark", '            row.coordination = self.mark(turn)?;', '            row.coordination = None;'),
    (AC, 'actor', "a turn's mark is read from another turn", '    let Some(stored) = log.find(&coordination_key(turn))? else {', '    let Some(stored) = log.find(&coordination_key(turn + 1))? else {'),
    (KV, 'actor', 'a note is no row of the turn', '            Self::Note(note) => Some(note.turn),', '            Self::Note(_) => None,'),
    # The live turn (the kernel, as the actor shows it).
    (KL, 'live', 'a live turn says not whom it is for', '"tool_count": 0, "audience": audience,', '"tool_count": 0,'),
    (KL, 'live', 'a live turn is always for the user', '"tool_count": 0, "audience": audience,', '"tool_count": 0, "audience": "user",'),
    (AL, 'host', 'a live turn of a marked turn is for the user', '        Ok(match self.mark(turn.0)? {\n            Some(_) => clarp_kernel::coordination::AGENTS,', '        Ok(match self.mark(turn.0)? {\n            Some(_) => clarp_kernel::coordination::USER,'),
    (AL, 'host', 'a live turn of an agent that cannot leave a note is for the agents', '        if !self.profile.hides_coordination {\n            return Ok(clarp_kernel::coordination::USER);\n        }', ''),
    # The rows, the working state and the notification (the Host's wire).
    (HW, 'host', 'a row of a marked turn is for the user', '    let marked = version.coordination.as_ref().map(|_| coordination::AGENTS);', '    let marked = version.coordination.as_ref().map(|_| coordination::USER);'),
    (HW, 'host', 'a row says not whom it is for', '    row["audience"] = json!(coordination::row_audience(row["kind"].as_str(), marked));', '    let _ = marked;'),
    (HW, 'host', "an agent that cannot leave a note has its rows hidden", '            if row.is_object() && (!hides || row.get("audience").is_none()) {', '            if row.is_object() && row.get("audience").is_none() {'),
    (HW, 'host', 'every row of an agent that can leave a note is the user\'s', '            if row.is_object() && (!hides || row.get("audience").is_none()) {', '            if row.is_object() {'),
    (HW, 'host', 'a working state for the agents is for the user', '        told["audience"] = json!(coordination::AGENTS);', '        told["audience"] = json!(coordination::USER);'),
    (HW, 'host', 'a working state names not the agent it works with', '            told["sender_agent_id"] = json!(agent_id);', '            let _ = agent_id;'),
    (HW, 'wire', "a working state names the agent by its id", '            told["sender_name"] = json!(name);', '            told["sender_name"] = json!(agent_id);'),
    (HW, 'host', "a turn's summary does not count its notes", '    if end.note_count > 0 {', '    if end.note_count > 99 {'),
    (HW, 'host', "a note's notification is of the moment the Host started", '            "done_ts": done_ms,\n            "source_message_id": message_id,', '            "done_ts": 0,\n            "source_message_id": message_id,'),
    (HW, 'host', "a note's notification names no row", '            "source_message_id": message_id,\n            "cause_message_id": "",', '            "source_message_id": "",\n            "cause_message_id": "",'),
    (HW, 'host', "a note's notification is a reply's", '            "origin": coordination::NOTE_ORIGIN,', '            "origin": "agent",'),
    (HW, 'host', "a note's notification shows nothing of it", '            "preview": visible_preview(text),', '            "preview": "",'),
    (HW, 'host', "a note is told as an agent's that is not muted", '    match coordination::note_decision(&capabilities(record)) {', '    match coordination::note_decision(&notify::Capabilities::default()) {'),
    # The Host's agent thread.
    (HA, 'host', 'the marks of a send are not handed to the actor', '                    .send_marked(&message, admission, reservation, &marks, now)', '                    .send_marked(&message, admission, reservation, &clarp_actor::Marks::default(), now)'),
    (HA, 'host', "a note's notification is not published", '                    self.setup.hub.publish(told.clone(), now);', '                    let _ = told;'),
    (HA, 'host', "a note is not pushed", '                    if told["push"] == true {\n                        self.setup.pusher.turn_done(TurnDone {', '                    if told["push"] == false {\n                        self.setup.pusher.turn_done(TurnDone {'),
    (HA, 'host', 'a note asked again tells the user again', '                        clarp_actor::Noted::Written { id, .. } => {', '                        clarp_actor::Noted::Written { id, .. } | clarp_actor::Noted::Again { id, .. } => {'),
    (HA, 'host', 'a working state of a marked turn is for the user', '    let Ok(Some(sender)) = actor.mark(live.turn.0) else {', '    let Ok(Some(sender)) = actor.mark(live.turn.0 + 1) else {'),
    (HA, 'host', "a working state of a peer's agent names an agent", '    if sender.is_empty() {\n        return wire::WorkingWith::Agents { sender: None };', '    if sender.is_empty() && false {\n        return wire::WorkingWith::Agents { sender: None };'),
    (HA, 'host', 'a working state is hidden for an agent that cannot leave a note', '    if !actor.profile().hides_coordination {\n        return wire::WorkingWith::User;\n    }', ''),
    (HF, 'host', 'an agent hides its turns without the key', '            works.turns.has_key(),', '            true,'),
    (HA, 'host', 'an agent sent to hides its turns without the key', '                self.setup.turns.has_key(),', '                true,'),
    (HE, 'host', 'a page hides the rows of an agent without the key', '        app.turns.has_key(),\n    );\n    let mut answer = wire::log_response', '        true,\n    );\n    let mut answer = wire::log_response'),
    # What a send presents (the Host's edge).
    (HE, 'host', "a send's turn token is not read", '    crate::coordination::made(app, turn_of(headers), peer, trace_named).await', '    crate::coordination::made(app, None, peer, trace_named).await'),
    (HE, 'host', 'a trace the Host named is looked at', '    let trace_named = named.is_some_and(|trace| !trace.is_empty());', '    let trace_named = true;'),
    (HE, 'host', 'a trace the caller named is not looked at', '    let trace_named = named.is_some_and(|trace| !trace.is_empty());', '    let trace_named = false;'),
    (HE, 'host', "a turn's agent may name another as its sender", '    .map_err(crate::coordination::refused)?;\n    let sender = sender(app, request.sender.as_ref()).or_else(|| {', '    .unwrap_or_default();\n    let sender = sender(app, request.sender.as_ref()).or_else(|| {'),
    (HE, 'host', "a send from a turn has not that turn's agent as sender", '            .then(|| sender_by_id(app, &principal.sender_agent_id))', '            .then(|| None)'),
    (HE, 'host', 'the origin a send claimed is kept whoever made it', '    let named = coordination::origin(request.origin.as_deref(), &principal, made.peer);', '    let named = coordination::origin(request.origin.as_deref(), &coordination::Principal::default(), false);'),
    (HE, 'host', "a send is marked by the Host's own origin", '        Some(&named),\n        Some(&principal.sender_agent_id),', '        Some("agent"),\n        Some(&principal.sender_agent_id),'),
    (HE, 'host', "a sender the Host does not know is another agent's", '        &sender_known(app, request.sender.as_ref()),', '        sender_named(request.sender.as_ref()).unwrap_or_default(),'),
    (HE, 'host', 'a trace taken is answered as a send', '        Err(clarp_actor::TraceTaken) => {\n            drop(guard);\n            return Err(ApiError::new(\n                StatusCode::CONFLICT,', '        Err(clarp_actor::TraceTaken) => {\n            drop(guard);\n            return Err(ApiError::new(\n                StatusCode::BAD_REQUEST,'),
    (HE, 'host', 'there is no route of a note', '        .route("/agent-notes", post(crate::coordination::agent_note))\n', ''),
    # The note at the edge, and its tool.
    (HC, 'host', 'a peer is anyone who sends the header', '        .is_some_and(|peer| !peer.as_bytes().is_empty())', '        .is_some()'),
    (HE, 'host', 'a peer is nobody', '    let peer = crate::coordination::from_a_peer(headers);', '    let peer = false;'),
    (HC, 'host', 'a token that names nobody is a turn of the agent', '        (None, _) => Caller::Nobody,', '        (None, _) => Caller::Own,'),
    (HC, 'host', "another agent's token is the agent's own", '        .filter(|(agent_id, _)| *agent_id != record.agent_id)', '        .filter(|_| false)'),
    (HC, 'host', "a gone agent's token is refused as a live one's", '        (Some(_), Some(session)) => Caller::Other(session.as_deref()),', '        (Some(_), Some(_)) => Caller::Other(Some("nobody")),'),
    (HC, 'host', 'a key that cannot be read is no token', '        .map_err(|why| ApiError::retryable(StatusCode::SERVICE_UNAVAILABLE, why.0))', '        .or(Ok(None))'),
    (HC, 'host', 'a note is refused before its body is read', '    let caller = token_of(app, token).await?;\n    let asked = rules::asked(body).map_err(refused)?;\n    let member = app', '    let asked = rules::asked(body).map_err(refused)?;\n    let caller = token_of(app, token).await?;\n    let member = app'),
    (HC, 'host', 'a note asked again is told anew', '        (Noted::Again { trace_id, .. }, _) => (trace_id, false, false, rules::AGAIN.to_string()),', '        (Noted::Again { trace_id, .. }, _) => (trace_id, true, true, "agent-note".to_string()),'),
    (HC, 'host', 'a note is answered as not told', '            (trace_id, true, true, reason)', '            (trace_id, true, false, reason)'),
    (HC, 'host', 'a note of a turn that does not run is a conflict of words', '        (Noted::NotRunning, _) => return Err(refused(rules::not_running(&record.session))),', '        (Noted::NotRunning, _) => return Err(refused(rules::other_words(&record.session, &note_id))),'),
    (HT, 'host', "the tool's note is left without its turn", '    match crate::coordination::leave_note(app, &body, token).await {', '    match crate::coordination::leave_note(app, &body, None).await {'),
    (HT, 'host', "the tool's refusal is a failure", '        Err(refused) if refused.status().is_client_error() => ToolAnswer::said(\n            4,\n            format!(\n                "clarp-admin tell-user: the Host refused it', '        Err(refused) if refused.status().is_client_error() => ToolAnswer::said(\n            1,\n            format!(\n                "clarp-admin tell-user: the Host refused it'),
    (HT, 'host', 'there is no tool of a note', '        Some((command, rest)) if command == "tell-user" => tell_user(app, call, rest).await,\n', ''),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
