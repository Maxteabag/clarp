#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

What agents finished for the user, as the voice of a call of the Oracle is told of it: the kernel's rule of which
finished turns are shown, the name the end of a turn is told under, what an agent's log says it last finished for
its user, and how the Host keeps that and hands it to a call.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'oracle_completions_vectors'],
    'actor': ['test', '-p', 'clarp-actor', '--test', 'actor', '--', 'what_an_agent_last_finished'],
    'unit': ['test', '-p', 'clarp-host', '--lib', '--', 'what_an_agent_finished_for_its_user'],
    'call': ['test', '--test', 'host', '--', 'a_call_of_the_oracle_is_told_of_what_agents_finished'],
}
K = 'clarp-kernel/src/oracle/attention.rs'
N = 'clarp-kernel/src/notify.rs'
A = 'clarp-actor/src/lib.rs'
H = 'clarp-host/src/agent.rs'
W = 'clarp-host/src/wire.rs'
C = 'clarp-host/src/oracle/call.rs'
# NOT PLANTED, each with its reason:
#   that the walk through an agent's messages ends at one that entered the conversation before the day that is
#     asked for (`if item.ts_ms < since_ms`, in `last_finished_for`): what it leaves unread ended longer ago still,
#     and would be nothing for that reason. It only saves the reading.
#   that a message at the very position a conversation was started again at is not of the new one (`item.seq <=
#     floor` against `<`): what stands at that position is the record of the start itself, never a message.
#   that what an agent last finished is read anew only for turns of the user's or the Oracle's (`theirs(...)` in the
#     worker's `show`): reading it anew for every turn finds the same, more often.
#   that a number of a record which is none is nought (`as_whole`): the Host writes a number there.
#   a moment of its own at which the worker reads anew what an agent finished when the agent is started again: an
#     agent that is started again is given a conversation of another name and is opened anew for it, and what it
#     finished is read when an agent is opened. (A rule for that moment was planted and missed, and the moment went.)
#   that a call asks what was finished as of now and not as of some moment long before (`let at = ...now_ms()`): seen
#     from before, every end is still to come, and what is still to come is shown as new, as what has just ended is.
#     A test would have to wait a quarter of an hour to tell the two apart. As of a later moment is planted.
NAMED = ('        let named = [\n            "source_message_id",\n            "cause_message_id",\n'
         '            "backend_session_id",\n        ];')
WHOSE = '        if !for_the_user(told_for) || !for_the_user(asked_by) || !in_one {'
ORDER = '    told.sort_by(|one, other| ended_and_named(other).cmp(&ended_and_named(one)));'
LOOKED = '    let looked_at = (most * 4).min(PENDING_MOST * 4);'
MOMENTS = ('        if began.is_some_and(|live| theirs(&live.origin))\n'
           '            || ended.is_some_and(|last| theirs(&last.origin))\n'
           '            || status.waiting != was_waiting\n        {')
PLANTS = [
    # Which finished turns are shown: the kernel's rule.
    (K, 'vectors', 'what the user was not told of is shown', '        row["notify"] == true\n            && row["unread"] == true', '        row["unread"] == true'),
    (K, 'vectors', 'what the user has read is shown', '        row["notify"] == true\n            && row["unread"] == true', '        row["notify"] == true'),
    (K, 'vectors', 'a turn that names no answer is shown with an answer that has no name', NAMED, '        let named = ["cause_message_id", "backend_session_id"];'),
    (K, 'vectors', 'a turn that names no message is shown with a message that has no name', NAMED, '        let named = ["source_message_id", "backend_session_id"];'),
    (K, 'vectors', 'a turn of no conversation is shown', NAMED, '        let named = ["source_message_id", "cause_message_id"];'),
    (K, 'vectors', "an answer that is the user's own message is shown", '            && answer["role"] == "assistant"\n', ''),
    (K, 'vectors', "another agent's answer is shown as this agent's", '            && answer["agent_id"] == row["agent_id"]\n', ''),
    (K, 'vectors', 'an answer in another conversation is shown', '            && answer["backend_session_id"] == row["backend_session_id"]\n', ''),
    (K, 'vectors', "a turn begun by the agent's own message is shown", '            && asked["role"] == "user"\n', ''),
    (K, 'vectors', 'a turn begun by a message to another agent is shown', '            && asked["agent_id"] == row["agent_id"]\n', ''),
    (K, 'vectors', 'the oldest is shown first', ORDER, '    told.sort_by(|one, other| ended_and_named(one).cmp(&ended_and_named(other)));'),
    (K, 'vectors', 'of two that ended at one moment the one that was kept first is shown first', '    (as_whole(&row["done_ts"]), as_text(&row["notification_id"]))', '    (as_whole(&row["done_ts"]), "")'),
    (K, 'vectors', 'names are put in order without regard to large and small letters', ORDER, '    told.sort_by(|one, other| {\n        let key = |row: &Value| (as_whole(&row["done_ts"]), as_text(&row["notification_id"]).to_lowercase());\n        key(*other).cmp(&key(*one))\n    });'),
    (K, 'vectors', 'three turns are looked at for each that is asked for', LOOKED, '    let looked_at = (most * 3).min(PENDING_MOST * 4);'),
    (K, 'vectors', 'five turns are looked at for each that is asked for', LOOKED, '    let looked_at = (most * 5).min(PENDING_MOST * 4);'),
    (K, 'vectors', 'as many turns are looked at as four for each that is asked for, however many', LOOKED, '    let looked_at = most * 4;'),
    (K, 'vectors', 'never more than sixty turns are looked at', LOOKED, '    let looked_at = (most * 4).min(PENDING_MOST * 3);'),
    (K, 'vectors', 'more are shown than are asked for', '        if shown.len() >= most {\n            break;\n        }\n', ''),
    (K, 'vectors', 'one more is shown than is asked for', '        if shown.len() >= most {', '        if shown.len() > most {'),
    (K, 'vectors', 'who a turn was told to be for is read with the blanks around it', 'python::strip(as_text(&row["origin"])));', 'as_text(&row["origin"]));'),
    (K, 'vectors', 'a message that does not say who wrote it is nobody\'s', '            "" => told_for,', '            "" => "",'),
    (K, 'vectors', 'who wrote a message is read with the blanks around it', '            said => python::strip(said),', '            said => said,'),
    (K, 'vectors', "a message that says only blanks of who wrote it is taken for what the turn was told to be for", '        let asked_by = match as_text(&asked["origin"]) {', '        let asked_by = match python::strip(as_text(&asked["origin"])) {'),
    (K, 'vectors', 'what a turn was told to be for does not matter', WHOSE, '        if !for_the_user(asked_by) || !in_one {'),
    (K, 'vectors', 'who wrote the message that began a turn does not matter', WHOSE, '        if !for_the_user(told_for) || !in_one {'),
    (K, 'vectors', 'a message in another conversation than the answer began the turn all the same', WHOSE, '        if !for_the_user(told_for) || !for_the_user(asked_by) {'),
    (K, 'vectors', "what the Oracle handed on is not the user's own", '    matches!(origin, "user" | "oracle")', '    matches!(origin, "user")'),
    (K, 'vectors', "what one agent asked of another is the user's own", '    matches!(origin, "user" | "oracle")', '    matches!(origin, "user" | "oracle" | "agent")'),
    (K, 'vectors', 'what ended a day ago to the moment is not shown', '        if now_ms - ended > FINISHED_SHOWN_FOR_MS\n', '        if now_ms - ended >= FINISHED_SHOWN_FOR_MS\n'),
    (K, 'vectors', 'what ended within twenty-five hours is shown', 'pub const FINISHED_SHOWN_FOR_MS: i64 = 24 * 60 * 60 * 1000;', 'pub const FINISHED_SHOWN_FOR_MS: i64 = 25 * 60 * 60 * 1000;'),
    (K, 'vectors', 'what ended more than twenty-three hours ago is not shown', 'pub const FINISHED_SHOWN_FOR_MS: i64 = 24 * 60 * 60 * 1000;', 'pub const FINISHED_SHOWN_FOR_MS: i64 = 23 * 60 * 60 * 1000;'),
    (K, 'vectors', 'what the user wrote to since is shown', '            || row["written_to_since"] == true\n', ''),
    (K, 'vectors', 'an answer that says nothing is shown', '            || answer.is_empty()\n', ''),
    (K, 'vectors', 'an answer is shown with the blanks around it', '        let answer = python::strip(as_text(&row["answer"]["text"]));', '        let answer = as_text(&row["answer"]["text"]);'),
    (K, 'vectors', 'an agent is told by its session where it has a name', '            "" => &row["session"],\n            _ => &row["persona"],', '            "" => &row["persona"],\n            _ => &row["session"],'),
    (K, 'vectors', 'a turn is shown as for whom it was told to be, whoever wrote its message', '        put(&mut record, "origin", json!(asked_by));', '        put(&mut record, "origin", json!(told_for));'),
    (K, 'vectors', 'a turn is shown as ended at the moment that is asked at', '        put(&mut record, "reference_ts", json!(ended));', '        put(&mut record, "reference_ts", json!(now_ms));'),
    (K, 'vectors', 'what ended a quarter of an hour ago to the moment is old', '        let old = now_ms - ended > FINISHED_NEW_FOR_MS;', '        let old = now_ms - ended >= FINISHED_NEW_FOR_MS;'),
    (K, 'vectors', 'what ended within sixteen minutes is new', 'pub const FINISHED_NEW_FOR_MS: i64 = 15 * 60 * 1000;', 'pub const FINISHED_NEW_FOR_MS: i64 = 16 * 60 * 1000;'),
    (K, 'vectors', 'what ended more than fourteen minutes ago is old', 'pub const FINISHED_NEW_FOR_MS: i64 = 15 * 60 * 1000;', 'pub const FINISHED_NEW_FOR_MS: i64 = 14 * 60 * 1000;'),
    (K, 'vectors', 'nothing that is shown is said to be old', '        put(&mut record, "stale", json!(old));', '        put(&mut record, "stale", json!(false));'),
    (K, 'vectors', "the agent's name is written after its session", '        put(&mut record, "agent", agent.clone());\n        for name in ["session", "backend_session_id"] {\n            put(&mut record, name, row[name].clone());\n        }', '        for name in ["session", "backend_session_id"] {\n            put(&mut record, name, row[name].clone());\n        }\n        put(&mut record, "agent", agent.clone());'),
    # The name the end of a turn is told under.
    (N, 'vectors', 'the agent and the moment are written together without a sign between', '    let mark = Sha1::digest(format!("{agent_id}\\0{done_ms}").as_bytes());', '    let mark = Sha1::digest(format!("{agent_id}{done_ms}").as_bytes());'),
    (N, 'vectors', 'the moment is written before the agent', '    let mark = Sha1::digest(format!("{agent_id}\\0{done_ms}").as_bytes());', '    let mark = Sha1::digest(format!("{done_ms}\\0{agent_id}").as_bytes());'),
    (N, 'vectors', 'a name is sixteen signs of the digest', '    format!("pn-{}", &mark[..20])', '    format!("pn-{}", &mark[..16])'),
    (N, 'vectors', 'a name begins otherwise', '    format!("pn-{}", &mark[..20])', '    format!("un-{}", &mark[..20])'),
    (N, 'vectors', 'a byte of the digest that is small is written as one sign', '    let mark: String = mark.iter().map(|byte| format!("{byte:02x}")).collect();', '    let mark: String = mark.iter().map(|byte| format!("{byte:x}")).collect();'),
    # What an agent's log says it last finished for its user.
    (A, 'actor', 'what the user wrote and still waits is not what he wrote since', '        if waiting.any(|queued| asked_by(queued.origin.name())) {\n            return Ok(None);\n        }\n', ''),
    (A, 'actor', 'whatever waits for the agent is what the user wrote since', '        if waiting.any(|queued| asked_by(queued.origin.name())) {', '        if waiting.next().is_some() {'),
    (A, 'actor', 'what an agent finished before it was started again is still what it last finished', '                if item.seq <= floor {\n                    return Ok(None);\n                }\n', ''),
    (A, 'actor', 'the newest message is taken for theirs, whoever wrote it', '                if !asked_by(message.origin.name()) {', '                if false {'),
    (A, 'actor', 'what another agent wrote is what the user wrote since', '                    if item.ts_ms < since_ms {\n                        return Ok(None);\n                    }\n                    continue;', '                    return Ok(None);'),
    (A, 'actor', 'a message of theirs that is being answered is passed over', '                let Some(end) = ended(item)? else {\n                    return Ok(None);\n                };', '                let Some(end) = ended(item)? else {\n                    continue;\n                };'),
    (A, 'actor', 'a turn that was stopped finished what it had said by then', '                if end.status != "completed" || end.ended_at_ms < since_ms {', '                if end.ended_at_ms < since_ms {'),
    (A, 'actor', 'what was finished too long ago for whoever asks is still told', '                if end.status != "completed" || end.ended_at_ms < since_ms {', '                if end.status != "completed" {'),
    (A, 'actor', 'what was finished at the very moment that is asked from is too long ago', '                if end.status != "completed" || end.ended_at_ms < since_ms {', '                if end.status != "completed" || end.ended_at_ms <= since_ms {'),
    (A, 'actor', 'a turn that said nothing finished its own message', '                let said = |answer: &Versioned| matches!(answer.entry, Entry::Reply(_));', '                let said = |_answer: &Versioned| true;'),
    # What the Host keeps of it.
    (W, 'unit', 'what an agent finished is kept though nobody was told of it', '    let mut told = user_notification(record, &id, &from, answer, end.ended_at_ms)?;', '    let mut told = user_notification(record, &id, &from, answer, end.ended_at_ms).unwrap_or_else(|| json!({}));'),
    (W, 'unit', 'the end of a turn is kept under a name of the moment alone', '    let id = notify::id(&record.agent_id, end.ended_at_ms);\n    let mut told', '    let id = notify::id("", end.ended_at_ms);\n    let mut told'),
    (W, 'unit', 'what an agent finished is kept as something the user was not told of', '    told["notify"] = json!(true);', '    told["notify"] = json!(false);'),
    (W, 'unit', 'what an agent finished is kept as in no conversation', '    told["backend_session_id"] = json!(conversation);', '    told["backend_session_id"] = json!("");'),
    (W, 'unit', 'the conversation of what an agent finished is the name the Host gave it, whatever its tool calls it', '    let conversation = session_name(record, status);', '    let conversation = record.conversation_id.clone();'),
    (W, 'unit', 'the answer is kept as the message that began the turn', '    told["answer"] = as_it_has(answer);', '    told["answer"] = as_it_has(asked);'),
    (W, 'unit', 'the message that began the turn is kept as its answer', '    told["asked"] = as_it_has(asked);', '    told["asked"] = as_it_has(answer);'),
    (W, 'unit', 'what an agent finished is kept as written to since', '    told["written_to_since"] = json!(false);', '    told["written_to_since"] = json!(true);'),
    (W, 'unit', 'the two rows are kept as of no agent', '            "role": row["role"], "agent_id": record.agent_id,', '            "role": row["role"], "agent_id": "",'),
    (W, 'unit', 'the two rows are kept without what they say', '"origin": row["origin"], "text": row["text"],', '"origin": row["origin"], "text": "",'),
    (W, 'unit', 'the two rows are kept as the user\'s own, whoever began the turn', '"origin": row["origin"], "text": row["text"],', '"origin": "user", "text": row["text"],'),
    # When the agent's thread reads it anew, and what a call is handed.
    (H, 'call', 'what an agent finished stays what it was when the user writes to it again', MOMENTS, MOMENTS.replace('        if began.is_some_and(|live| theirs(&live.origin))\n            || ended', '        if ended')),
    (H, 'call', 'what an agent finishes is not read when its turn ends', MOMENTS, MOMENTS.replace('            || ended.is_some_and(|last| theirs(&last.origin))\n', '')),
    (H, 'call', "what an agent finished stays what it was when the user's words come to wait for it, or are taken back", MOMENTS, MOMENTS.replace('\n            || status.waiting != was_waiting\n', '\n')),
    (H, 'call', 'what an agent finished is told by the name the agent had then', '                if self.finished.is_some() && self.actor.is_some() {\n                    self.finished = self.last_finished();', '                if false {\n                    self.finished = self.last_finished();'),
    (H, 'call', 'the apps are told the end of a turn under a name of the moment alone', '            let id = clarp_kernel::notify::id(&record.agent_id, ended.ended_at_ms);', '            let id = clarp_kernel::notify::id("", ended.ended_at_ms);'),
    (H, 'call', 'what an agent finished is read as finished long ago', '        let since = self.now_ms() - attention::FINISHED_SHOWN_FOR_MS;', '        let since = self.now_ms() + attention::FINISHED_SHOWN_FOR_MS;'),
    (H, 'call', "what an agent last finished for anybody is taken for the user's", '            .last_finished_for(attention::for_the_user, since)', '            .last_finished_for(|_| true, since)'),
    (C, 'call', 'a call is told of what no agent finished', '            let had = views.iter().filter_map(|view| view.finished.as_deref());', '            let had = views.iter().filter_map(|view| view.finished.as_deref()).take(0);'),
    (C, 'call', 'a call is told of one thing an agent finished and no more', '            let finished = attention::finished(had, at, attention::PENDING_MOST);', '            let finished = attention::finished(had, at, 1);'),
    (C, 'call', 'a call asks what was finished as of a day from now', '            let at = self.app.clock.now_ms();\n            let finished', '            let at = self.app.clock.now_ms() + 2 * attention::FINISHED_SHOWN_FOR_MS;\n            let finished'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
