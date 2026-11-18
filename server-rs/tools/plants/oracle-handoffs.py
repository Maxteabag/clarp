#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

A call of the Oracle put through to an agent, and back: the kernel's record of a handoff, its steps and its
deadlines, and what a call and the apps are told of it; and the Host's part: the desk it keeps, the calls it tells,
the deadlines it runs, the doors, and the agents' own tool.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'oracle_handoff_vectors'],
    'host': ['test', '--test', 'host', '--', 'a_call_of_the_oracle_is_put_through'],
}
K = 'clarp-kernel/src/oracle/handoffs.rs'
H = 'clarp-host/src/oracle/handoffs.rs'
C = 'clarp-host/src/oracle/call.rs'
T = 'clarp-host/src/admin_tool.rs'
# NOT PLANTED, each with its reason:
#   that the time a phone is shown it has left is never less than nothing (`.max(0)`): no step of a story is taken
#     after a deadline that was not acted on, so none is shown a time that has run out.
#   that a phone's book with nothing in it is let go of again where a request only looked (`tidy`) is planted; that
#     it is let go of only where it never had a handoff (`generation == 0`) is not: a book that had one has records.
#   the order of two deadlines of two phones at the same moment: no story has two at one moment.
#   of the Host: that whoever asked for a handoff is let go after every step's time and a little more (a test would
#     wait fifty-five seconds for it); that the time of a step runs anew when the Host starts in the middle of one
#     (the kernel's rule, held by its vectors: the Host only calls it); that the way to a call's task is let go of
#     when the call is over (what is sent down it afterwards reaches nobody either way); and the route's own refusal
#     of a phone with fewer rights (the door keeps such a phone out before the route is reached).
#   three conditions the Python Host has and nothing can reach, which this kernel therefore has not (each was planted
#     once, and missed): that only a handoff to an agent counts as the one the phone is with an agent by (a way back
#     is never active: made, it is over); that a handoff to an agent ends no other when it fails (it names none);
#     and that a way back which a new call supersedes ends the talk it was the way back of (that talk stands before
#     it in the book, and where the phone came back by itself its own turn has ended it already).
WAITS = '        "offered" | "preparing" => Some(10_000),\n        "activating" => Some(30_000),'
OVER = 'const OVER: [&str; 4] = ["failed", "broken", "cancelled", "ended"];'
LATE = 'const LATE: [(&str, &str); 2] = [\n    ("failed", "prepare_timeout"),\n    ("broken", "activation_timeout"),\n];'
WHOSE_OUT = '            call.voice_session_id == record.oracle.voice_session_id\n        } else {\n            call.handoff_id == record.handoff_id\n        };'
PLANTS = [
    # ---- how long a step may take
    (K, 'vectors', 'an offer waits nine seconds', WAITS, '        "offered" => Some(9_000),\n        "preparing" => Some(10_000),\n        "activating" => Some(30_000),'),
    (K, 'vectors', 'a phone that prepares is given half a minute', WAITS, '        "offered" => Some(10_000),\n        "preparing" | "activating" => Some(30_000),'),
    (K, 'vectors', 'a phone that activates is given ten seconds', WAITS, '        "offered" | "preparing" | "activating" => Some(10_000),'),
    (K, 'vectors', 'a talk with an agent has a time too', WAITS, '        "offered" | "preparing" => Some(10_000),\n        "activating" | "active" => Some(30_000),'),
    # ---- what is over, and what is kept of it
    (K, 'vectors', 'a handoff that ended is not over', OVER, 'const OVER: [&str; 3] = ["failed", "broken", "cancelled"];'),
    (K, 'vectors', 'a handoff that was called off is not over', OVER, 'const OVER: [&str; 3] = ["failed", "broken", "ended"];'),
    (K, 'vectors', 'a handoff that broke is not over', OVER, 'const OVER: [&str; 3] = ["failed", "cancelled", "ended"];'),
    (K, 'vectors', 'a handoff that failed is not over', OVER, 'const OVER: [&str; 3] = ["broken", "cancelled", "ended"];'),
    (K, 'vectors', 'a handoff the phone did not get on with is let go of like any that is over', LATE, 'const LATE: [(&str, &str); 1] = [("broken", "activation_timeout")];'),
    (K, 'vectors', 'a handoff that broke for its time is let go of like any that is over', LATE, 'const LATE: [(&str, &str); 1] = [("failed", "prepare_timeout")];'),
    (K, 'vectors', 'nineteen of what is over are kept', 'pub const KEPT: usize = 20;', 'pub const KEPT: usize = 19;'),
    (K, 'vectors', 'what a kept way back was of is let go of like any that is over', '        keep.extend(parents);\n', ''),
    (K, 'vectors', 'the oldest of what is over are kept, and not the newest', '        keep.extend_from_slice(&history[newest..]);', '        keep.extend_from_slice(&history[..history.len() - newest]);'),
    (K, 'vectors', 'what a talk depends on is let go of when it is old', '        let pinned = |record: &&Record| record.pinned();', '        let pinned = |_record: &&Record| false;'),
    (K, 'vectors', 'the phone is with an agent by the oldest handoff that is active', '        self.records.iter().rev().find(with_an_agent)', '        self.records.iter().find(with_an_agent)'),
    # ---- what the call of the Oracle is
    (K, 'vectors', "any call of the phone is the call a handoff left", '        let Some(call) =\n            call.filter(|call| call.voice_session_id == record.oracle.voice_session_id)\n        else {\n            return "closed";\n        };', '        let Some(call) = call else {\n            return "closed";\n        };'),
    (K, 'vectors', 'the sound of a call is not known until the phone says', '        let live = record.oracle_audio.as_deref().unwrap_or("live") == "live";', '        let live = record.oracle_audio.as_deref().unwrap_or("paused") == "live";'),
    (K, 'vectors', 'a call the Host holds runs', '        return if live && !call.held {', '        return if live {'),
    (K, 'vectors', 'a call whose phone paused its sound runs', '        return if live && !call.held {', '        return if !call.held {'),
    (K, 'vectors', 'any call of the phone is the way back', '    match call.filter(|call| call.handoff_id == record.handoff_id) {\n        Some(_) => "running",', '    match call {\n        Some(_) => "running",'),
    (K, 'vectors', 'a way back whose call is open is not known to run', '        Some(_) => "running",\n        None => "closed",', '        Some(_) => "unknown",\n        None => "closed",'),
    (K, 'vectors', 'what the call was when a handoff was over is asked anew each time', '        true => record.oracle_call.as_str(),', '        true => oracle_call(calls, record),'),
    (K, 'vectors', 'a record is shown as of the moment it was made', '"issued_at": record.issued_at, "server_now": now,', '"issued_at": record.issued_at, "server_now": record.issued_at,'),
    (K, 'vectors', 'the time that is left is told from the moment the handoff was made', '        "ttl_ms": deadline.map(|deadline| (deadline - now).max(0)), "expires_at": deadline,', '        "ttl_ms": deadline.map(|deadline| (deadline - record.issued_at).max(0)), "expires_at": deadline,'),
    # ---- what is told
    (K, 'vectors', 'the apps are told under another name', '        event.insert("type".to_string(), json!("oracle-handoff"));', '        event.insert("type".to_string(), json!("oracle_handoff"));'),
    (K, 'vectors', 'the call that is a way back is shown the handoff too', '        if record.outward()\n            && let Some(call) = self', '        if let Some(call) = self'),
    (K, 'vectors', 'the apps are not told', '        self.told.push(Effect::Apps(Value::Object(event)));\n', ''),
    (K, 'vectors', 'any call of the phone is the one a handoff left', WHOSE_OUT, '            true\n        } else {\n            call.handoff_id == record.handoff_id\n        };'),
    (K, 'vectors', 'any call of the phone is the one that is the way back', WHOSE_OUT, '            call.voice_session_id == record.oracle.voice_session_id\n        } else {\n            true\n        };'),
    # ---- a change of state
    (K, 'vectors', 'a change does not move the revision', '        record.reason = reason.to_string();\n        record.revision += 1;', '        record.reason = reason.to_string();'),
    (K, 'vectors', 'a deadline stands on when its state is left', '        record.deadline = waits_ms(state).map(|waits| now + waits);', '        if let Some(waits) = waits_ms(state) {\n            record.deadline = Some(now + waits);\n        }'),
    (K, 'vectors', 'what the call was is not kept when a handoff is over', '        if over(state) {\n            record.oracle_call = oracle_call(self.calls, record).to_string();\n        }\n', ''),
    (K, 'vectors', 'a change is told with the reason it had before', '        record.reason = reason.to_string();\n        record.revision += 1;', '        record.revision += 1;'),
    # ---- the call a handoff left
    (K, 'vectors', 'a call that is fed again is still held', '        if let Some(call) = self.calls.get_mut(&record.principal) {\n            call.held = false;\n        }\n', ''),
    (K, 'vectors', 'a call that is fed again is not told so', '        self.tell(&call, ToCall::Resume);\n', ''),
    (K, 'vectors', 'the call that is a way back is fed again like one that was left', '    fn resume(&mut self, record: &Record) {\n        if !record.outward() {\n            return;\n        }\n', '    fn resume(&mut self, record: &Record) {\n'),
    (K, 'vectors', 'the call that is a way back hears of a failure like one that was left', '    fn failed(&mut self, record: &Record, note: bool) {\n        if !record.outward() {\n            return;\n        }\n', '    fn failed(&mut self, record: &Record, note: bool) {\n'),
    (K, 'vectors', 'a failure is told without its reason', '        let reason = record.reason.clone();\n        let failed = ToCall::Failed {', '        let reason = String::new();\n        let failed = ToCall::Failed {'),
    (K, 'vectors', 'a failure is told of the session and not of the name', '        let persona = record.agent.persona.clone();\n        let reason', '        let persona = record.agent.session.clone();\n        let reason'),
    # ---- a new handoff
    (K, 'vectors', 'a new handoff has the number of the one before', '        self.book.generation += 1;\n', ''),
    (K, 'vectors', 'a new handoff begins at revision nought', '            revision: 1,', '            revision: 0,'),
    (K, 'vectors', 'a new handoff has no deadline', '            deadline: waits_ms("offered").map(|waits| self.now + waits),', '            deadline: None,'),
    (K, 'vectors', 'a new handoff is of no Host', '            host_id: self.host_id.to_string(),', '            host_id: String::new(),'),
    (K, 'vectors', 'a new handoff is in another mode', '            mode: "hands_free".to_string(),', '            mode: "handsfree".to_string(),'),
    (K, 'vectors', 'a new handoff says what the call is from the start', '            oracle_call: String::new(),', '            oracle_call: "running".to_string(),'),
    # ---- the handoff a way back ends
    (K, 'vectors', 'a handoff that is over already is ended again by its way back', '        if parent.is_some_and(|parent| parent.state == "active") {', '        if parent.is_some() {'),
    # ---- the steps
    (K, 'vectors', 'a phone that prepares does not say it paused the sound', '                    self.sound(handoff_id, "paused");\n', ''),
    (K, 'vectors', 'a call is not held while its phone prepares', '                        self.tell(&call, ToCall::Hold);\n', ''),
    (K, 'vectors', 'a call that is told to hold is not known to be held', '                        if let Some(held) = self.calls.get_mut(&record.principal) {\n                            held.held = true;\n                        }\n', ''),
    (K, 'vectors', 'the call that is a way back is held while its phone prepares', '            ("preparing", "offered") => {\n                if outward {', '            ("preparing", "offered") => {\n                if true {'),
    (K, 'vectors', 'any phone that fails an offer changed its mind', '                let changed = !outward && reason == "local_intent_changed";', '                let changed = !outward;'),
    (K, 'vectors', 'a phone that changed its mind about an agent ends more than the offer', '                let changed = !outward && reason == "local_intent_changed";', '                let changed = reason == "local_intent_changed";'),
    (K, 'vectors', 'a phone that changed its mind on the way back is still with the agent', '                if changed {\n                    self.end_parent(&record, "local_intent_changed");\n                }\n', ''),
    (K, 'vectors', 'an offer that failed is not told to the call', '                let now = self.set(handoff_id, "failed", why);\n                if changed {\n                    self.end_parent(&record, "local_intent_changed");\n                }\n                if let Some(now) = now {\n                    self.failed(&now, false);\n                }', '                self.set(handoff_id, "failed", why);\n                if changed {\n                    self.end_parent(&record, "local_intent_changed");\n                }'),
    (K, 'vectors', 'a phone that activates does not say it let the sound go', '                    self.sound(handoff_id, "released");\n', ''),
    (K, 'vectors', 'a phone that activates cannot roll back', '            ("rolled_back", "preparing" | "activating") => self.rolled_back(&record),', '            ("rolled_back", "preparing") => self.rolled_back(&record),'),
    (K, 'vectors', 'a phone that prepares cannot roll back', '            ("rolled_back", "preparing" | "activating") => self.rolled_back(&record),', '            ("rolled_back", "activating") => self.rolled_back(&record),'),
    (K, 'vectors', 'a handoff that broke for whatever reason is rolled back late', '            ("rolled_back", "broken") if record.reason == "activation_timeout" => {', '            ("rolled_back", "broken") => {'),
    (K, 'vectors', 'a phone is with the agent late after any break', '            ("ready", "broken") if record.reason == "activation_timeout" => self.ready(&record),', '            ("ready", "broken") => self.ready(&record),'),
    (K, 'vectors', 'a phone cannot be ready late', '            ("ready", "broken") if record.reason == "activation_timeout" => self.ready(&record),', '            ("ready", "broken") if record.reason == "no such reason" => self.ready(&record),'),
    (K, 'vectors', 'a phone that prepares is ready at once', '            ("ready", "activating") => self.ready(&record),', '            ("ready", "activating" | "preparing") => self.ready(&record),'),
    (K, 'vectors', 'a way back that cannot be made leaves the phone with the agent', '                self.end_parent(&record, "return_failed");\n', ''),
    (K, 'vectors', 'a way back that cannot be made is told to nobody', '                let now = self.set(handoff_id, "broken", "rollback_failed");\n                self.end_parent(&record, "return_failed");\n                if let Some(now) = now {\n                    self.failed(&now, false);\n                }', '                self.set(handoff_id, "broken", "rollback_failed");\n                self.end_parent(&record, "return_failed");'),
    (K, 'vectors', 'a handoff that cannot be rolled back failed', '                let now = self.set(handoff_id, "broken", "rollback_failed");', '                let now = self.set(handoff_id, "failed", "rollback_failed");'),
    (K, 'vectors', 'any handoff that failed is rolled back late', '            ("rolled_back", "failed") if record.reason == "prepare_timeout" => {', '            ("rolled_back", "failed") => {'),
    (K, 'vectors', 'a late word that the phone has its call again does not feed the call', '                if outward {\n                    self.sound(handoff_id, "live");\n                    self.resume(&record);\n                }', '                if outward {\n                    self.sound(handoff_id, "live");\n                }'),
    (K, 'vectors', 'a late word that the phone has its call again leaves the sound unknown', '                if outward {\n                    self.sound(handoff_id, "live");\n                    self.resume(&record);\n                }', '                if outward {\n                    self.resume(&record);\n                }'),
    (K, 'vectors', 'a late word that the phone has its call again does not move the revision', '                    record.oracle_call = call.to_string();\n                    record.revision += 1;', '                    record.oracle_call = call.to_string();'),
    (K, 'vectors', 'a late word that the phone has its call again leaves what the call was', '                    record.oracle_call = call.to_string();\n                    record.revision += 1;', '                    record.revision += 1;'),
    (K, 'vectors', 'a late word that the phone has its call again is told to nobody', '                    self.book.retain();\n                    self.emit(&record);\n                    self.last = Some(record);', '                    self.book.retain();\n                    self.last = Some(record);'),
    (K, 'vectors', 'a handoff is given up for any reason', '            ("abandon", _) if waits_ms(state).is_some() && GIVEN_UP_FOR.contains(&reason) => {', '            ("abandon", _) if waits_ms(state).is_some() => {'),
    (K, 'vectors', 'a talk with an agent is given up for any reason', '            ("abandon", "active") if GIVEN_UP_FOR.contains(&reason) => {', '            ("abandon", "active") => {'),
    (K, 'vectors', 'a way back that is given up leaves the phone with the agent', '                self.set(handoff_id, "cancelled", reason);\n                self.end_parent(&record, reason);', '                self.set(handoff_id, "cancelled", reason);'),
    (K, 'vectors', 'a handoff that is given up failed', '                self.set(handoff_id, "cancelled", reason);\n                self.end_parent(&record, reason);', '                self.set(handoff_id, "failed", reason);\n                self.end_parent(&record, reason);'),
    (K, 'vectors', 'a phone that was restarted may not give a handoff up', 'const GIVEN_UP_FOR: [&str; 3] = ["local_stop", "client_restarted", "local_intent_changed"];', 'const GIVEN_UP_FOR: [&str; 2] = ["local_stop", "local_intent_changed"];'),
    (K, 'vectors', 'a phone that has its call again does not say the sound is live', '            self.sound(&record.handoff_id, "live");\n            self.resume(record);', '            self.resume(record);'),
    (K, 'vectors', 'a phone that has its call again is not fed', '            self.sound(&record.handoff_id, "live");\n            self.resume(record);', '            self.sound(&record.handoff_id, "live");'),
    (K, 'vectors', 'a rollback is told to the voice without a word', '        if let Some(now) = self.set(&record.handoff_id, "failed", "rolled_back") {\n            self.failed(&now, true);', '        if let Some(now) = self.set(&record.handoff_id, "failed", "rolled_back") {\n            self.failed(&now, false);'),
    (K, 'vectors', 'the focus stays where it was when the phone is with the agent', '            self.told.push(Effect::Focus(record.agent.clone()));\n', ''),
    (K, 'vectors', 'a way back that is made leaves the phone with the agent', '        self.set(&record.handoff_id, "ended", "returned");\n        self.end_parent(record, "returned");', '        self.set(&record.handoff_id, "ended", "returned");'),
    (K, 'vectors', 'the call that is the way back is not told that the user is back', '            self.tell(&call, ToCall::Returned(record.agent.persona.clone()));', '            let _ = call;'),
    # ---- deadlines
    (K, 'vectors', 'a deadline acts on a handoff that has changed since', '        if record.revision != revision {\n            return;\n        }\n', ''),
    (K, 'vectors', 'an offer nobody answered is called off', '                if let Some(now) = self.set(handoff_id, "failed", "no_ack") {', '                if let Some(now) = self.set(handoff_id, "cancelled", "no_ack") {'),
    (K, 'vectors', 'an offer nobody answered fails for another reason', '                if let Some(now) = self.set(handoff_id, "failed", "no_ack") {', '                if let Some(now) = self.set(handoff_id, "failed", "prepare_timeout") {'),
    (K, 'vectors', 'a call is left held when its phone did not get on with it', '                self.resume(&record);\n                if let Some(now) = self.set(handoff_id, "failed", "prepare_timeout") {', '                if let Some(now) = self.set(handoff_id, "failed", "prepare_timeout") {'),
    (K, 'vectors', 'a phone that did not get on with it broke the handoff', '                if let Some(now) = self.set(handoff_id, "failed", "prepare_timeout") {', '                if let Some(now) = self.set(handoff_id, "broken", "prepare_timeout") {'),
    (K, 'vectors', 'a handoff that was not activated in time failed', '                self.set(handoff_id, "broken", "activation_timeout");', '                self.set(handoff_id, "failed", "activation_timeout");'),
    # ---- the phase a handoff is ended by at an older number
    (K, 'vectors', 'anything may be said of a talk with an agent at its older number', '    phase == "abandon" && active == Some(&record.handoff_id)', '    active == Some(&record.handoff_id)'),
    (K, 'vectors', 'any handoff may be given up at its older number', '    phase == "abandon" && active == Some(&record.handoff_id)', '    phase == "abandon" && active.is_some() && !record.handoff_id.is_empty()'),
    # ---- names
    (K, 'vectors', 'a name may be longer than thirty-two signs', '        .is_some_and(|rest| rest.len() == 32 && rest.bytes().all(sixteens))', '        .is_some_and(|rest| rest.len() >= 32 && rest.bytes().all(sixteens))'),
    (K, 'vectors', 'a name may be of any letters', '        .is_some_and(|rest| rest.len() == 32 && rest.bytes().all(sixteens))', '        .is_some_and(|rest| rest.len() == 32)'),
    (K, 'vectors', 'a name may be in large letters', "    let sixteens = |sign: u8| sign.is_ascii_digit() || (b'a'..=b'f').contains(&sign);", '    let sixteens = |sign: u8| sign.is_ascii_hexdigit();'),
    # ---- a call is opened, and closed
    (K, 'vectors', 'a phone that asked for handoffs is not told which call this is', '        if call.capable {\n            let identity', '        if false {\n            let identity'),
    (K, 'vectors', 'every phone is told which call this is', '        if call.capable {\n            let identity', '        if true {\n            let identity'),
    (K, 'vectors', 'a call that is no way back is told it is one of nothing', '            "" => Value::Null,\n            handoff => json!(handoff),', '            handoff => json!(handoff),'),
    (K, 'vectors', 'an offer is over when its own call is opened again', '                if offered && record.oracle.voice_session_id != call.voice_session_id {', '                if offered {'),
    (K, 'vectors', 'a talk with an agent is over when its way back is opened', '                } else if record.state == "active" && call.handoff_id.is_empty() {', '                } else if record.state == "active" {'),
    (K, 'vectors', 'a talk with an agent goes on when the phone opens a call by itself', '                } else if record.state == "active" && call.handoff_id.is_empty() {\n                    work.set(&record.handoff_id, "ended", "client_returned");', '                } else if record.state == "active" && call.handoff_id.is_empty() {'),
    (K, 'vectors', 'a way back is over when its own call is opened', '            } else if call.handoff_id != record.handoff_id && offered {', '            } else if offered {'),
    (K, 'vectors', 'a handoff that is being activated is over when a call is opened', '            let offered = matches!(record.state.as_str(), "offered" | "preparing");\n            if over(&record.state) {', '            let offered = matches!(record.state.as_str(), "offered" | "preparing" | "activating");\n            if over(&record.state) {'),
    (K, 'vectors', 'a call that is closed takes the phone\'s newer call with it', '        if self.calls.get(principal).is_some_and(this) {', '        if self.calls.contains_key(principal) {'),
    (K, 'vectors', 'a call that is closed stays the phone\'s open call', '        if self.calls.get(principal).is_some_and(this) {\n            self.calls.remove(principal);\n        }', '        let _ = this;'),
    # ---- whose call
    (K, 'vectors', 'the administrator acts on the one call there is, whatever phone it names', '        if let Some(named) = named.filter(|named| !named.is_empty()) {\n            return Ok(named.to_string());\n        }\n', ''),
    (K, 'vectors', 'a phone may act on the call of another it names', '        if caller != ADMINISTRATOR {\n            return Ok(caller.to_string());\n        }\n        if let Some(named)', '        if let Some(named)'),
    (K, 'vectors', 'the way back is looked for among the calls that are open', '        let found: Vec<&String> = if returning {', '        let found: Vec<&String> = if false {'),
    (K, 'vectors', 'a call is looked for among the talks with agents', '        let found: Vec<&String> = if returning {', '        let found: Vec<&String> = if true {'),
    (K, 'vectors', 'of several calls the first is taken', '            _ => Err(Refused::plainly(409, "several live calls")),', '            [one, ..] => Ok((*one).clone()),'),
    (K, 'vectors', 'where there is no way back there is said to be no call', '            [] if returning => Err(Refused::plainly(404, "no active handoff")),\n', ''),
    # ---- an offer
    (K, 'vectors', 'a name of blanks is looked up among the agents', '        if wanted.is_empty() {\n            return (Err(Refused::plainly(400, "agent required")), Vec::new());\n        }\n', ''),
    (K, 'vectors', 'the Oracle is meant only in small letters', '        let returning = python::casefold(&wanted) == "oracle";', '        let returning = wanted == "oracle";'),
    (K, 'vectors', 'the blanks around a name are part of it', '        let wanted = python::strip(&said(target)).to_string();', '        let wanted = said(target);'),
    (K, 'vectors', 'another handoff is offered beside one that is being activated', '        if work.book.records.iter().any(activating) {\n            return Err(in_progress());\n        }\n', ''),
    (K, 'vectors', 'a second way back is offered beside the first', '            if work.book.records.iter().any(on_its_way) {\n                return Err(in_progress());\n            }\n', ''),
    (K, 'vectors', 'no way back is offered beside an offer of another agent', '                waits_ms(&record.state).is_some() && record.parent_handoff_id == parent.handoff_id', '                waits_ms(&record.state).is_some()'),
    (K, 'vectors', 'a way back is of no handoff', '            let record = work.new_offer(id, "agent_to_oracle", whom, &parent.handoff_id);', '            let record = work.new_offer(id, "agent_to_oracle", whom, "");'),
    (K, 'vectors', 'a way back is not told to the apps', '            let record = work.new_offer(id, "agent_to_oracle", whom, &parent.handoff_id);\n            work.emit(&record);', '            let _ = work.new_offer(id, "agent_to_oracle", whom, &parent.handoff_id);'),
    (K, 'vectors', 'an agent that is not found is a conflict', '            let status = if why == "unknown agent" { 404 } else { 409 };', '            let status = 409;'),
    (K, 'vectors', 'whatever is wrong with a name, there is no such agent', '            let status = if why == "unknown agent" { 404 } else { 409 };', '            let status = 404;'),
    (K, 'vectors', 'a phone that did not ask for handoffs is offered one', '        if !call.capable {\n            return Err(Refused::plainly(409, "handoff_unsupported"));\n        }\n', ''),
    (K, 'vectors', 'an offer the phone is preparing stands beside a new one', '                |record: &Record| matches!(record.state.as_str(), "offered" | "preparing");\n            if work.book.find(&older).is_some_and(offered) {', '                |record: &Record| matches!(record.state.as_str(), "offered");\n            if work.book.find(&older).is_some_and(offered) {'),
    (K, 'vectors', 'an older offer stands beside a new one', '                work.set(&older, "cancelled", "superseded");\n            }\n        }\n        let oracle = Left {', '            }\n        }\n        let oracle = Left {'),
    (K, 'vectors', 'the cue of an offer is not played', '        work.tell(\n            &call.voice_session_id,\n            ToCall::Offer(record.agent.persona.clone()),\n        );\n', ''),
    (K, 'vectors', 'an offer to an agent is not told to the apps', '        work.tell(\n            &call.voice_session_id,\n            ToCall::Offer(record.agent.persona.clone()),\n        );\n        work.emit(&record);', '        work.tell(\n            &call.voice_session_id,\n            ToCall::Offer(record.agent.persona.clone()),\n        );'),
    # ---- what whoever asked is answered
    (K, 'vectors', 'a way back that was made is no success', '        let done = state == "active" || record.is("ended", "returned");', '        let done = state == "active";'),
    (K, 'vectors', 'an offer is a success for one who waited', '        Some(if done || (!waited && state == "offered") {', '        Some(if done || state == "offered" {'),
    (K, 'vectors', 'an offer is no success for one who did not wait', '        Some(if done || (!waited && state == "offered") {', '        Some(if done {'),
    (K, 'vectors', 'a handoff that broke is said not to have happened', '        } else if state == "broken" {', '        } else if state == "no such state" {'),
    (K, 'vectors', 'a handoff that is over is said to be under way', '        } else if over(state) {\n            let why = format!("the handoff did not happen ({state}: {reason})");', '        } else if false {\n            let why = format!("the handoff did not happen ({state}: {reason})");'),
    # ---- an acknowledgement
    (K, 'vectors', 'what was said before is taken as new', '        if record.acks.contains(&said_before) {\n            return (Ok(json!({ "handoff": work.shown(&record) })), Vec::new());\n        }\n', ''),
    (K, 'vectors', 'what a phone said is not kept', '            work.book.records[at].acks.push(said_before);', '            let _ = (at, said_before);'),
    (K, 'vectors', 'a number that is not the handoff\'s is taken', '        if generation != record.generation\n            || (older && !finalizes_parent(work.book, &record, &phase))\n        {', '        if older && !finalizes_parent(work.book, &record, &phase) {'),
    (K, 'vectors', 'a handoff that is not the phone\'s newest takes any step', '        if generation != record.generation\n            || (older && !finalizes_parent(work.book, &record, &phase))\n        {', '        if generation != record.generation {'),
    (K, 'vectors', 'a talk with an agent cannot be ended once a newer handoff was made', '        if generation != record.generation\n            || (older && !finalizes_parent(work.book, &record, &phase))\n        {', '        if generation != record.generation || older {'),
    (K, 'vectors', 'any phone may say how far another is', '        let principal = if caller == ADMINISTRATOR {', '        let principal = if true {'),
    (K, 'vectors', 'the administrator says only how far its own handoff is', '        let principal = if caller == ADMINISTRATOR {', '        let principal = if false {'),
    (K, 'vectors', 'a number with a point is a number of a handoff', '        let generation = body["generation"]\n            .as_i64()\n            .filter(|_| !body["generation"].is_f64());', '        let generation = body["generation"].as_f64().map(|number| number as i64);'),
    (K, 'vectors', 'a step that is none is taken for one', '            PHASES.contains(&phase.as_str()),\n', '            !phase.is_empty(),\n'),
    # ---- what a phone is shown, the deadlines, and a Host that starts again
    (K, 'vectors', 'a phone is shown its oldest handoff', '        let newest = book.and_then(|book| book.records.last());', '        let newest = book.and_then(|book| book.records.first());'),
    (K, 'vectors', 'a phone is not shown what it is with an agent by', '        let active = book.and_then(Book::active);', '        let active: Option<&Record> = None;'),
    (K, 'vectors', 'a Host that starts again lets the old deadlines stand', '                if let Some(waits) = waits_ms(&record.state) {\n                    record.deadline = Some(now + waits);\n                }\n', ''),
    (K, 'vectors', 'a Host that starts again still has its calls', '        self.calls.clear();\n        for book', '        for book'),
    (K, 'vectors', 'a phone that was only asked about has a book', '        if self.books.get(principal).is_some_and(empty) {\n            self.books.remove(principal);\n        }', '        let _ = empty;'),
    # ---- the Host: what the desk says to tell is told
    (H, 'host', 'a call is not told what its desk says', '                    let _ = line.send(what);', '                    drop((line, what));'),
    (H, 'host', 'the apps are not told of a handoff', '                app.hub.publish(event, now);', '                drop(event);'),
    (H, 'host', "the Host's focus stays where it was", '                if let Ok(Some(record)) = app.fleet.select(agent.session).await {', '                if let Some(record) = None::<crate::fleet::AgentRecord>.filter(|_| !agent.session.is_empty()) {'),
    (H, 'host', 'what changed of the handoffs is not written', '    written(app, &before, desk).await?;\n    let changed', '    let changed'),
    (H, 'host', 'whoever waits for a handoff is not told that one changed', '        handoffs.changed.notify_waiters();', '        let _ = &handoffs.changed;'),
    (H, 'host', 'a call that is opened has no way by which it is told', '    app.oracle.handoffs.lines().insert(name, line);', '    drop((name, line));'),
    (H, 'host', "a call that is over stays the phone's open call", '        desk.unregister(principal, voice_session_id);', '        let _ = &desk;'),
    (H, 'host', 'a deadline is acted on at once', '            let left = u64::try_from(deadline - app.clock.now_ms()).unwrap_or(0);', '            let left = u64::try_from(deadline - deadline).unwrap_or(0);'),
    (H, 'host', 'a deadline that passed is not acted on', '                    ((), desk.expire(&principal, &handoff_id, revision, now))', '                    let _ = (&principal, &handoff_id, revision, now, &desk);\n                    ((), Vec::new())'),
    (H, 'host', 'the administrator is shown its own handoffs whatever phone it names', '            who.clone_from(named);', '            let _ = named;'),
    (H, 'host', 'the phone the administrator names is not looked at', '    let named = python::truthy(phone).then(|| oracle::said(phone));', '    let named: Option<String> = None;'),
    (H, 'host', 'a handoff is named with sixteen signs', '    let id = format!("hof_{}", ids::hex(16));', '    let id = format!("hof_{}", ids::hex(8));'),
    (H, 'host', "a handoff names the agent by its session where its own name belongs", '                agent_id: member.record.agent_id.clone(),', '                agent_id: member.record.session.clone(),'),
    (H, 'host', 'whoever asked for a handoff is kept for every step\'s time though the phone has settled it', '        if with_desk(app, settled).await? {\n            break;\n        }', '        let _ = with_desk(app, settled).await?;'),
    # ---- the Host: a call and the desk
    (C, 'host', 'every call can be put through, whatever its phone asked for', '        capable: call.asked.handoff,', '        capable: true,'),
    (C, 'host', 'no call can be put through', '            handoff: !named(query, "handoff").is_empty(),', '            handoff: false,'),
    (C, 'host', 'a call is the way back of no handoff', '        handoff_id: call.asked.handoff_id.clone(),', '        handoff_id: String::new(),'),
    (C, 'host', 'a phone is told which call this is after the rest', '    while let Ok(what) = handoff_tells.try_recv() {\n        session.handoff(what);\n    }\n', ''),
    (C, 'host', 'a call that runs is not told what its desk says', '            Some(what) = handoff_tells.recv() => session.handoff(what),', '            Some(what) = handoff_tells.recv() => drop(what),'),
    (C, 'host', 'a call that is over is not taken off the desk', '    handoffs::closed(app, &call.owner, &call.token).await;', '    let _ = (&call.owner, &call.token);'),
    (C, 'host', 'a phone is not told which call this is', '            ToCall::Session(frame) => said.phone(frame),', '            ToCall::Session(frame) => drop(frame),'),
    (C, 'host', 'the chime of the agent is not played', '            ToCall::Offer(persona) => machine.handoff_offer(&persona, said, now),', '            ToCall::Offer(persona) => drop(persona),'),
    (C, 'host', "a handoff is not shown on the call's own socket", '            ToCall::Mirror(record) => machine.handoff_mirror(&record, said),', '            ToCall::Mirror(record) => drop(record),'),
    (C, 'host', 'a call that is put through is not held', '            ToCall::Hold => machine.handoff_hold(),', '            ToCall::Hold => {}'),
    (C, 'host', 'a call that is fed again stays held', '            ToCall::Resume => machine.handoff_resume(),', '            ToCall::Resume => {}'),
    (C, 'host', 'a call is not told that the user is back', '            ToCall::Returned(persona) => machine.handoff_returned(&persona, said, now),', '            ToCall::Returned(persona) => drop(persona),'),
    (C, 'host', 'a failure is told to the voice in a word whatever it was', '            } => machine.handoff_failed((&persona, &reason, note), said, now),', '            } => machine.handoff_failed((&persona, &reason, note || true), said, now),'),
    (C, 'host', 'a failure is never told to the voice', '            } => machine.handoff_failed((&persona, &reason, note), said, now),', '            } => machine.handoff_failed((&persona, &reason, note && false), said, now),'),
    # ---- the agents' own tool
    (T, 'host', 'the tool does not name the phone it was given', '    let phone = json!(parsed.option("--principal").unwrap_or(""));', '    let phone = json!("");'),
    (T, 'host', 'the tool says a refusal is a success', '        Ok(Err(refused)) => match serde_json::to_string_pretty(&refused.body()) {\n            Ok(body) => ToolAnswer::printed(1, body),', '        Ok(Err(refused)) => match serde_json::to_string_pretty(&refused.body()) {\n            Ok(body) => ToolAnswer::printed(0, body),'),
    (T, 'host', 'the tool asks for the Oracle whatever agent it was given', '    let agent = json!(parsed.positional[0]);', '    let agent = json!("oracle");'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
