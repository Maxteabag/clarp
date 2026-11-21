#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The Host's part of a call of the Oracle: the socket and its claims, the task of a call, the tools of the fleet as
the Host does them, the memory as the Host keeps it, the router as the Host asks it.
"""
import runner

RUNS = {
    'call': ['test', '--test', 'host', '--', 'a_call_of_the_oracle_is_carried'],
    'direct': ['test', '--test', 'host', '--', 'a_call_in_which_everything'],
    'refused': ['test', '--test', 'host', '--', 'a_call_of_the_oracle_that_cannot_be'],
    'ring': ['test', '--test', 'host', '--', 'a_ring_is_held'],
}
# NOT PLANTED, each with its reason:
#   that a call whose thread is another call's now ends when it keeps its talk (`Ok(Err(threads::STALE)) => return
#     false`; taken off after its first run, MISSED): a later call of the same phone ends the older as replaced
#     before that one keeps anything again, so only a race reaches the branch.
#   that a talk that could not be kept is said on the Host's stderr (`eprintln!` in `keeps`): no test reads the
#     Host's stderr; what is said and that it is said once are the kernel's (`threads::not_kept`), planted in the
#     list oracle-memory.
#   that the store's own failure ends no call (`Err(_) => threads::STORE_FAILED` in `keeps`): no stand-in of the
#     tests makes the store fail; the kernel's rule for it is planted in oracle-memory, and a talk too long to keep,
#     which goes the same way at the Host, is planted here.
#   that a stop is handed the call's work as the book has it (`supersede(session, &rows, now)`; taken off after its
#     first run, MISSED): the work a stop silences beyond what waits to be told is a result that is over and not yet
#     seen, which at this Host lasts less than one tick (250 ms) and no test can place a stop into; the kernel's
#     story "what a stop silenced is not told, and what landed after the stop's read is told once" holds it.
#   where the router is asked (router.rs, `"" => ROUTER_URL`) and where the voice is reached (calls/live.rs): a
#     wrong rule there would make a test reach a provider outside this machine, with a key that is none. No test
#     reaches an engine outside, and no planted rule may make one.
#   the route's own refusal of a phone with limited rights (`caller(&principal)`, 401): the door refuses such a
#     phone before the route is reached (403), on this Host as on the Python Host, whose `serve` has the same second
#     guard.
#   that a contact which names no agent any more is let go of when a call is opened (`if let_go`): whoever asks for
#     the contact afterwards lets it go too, so no request shows which of the two did.
#   that a claim which is let go of counts as ending from that moment, before the voice is free (the mark in
#     `Claimed::drop`), and that its entry is taken away: the time between the two is the start of a task, which no
#     test can ask within; and a stale entry changes only how soon a phone whose voice a ring has is refused.
#   that a phone whose voice another kind of call has is refused at once and not after the wait (`None => return
#     None` in `claim`), and how long an older call is waited for (three seconds): the answer is the same, later.
#   that the store of the Host cannot be read or written (`Err(_) => UNAVAILABLE`, the `else` after `begun.await`):
#     no test can take the store away from a Host that runs.
#   that the thread of a call is another call's while it runs (`if !session.keeps().await`, the `has` in `admits`, a
#     finish that is refused): the thread of a phone is taken only by a call of the same phone, which ends the older
#     call first. The rules themselves are the kernel's, held by its vectors.
#   that an action is ended with what its tool answered, when it answers (`memory.finish`; taken off the list after
#     its first run, where it was MISSED): where that is not written, the next call of the thread ends the action
#     by the work it handed on, which is the rule for a receipt that was lost, and nothing reads it between the two.
#   that an action whose receipt was lost is ended by the work it handed on when the next call begins
#     (`memory.reconcile`): it takes a Host that dies between a tool's answer and its writing down, which no test
#     can time. The rule is the kernel's, held there.
#   that the end of a call is kept with its thread (`machine.ends`), and that a call that was taken over keeps
#     nothing more: what it changes is whether a call within thirty seconds goes on as the one before, and every
#     call of the tests is within thirty seconds of the one before anyway.
#   how long the voice is given to say that it took the end, and how long a phone may say nothing (two and thirty
#     seconds): nothing but time depends on them.
#   that a contact which is gone after a call began fails the routing before anything is written down
#     (`contact_there` true where it is not): the tool refuses the same request with the same notice.
#   the two short cuts that save a look at the book and at the memory where there is nothing to look for (`rows`,
#     `admits`): what is answered is the same.
#   the bytes of JSON on the two sockets and to the router (`dumps_ascii`): other bytes that say the same are the
#     same to whoever reads JSON.
#   which of an agent's messages a tool reads (`Which::WithoutAutomated`): no agent of these tests has a message the
#     Host began itself; that such messages are apart is the conversation's own rule, held by its tests.
#   the kinds of row the memory is read from, one by one: one loop reads the four, and the plant below takes all but
#     the threads away.
C = 'clarp-host/src/oracle/call.rs'
T = 'clarp-host/src/oracle/tools.rs'
M = 'clarp-host/src/oracle/memory.rs'
R = 'clarp-host/src/oracle/router.rs'
O = 'clarp-host/src/oracle.rs'
LOST = ('                        if voice.machine().is_some_and(|s| !s.began) {app.oracle.calls.health(false);}')
LOST_NOTICE = '                        line.tell(Told::Text(python::dumps_ascii(&voice_lost()))).await;'
PLANTS = [
    # ---- one call a phone
    (C, 'ring', 'a phone has as many voices as it asks for', '            if app.calls.claim_voice(app, principal).await {', '            if app.calls.claim_voice(app, principal).await || true {'),
    (C, 'call', 'a call that is ending is not waited for', '                Some(held) if held.closing => {}', '                Some(held) if held.closing => return None,'),
    (C, 'call', 'a call a phone still has is not told to stop', '                    held.stop.as_ref()?.send(()).ok();', '                    held.stop.as_ref()?;'),
    (C, 'call', 'a later call does not wait for the older to end', '            tokio::time::timeout_at(deadline, freed).await.ok()?;', '            return None;'),
    (C, 'call', 'a call that runs cannot be told to stop', '            held.stop = Some(stop);', '            drop(stop);'),
    (C, 'call', 'a call that is open is not known to be', '                self.open().insert(principal.to_string(), Open::default());\n', ''),
    (C, 'call', 'a phone that said it ends its call is told to stop when it calls again', '                        if matches!(phoned, Phoned::Close { .. }) {\n                            app.oracle.calls.closing(&call.owner);\n                        }', '                        drop(phoned);'),
    (C, 'call', 'the voice of a phone is never free again', '            app.calls.release_voice(&principal).await;\n', ''),
    (C, 'call', 'nobody is told that a voice is free', '            app.oracle.calls.freed.notify_waiters();\n', ''),
    # ---- what a phone asks a call with
    (C, 'call', 'a call that names no strategy goes to the contact', '            [] => Strategy::Operator,', '            [] => Strategy::DirectContact,'),
    (C, 'call', 'a strategy named twice is the first', '            _ => return Err("Choose one Oracle delegation strategy".to_string()),', '            [one, ..] => Strategy::named(Some(one)).ok_or("Unsupported Oracle delegation strategy")?,'),
    (C, 'refused', 'the contact a phone names is not heard', '                Strategy::Operator => rules::contact_of_a_call(kept, &json!(wanted), &agents),', '                Strategy::Operator => rules::contact_of_a_call(kept, &json!(""), &agents),'),
    (C, 'direct', "the Host's own contact is not a call's", '            let kept = directory.setting(CONTACT_KEY).unwrap_or("");', '            let kept = "";'),
    (C, 'call', 'a call that goes to the contact takes its contact as any call does', '                    let (contact, let_go) =\n                        rules::contact_of_a_direct_call(kept, &json!(wanted), &agents);\n                    (contact?, let_go)', '                    rules::contact_of_a_call(kept, &json!(wanted), &agents)'),
    (C, 'call', 'a podcast detour is taken as a call', '        if !named(query, "podcast_artifact").is_empty() {', '        if false {'),
    (C, 'call', 'a thread may be named twice', '            if named(query, name).len() > 1 {\n                return Err("Use one Oracle context selection".to_string());', '            if named(query, name).len() > 2 {\n                return Err("Use one Oracle context selection".to_string());'),
    (C, 'call', 'a call may be asked to begin anew in any words', '            Some(_) => return Err("new_conversation must be 0 or 1".to_string()),', '            Some(_) => false,'),
    (C, 'call', 'a call that is to begin anew is not heard to be', '            Some(&"1") => true,', '            Some(&"1") => false,'),
    (C, 'call', 'a handoff may be named twice', '            if named(query, name).len() > 1 {\n                return Err(format!("Use one {name}"));', '            if named(query, name).len() > 2 {\n                return Err(format!("Use one {name}"));'),
    (C, 'call', 'a call may ask for any kind of handoff', '            .is_some_and(|how| *how != "hands_free")', '            .is_some_and(|_| false)'),
    (C, 'call', 'a call may come back from a handoff of any name', '        if !handoff_id.is_empty() && !phone::handoff_id(handoff_id) {', '        if false {'),
    (C, 'call', 'a thread that is asked for by its name is not looked for', '            thread_id: named(query, "thread_id")\n                .first()\n                .map(|id| (*id).to_string()),', '            thread_id: None,'),
    # ---- the socket
    (C, 'call', 'a request that asks for no socket is taken as a call', '    if !socket.asked() {', '    if false {'),
    (C, 'refused', 'a Host without a key reaches for the voice', '    let Some(key) = app.ears.key("openai") else {\n        let words = "Oracle v2 needs an OpenAI key on this Host";\n        return line::refused(StatusCode::SERVICE_UNAVAILABLE, words);\n    };', '    let key = app.ears.key("openai").unwrap_or_default();'),
    (C, 'call', "what a call is asked with wrongly is the Host's fault", '        Err(why) => return line::refused(StatusCode::BAD_REQUEST, &why),', '        Err(why) => return line::refused(StatusCode::BAD_GATEWAY, &why),'),
    (C, 'ring', 'a phone that has a voice is refused as one who asked wrongly', '        return line::refused(StatusCode::CONFLICT, "An Oracle session is already active");', '        return line::refused(StatusCode::BAD_REQUEST, "An Oracle session is already active");'),
    (C, 'refused', "a voice that cannot be reached is the asker's mistake", '        (StatusCode::BAD_GATEWAY, "Oracle v2 upstream unavailable");', '        (StatusCode::BAD_REQUEST, "Oracle v2 upstream unavailable");'),
    (C, 'call', "a thread that cannot be had is the Host's fault", '        Ok(Err(why)) => return Err((StatusCode::BAD_REQUEST, why)),', '        Ok(Err(why)) => return Err((StatusCode::BAD_GATEWAY, why)),'),
    (C, 'call', 'every call goes on in the thread of no contact', '            contact: &asked.contact,\n            thread_id: asked.thread_id.as_deref(),', '            contact: "",\n            thread_id: asked.thread_id.as_deref(),'),
    (C, 'call', 'a call that is to begin anew goes on in the old thread', '            fresh: asked.fresh,', '            fresh: false,'),
    (C, 'refused', 'a call is opened though its voice cannot be reached... as unavailable for no reason', '    let voice = Upstream::open(app, key).await.ok_or(UNAVAILABLE)?;', '    let voice = Upstream::open(app, key).await.ok_or((StatusCode::CONFLICT, "Oracle v2 upstream unavailable"))?;'),
    # ---- how a call begins
    (C, 'call', 'a call begins as if nothing was kept of its thread', '        let saved = memory.load(opened).unwrap_or(Value::Null);', '        let saved = Value::Null;'),
    (C, 'call', 'the work of a thread is not the work of its next call', '            work: &work,\n            contexts: &[],\n            told: &told,', '            work: &[],\n            contexts: &[],\n            told: &told,'),
    (C, 'call', 'what a voice was told of is told again in the next call', '            told: &told,', '            told: &[],'),
    (C, 'call', 'the history of a call has nothing of its talk', '            state: &saved,', '            state: &Value::Null,'),
    (C, 'call', 'the history of a call has nothing of its work', '            work: &work,\n            contexts: &[],\n            roster: &roster,', '            work: &[],\n            contexts: &[],\n            roster: &roster,'),
    (C, 'call', 'the history of a call names no agents', '            contexts: &[],\n            roster: &roster,', '            contexts: &[],\n            roster: &Value::Null,'),
    (C, 'call', "the times of a call are told in the world's own time", '    let zone = threads::zone_named(&zone_of_this_machine());', '    let zone = threads::zone_named("");'),
    (C, 'call', 'a zone that is named with a colon is none', "    let told = told.trim_start_matches(':');\n", ''),
    (C, 'call', 'the zone a machine was told is not its zone', '    if !told.is_empty() {\n        return told.to_string();\n    }\n', ''),
    (C, 'call', 'the owner of a call is nobody', '            principal: opened.owner,', '            principal: "",'),
    (C, 'direct', 'a call is of the usual strategy whatever was asked', '            strategy: call.asked.strategy,', '            strategy: Strategy::Operator,'),
    (C, 'direct', 'a call does not know its contact', '            contact: &call.asked.contact,\n            direct_prefix', '            contact: "",\n            direct_prefix'),
    (C, 'direct', 'cues are played whatever the owner wrote', '    if no_cues {', '    if false {'),
    (C, 'call', 'cues are played only where the owner wrote that they are not', '    let no_cues = of_oracle("earcons").and_then(toml::Value::as_bool) == Some(false);', '    let no_cues = of_oracle("earcons").and_then(toml::Value::as_bool) != Some(false);'),
    (C, 'direct', 'the languages an owner wrote are not read', '    let languages = voice::languages(&codes.unwrap_or(Value::Null));', '    let languages = voice::languages(&Value::Null);'),
    (C, 'direct', 'a phone is not told that no router is asked', '    if direct {\n        // A phone that asked for it', '    if false {\n        // A phone that asked for it'),
    (C, 'call', 'the voice is told of no agents', '        roster: Some(&roster),', '        roster: None,'),
    (C, 'direct', 'a call that goes to the contact is begun as any other', '        roster: Some(&roster),\n        direct,', '        roster: Some(&roster),\n        direct: false,'),
    (C, 'call', 'a call begins without what was kept of the talk', '        history: history.as_array().map_or(&[][..], Vec::as_slice),', '        history: &[],'),
    (C, 'direct', 'the languages an owner wrote are not told', '        languages: &languages,', '        languages: &[],'),
    # ---- the task of a call
    (C, 'call', 'a voice that is lost is lost without a word to the phone', LOST + '\n' + LOST_NOTICE, LOST),
    (C, 'call', 'a voice that ends its call is said to be lost', '                    Some(Ok(Message::Close(_))) if voice.machine().is_none() => break Ending::VoiceGone,', '                    Some(Ok(Message::Close(_))) if voice.machine().is_none() => {\n                        line.tell(Told::Text(python::dumps_ascii(&voice_lost()))).await;\n                        break Ending::VoiceGone;\n                    }'),
    (C, 'call', 'a call goes on after its voice said that it is over', '        if session.machine.closed() {\n            // The voice said that the call is over.\n            break Ending::VoiceGone;\n        }\n', ''),
    (C, 'call', 'two things are routed at once', '        if session.route.is_none()\n            && let Some(ident) = session.machine.next_wanted()', '        if let Some(ident) = session.machine.next_wanted()'),
    (C, 'call', 'bytes of a phone that are no text end its call', '                    Ok(Some(Heard::Bytes(_))) => {}', '                    Ok(Some(Heard::Bytes(_))) => break Ending::Left,'),
    (C, 'call', 'a phone that goes away leaves its voice without a word', '            let _ = voice.send(json!({"type":"session.close"})).await;\n', ''),
    (C, 'call', 'a call that was taken over says goodbye to its voice', '    if matches!(ending, Ending::Left) {', '    if matches!(ending, Ending::Left | Ending::Replaced) {'),
    # ---- a routing
    (C, 'refused', 'the router is asked while the user still speaks', '                if now.ms < self.machine.quiet_at() {', '                if false {'),
    (C, 'direct', 'a contact is never there', '                    let there = tool_rules::resolve(&agents, &json!(contact)).is_ok();', '                    let there = false;'),
    (C, 'call', 'the router is asked the question bare', '                    let answer = router::asked(&app, &key, routing::request(&question)).await;', '                    let answer = router::asked(&app, &key, question.clone()).await;'),
    (C, 'refused', 'a request whose file was not written is handed on', '            Came::Written(Some(path)) => self.routed(Fed::Written(&path)).await,\n            Came::Answer(None) | Came::Written(None) => self.routed(Fed::Failed).await,', '            Came::Written(path) => {\n                let path = path.unwrap_or_default();\n                self.routed(Fed::Written(&path)).await;\n            }\n            Came::Answer(None) => self.routed(Fed::Failed).await,'),
    (C, 'refused', 'a router that does not answer is waited for without end', '            Came::Answer(None) | Came::Written(None) => self.routed(Fed::Failed).await,', '            Came::Answer(None) | Came::Written(None) => {}'),
    (C, 'call', 'what an agent was told to leave is still told', '                if let Some(session) = &done.left {\n                    let rows = self.rows().await;\n                    let now = self.now();\n                    self.machine.supersede(session, &rows, now);\n                }\n', ''),
    (C, 'refused', 'a tool that refused is taken to have answered', '                let (Some(place), Some(receipt)) = (place, done.receipt) else {\n                    return self.routed(Fed::Failed).await;\n                };', '                let Some(place) = place else {\n                    return self.routed(Fed::Failed).await;\n                };\n                let receipt = done.receipt.unwrap_or_else(|| json!({}));'),
    (C, 'call', 'nobody is known to own work', '                        found.map(|row| row.owner_principal.clone())', '                        found.and_then(|_| None::<String>)'),
    (C, 'call', 'an action is done for the folder of the data itself', '                let folder = app.config.data_dir.join("media").join(HANDOFF_FILES);', '                let folder = app.config.data_dir.join(HANDOFF_FILES);'),
    (C, 'direct', 'a tool is done for the administrator whoever called', '                let who = (self.call.owner.clone(), self.call.asked.contact.clone());', '                let who = (self.call.owner.clone(), String::new());'),
    # ---- what waits for the user
    (C, 'call', 'what waits for the user is never read', '            let decisions = attention::pending(&listed, attention::PENDING_MOST);', '            let decisions = attention::pending(&listed, 0);'),
    (C, 'call', 'questions that wait for the user are not read', '            let listed = library::inbox(self.app, true, false);', '            let listed = library::inbox(self.app, false, false);'),
    (C, 'call', 'what a voice was told of is not written down', '            let _ = told.await;', '            drop(told);'),
    # ---- the file of where a request came from
    (C, 'call', 'the folder of the files is open to all', '    made.recursive(true).mode(0o700).create(folder).ok()?;', '    made.recursive(true).mode(0o755).create(folder).ok()?;'),
    (C, 'call', 'a file of where a request came from is open to all', '        .mode(0o600)', '        .mode(0o644)'),
    (C, 'call', 'a file of where a request came from is written in ASCII', '    open.write_all(python::dumps(provenance).as_bytes()).ok()?;', '    open.write_all(python::dumps_ascii(provenance).as_bytes()).ok()?;'),
    (C, 'refused', 'a talk too long to keep ends the call', '            Ok(Err(why)) => why,', '            Ok(Err(_)) => return false,'),
    # ---- the router as the Host asks it
    (R, 'call', 'the router is asked without a key', '            .header("Authorization", &bearer)\n', ''),
    (R, 'refused', 'a router that answers an error is taken at its word', '        if !(200..300).contains(&response.status().as_u16()) {\n            return None;\n        }\n', ''),
    # ---- the memory as the Host keeps it
    (M, 'call', 'the memory is not written', '        app.fleet.keep(kept.kind, kept.id, row).await?;', '        drop(row);'),
    (M, 'call', 'the memory is read of its threads alone', '        for kind in [THREADS, BEGUN, LINKED, TOLD] {', '        for kind in [THREADS] {'),
    # ---- the tools of the fleet as the Host does them
    (T, 'call', 'a look at an agent sees nothing of what it said', '            let rows = said(&member, rules::STATUS_MESSAGES).await?;', '            let rows = said(&member, 0).await?;'),
    (T, 'call', 'what an agent said is read without its newest', '            let rows = said(&member, 2 * rules::MESSAGES_READ).await?;', '            let rows = said(&member, 0).await?;'),
    (T, 'call', "a reading of an agent's own words reads one message more than was asked", '            Some(rules::transcript(&known(&member), &rows, limit, now_ms))', '            Some(rules::transcript(&known(&member), &rows, limit + 1, now_ms))'),
    (T, 'call', 'who a stop reached is not said', '            *left = Some(session.to_string());\n', ''),
    (T, 'call', 'a stop that hands other work on leaves the agent held', '            member.agent.resume("oracle".to_string()).await.ok()?;\n', ''),
    (T, 'call', 'a stop calls off the work of the administrator', '            delegations::called_off(app, principal, session)\n', '            delegations::called_off(app, "administrator", session)\n'),
    (T, 'call', 'work is handed on for the administrator whoever asked', '    let taken = delegations::handed(app, owner, work).await.ok()?;', '    let taken = delegations::handed(app, "administrator", work).await.ok()?;'),
    (T, 'call', 'the roster of a call has its janitors... none of its agents', '    roster.map(|member| known(member)).collect()', '    roster.take(0).map(|member| known(member)).collect()'),
    # ---- what calls a Host says it can hold
    (O, 'refused', 'a Host without a key says that a call can be held', '    let key = app.ears.key("openai").is_some();\n', '    let key = true;\n'),
    (O, 'call', 'a Host with a key says that no call can be held', '    let key = app.ears.key("openai").is_some();\n', '    let key = false;\n'),
    (O, 'call', 'the first kind of call is said to be held where the key is', '        "available": false,\n        "model": FIRST_KIND.0,', '        "available": key,\n        "model": FIRST_KIND.0,'),
    (O, 'call', 'WebRTC is claimed', '        "webrtc": false,\n        "v2": {', '        "webrtc": true,\n        "v2": {'),
    (O, 'call', 'detours into a podcast are claimed', '            "podcast": false,\n            "podcast_history": false,\n            "voice_provider": choice["provider"],', '            "podcast": true,\n            "podcast_history": false,\n            "voice_provider": choice["provider"],'),
    (O, 'call', 'the other engine is said to hold calls where the key is', '            "direct_contact_available": false,\n            "available": false,', '            "direct_contact_available": false,\n            "available": key,'),
    (O, 'refused', 'a call to the contact alone is said to be had without a key', '            "direct_contact_available": key,', '            "direct_contact_available": true,'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
