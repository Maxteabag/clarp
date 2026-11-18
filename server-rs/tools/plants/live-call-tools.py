#!/usr/bin/env python3
"""Wrong rules in the tools of a live call, planted against the Python Host's receipts."""
import runner

RUNS = {'receipts':['test','--test','live_call_tools_vectors']}
RECEIPTS = 'clarp-kernel/src/live_call/receipts.rs'
PLANTS = []
for what,old,new in [
    ('a refusal does not require a refusal status','code != Some(200)','code == Some(200)'),
    ('absence of intent is accepted','!answer["intent"].is_string()','false'),
    ('a direction may omit its session','answer.get("sesjon").is_none()','false'),
    ('a number says a send was observed','!answer["sent"].is_boolean()','false'),
    ('an unobserved forward is confirmed','answer["sent"] != true','false'),
    ('an unknown non-forward is definite','intent_status=="ukjent"','intent_status == "unknown"'),
    ('a task started now loses its note','answer["base_task"]=="started_now"','answer["base_task"] == "next"'),
    ('receipt lines have no separator','current.push_str(" | ")','current.push_str(" ")'),
    ('UTF-8 truncation loses an extra byte','limit.saturating_sub(3)','limit.saturating_sub(4)'),
    ('an empty agent label changes the action name','"action"','"unknown action"'),
    ('the member name exceeds its byte cap','&or(&r["agent"], ""), 40','&or(&r["agent"], ""), 41'),
    ('the status exceeds its byte cap','&or(&r["status"], "unknown"), 60','&or(&r["status"], "unknown"), 61'),
    ('an empty result loses its default reason','"missing fields"','"unavailable fields"'),
]:
    PLANTS.append((RECEIPTS,'receipts',what,old,new))

def cases(module, changes, run='receipts'):
    file = 'clarp-kernel/src/live_call/' + module + '.rs'
    PLANTS.extend((file,run,what,old,new) for what,old,new in changes)

cases('team', [
    ('the question id has one fewer sign','prefix.len() + 24','prefix.len() + 23'),
    ('a session is looked up by display name','&member["session"] == target','&member["name"] == target'),
    ('the label is the session','let name = member["name"]','let name = member["session"]'),
    ('one transcript row is below the minimum','1..=5','2..=5'),
    ('five transcript rows are too many','1..=5','1..=4'),
    ('a sixth transcript row is accepted','1..=5','1..=6'),
    ('default transcript length changes','.unwrap_or(3) as usize','.unwrap_or(2) as usize'),
    ('a fully sized request is refused','size > 4000','size >= 4000'),
    ('one extra request character passes','size > 4000','size > 4001'),
    ('the twelfth delegation is refused','self.delegations >= 12','self.delegations >= 11'),
    ('a thirteenth delegation goes out','self.delegations >= 12','self.delegations >= 13'),
    ('admission no longer counts the delegation','self.delegations += 1','self.delegations += 0'),
    ('direct prompt refusal claims uncertain delivery','matches!(outcome, Prompt::Refused(_))','matches!(outcome, Prompt::Uncertain(_))'),
    ('a successful prompt loses its receipt','receipt["sent"] = json!(true)','receipt["sent"] = json!(false)'),
])
cases('callback', [
    ('one missing retry on uncertainty','for _ in 0..2','for _ in 0..1'),
    ('one extra retry on uncertainty','for _ in 0..2','for _ in 0..3'),
    ('a director server error counts as certain','code < 500','code <= 500'),
    ('declined recipient is scheduled','if !r.eligible','if false'),
    ('cancel goes as schedule','b.body("cancel", &id)','b.body("schedule", &id)'),
    ('cancel id belongs to another namespace','operation("cbx-"','operation("rq-"'),
    ('schedule id omits normalized reason','&minutes_text, reason','&minutes_text, ""'),
    ('new booking omits explicit replacement','body["explicit"] = json!(true)','body["explicit"] = json!(false)'),
    ('old director mismatch is never reconciled','Some((409, reply))','Some((408, reply))'),
    ('replacement carries the wrong pending id','reply["pending"].clone()','reply["operation_id"].clone()'),
    ('legacy replacement uses the original id','parts.push("after-cancel")','parts.push("before-cancel")'),
    ('replacement retains supersedes','fields.remove("supersedes")','fields.remove("other")'),
    ('replacement retains explicit','fields.remove("explicit")','fields.remove("other")'),
    ('cancelled operation id omitted','receipt["operation_id"] = reply["operation_id"].clone()','receipt["operation_id"] = Value::Null'),
    ('already scheduled is refused','!matches!(result, "scheduled" | "already_scheduled")','result != "scheduled"'),
    ('unknown result accepted','!matches!(result, "scheduled" | "already_scheduled")','false'),
])
cases('routing',[
    ('later agent fragment treated as user speech','Some("session.output_transcript.delta") => "assistant"','Some("session.output_transcript.delta") => "user"'),
    ('truncation drops a fragment early','self.fragments.len() > 40','self.fragments.len() >= 40'),
    ('an extra fragment survives','self.fragments.len() > 40','self.fragments.len() > 41'),
    ('latest user text loses a character','nth(3999)','nth(3998)'),
    ('an extra character survives','nth(3999)','nth(4000)'),
    ('one fewer route permitted','self.calls >= 30','self.calls >= 29'),
    ('one extra route permitted','self.calls >= 30','self.calls >= 31'),
    ('quiet threshold one instant too late','now - self.last_user < 1.2','now - self.last_user <= 1.200001'),
    ('route before quiet time','now - self.last_user < 1.2','now - self.last_user < 1.19'),
    ('already routed speech is current','f.revision > self.routed','f.revision >= self.routed'),
    ('waiting call has no commentary','if route.waiting && receipts.is_empty()','if false && receipts.is_empty()'),
])
cases('transcript',[
    ('a valid 120-character call id is digested','call.len() <= 120','call.len() < 120'),
    ('a 121-character id becomes a filename','call.len() <= 120','call.len() <= 121'),
    ('unsafe call id has an incorrect digest prefix','operation("call-"','operation("live-"'),
    ('pacing entries counted as receipts','Some("pacing") =>','Some("other-pacing") =>'),
    ('last fragment end not updated','last["end"] = e["at"].clone()','last["end"] = last["at"].clone()'),
])
cases('answers',[
    ('stored text loses the served label','if f.served {','if false {'),
    ('word for word label inverted','if f.verbatim {','if !f.verbatim {'),
    ('a single part is called the end','if total > 1 {','if total >= 1 {'),
    ('no pause hint on the eighth part','a.auto + 1 >= 8','a.auto + 1 > 8'),
    ('auto stops after seven parts','a.auto < 8','a.auto < 7'),
    ('auto continues into the ninth part','a.auto < 8','a.auto < 9'),
    ('stalled voice retries at the deadline','now - a.sent_at > 15.0','now - a.sent_at >= 15.0'),
    ('a sent part is replayed by pacing','a.next += 1','a.next += 0'),
    ('every sent part acknowledges again','a.ack = false','a.ack = true'),
    ('no known-drop retry','a.unsent = true','a.unsent = false'),
    ('an uncertain send is automatically replayed','None => {\n                a.unsent = false','None => {\n                a.unsent = true'),
    ('complete status uses the commentary channel','channel: "thinking"','channel: "commentary"'),
    ('wrong budget for answer text','450usize.saturating_sub(worst)','451usize.saturating_sub(worst)'),
])
cases('receipts',[
    ('status 400 loses definite refusal','Some(400 | 409 | 413)','Some(409 | 413)'),
    ('status 409 loses definite refusal','Some(400 | 409 | 413)','Some(400 | 413)'),
    ('status 413 loses definite refusal','Some(400 | 409 | 413)','Some(400 | 409)'),
    ('refused row accepted even when it says forwarded','answer.get("result").is_none() || result == "refused"','true'),
    ('more time detail than permitted','&text(&r["at"]), 8','&text(&r["at"]), 9'),
    ('regi status too long','&text(regi), 24','&text(regi), 25'),
    ('regi reason too long','&text(reason), 80','&text(reason), 81'),
    ('a queued status is unnecessarily spoken','regi != "venter"','true'),
    ('status fallback loses the error','&or(&data["error"], "-")','"-"'),
    ('last user text can be reported as an agent answer','m["role"] == "assistant"','m["role"] == "user"'),
    ('detail of an agent has one more byte','cut(&said, 120)','cut(&said, 121)'),
])

RUNS['host']=['test','--test','host','--','a_live_call_uses_its_frozen_team']
H='clarp-host/src/calls/tools.rs'
for what,old,new in [
    ('the director callback response is forgotten','callback::execute(&b, r, &mut row, &mut effects)','Err::<Value,_>(callback::Failure::Ledger)'),
    ('no call can ask its contact','if tool == "ask_my_agent"','if tool == "never-ask"'),
    ('a completed answer cannot be replayed','if tool == "read_result"','if tool == "never-read"'),
    ('replaying falls back to the live cursor','self.answers.read(args)','self.answers.read(&json!({}))'),
    ('a queued operation never reconciles','receipt["status"] == "queued_at_regi"','receipt["status"] == "never-queued"'),
    ('the confirmed intent is called uncertain','Some("sendt") if answer["sent"] == true','Some("sendt") if false'),
    ('automatic booking is never attempted','!self.auto_decided','false'),
    ('the callback is attributed to the director','principal: &self.call.principal','principal: "director"'),
    ('queued delegation uses a prompt','cfg["delegate"]["kind"] == "prompt"','true'),
    ('plain delegation uses the HTTP director','cfg["delegate"]["kind"] == "prompt"','false'),
    ('the tool team loses its actor','self.call.snapshot["live_tools"]["contact"]["name"]','self.call.snapshot["live_tools"]["contact"]["missing"]'),
    ('only the first action is executed','.take(8)','.take(1)'),
]:
    PLANTS.append((H,'host',what,old,new))
# Endpoint isolation, proxy disabling and redirect refusal are not planted. Changing
# them could allow tests to contact an external provider or director.
cases('team', [
    ('question words joined with a different separator','.join(" ");\n        if question.is_empty()', '.join("|");\n        if question.is_empty()'),
    ('question cannot fill the last character','question.chars().count() > 4000','question.chars().count() >= 4000'),
    ('one extra question character is sent','question.chars().count() > 4000','question.chars().count() > 4001'),
    ('question ids are case sensitive','&question.to_lowercase()','&question'),
    ('phone omitted from question binding','&[host, phone, call, session,','&[host, call, session,'),
    ('an ambiguous readback says failed','if unknown {','if false {'),
    ('failed admission loses its reason','if row["status"]=="failed"','if false'),
])
for what,old,new in [
    ('late admitted action is sent after hangup','row["status_text"] = json!("not_sent")','row["status_text"] = json!("forwarded")'),
    ('role of answering agent is substituted','team::prompted(proposed.name, &id, verdict)','team::prompted(proposed.session, &id, verdict)'),
    ('reconciliation listens on the wrong intent','encode(receipt["intent"].as_str().unwrap_or(""))','encode("wrong")'),
]:
    PLANTS.append((H,'host',what,old,new))

RUNS['openings']=['test','--test','listen_first_vectors']
O='clarp-kernel/src/rings/opening.rs'
for what,old,new in [
    ('fallback exposes the owner nonce','if secret && python::truthy(&op["token"])','if python::truthy(&op["token"])'),
    ('renew omits the old epoch check','op["epoch"] != body["expected_epoch"]','false'),
    ('renew ignores owner nonce','op["owner_nonce"] != body["owner_nonce"]','false'),
    ('another device may claim','if op["principal"] != owner','if false'),
    ('a stale claim is admitted','else if op["epoch"] != body["epoch"]','else if false'),
    ('accept an empty claim id','python::strip(id).is_empty()','false'),
    ('a 128-character id is refused','id.chars().count() > 128','id.chars().count() >= 128'),
    ('a 129-character id is accepted','id.chars().count() > 128','id.chars().count() > 129'),
    ('different retry accepted','prior["fingerprint"] != fp','false'),
    ('replay count grows again','"opening":shown,"changed":false','"opening":shown,"changed":true'),
    ('drop a stored claim too early','requests.len() > 64','requests.len() >= 64'),
    ('drop a stored claim too late','requests.len() > 64','requests.len() > 65'),
    ('newest request evicted first','.min_by_key(|(_, v)| v["at"].as_i64())','.max_by_key(|(_, v)| v["at"].as_i64())'),
    ('renew reuses nonce','op["owner_nonce"] = json!(w.nonce)','op["owner_nonce"] = json!("old")'),
    ('intent overwrites previous generation','op["intent"]["gen"].as_i64().unwrap_or(0) + 1','1'),
    ('silence at deadline is refused','< op["silence_ms"].as_i64().unwrap_or(1250)','<= op["silence_ms"].as_i64().unwrap_or(1250)'),
    ('late watermark unnecessary','["at_ms", "watermark_ms"].iter().any','["at_ms"].iter().any'),
    ('clock unnecessary','["at_ms", "watermark_ms"].iter().any','["watermark_ms"].iter().any'),
    ('abandon rejects empty capture','Some("empty_capture" | "capture_aborted")','Some("capture_aborted")'),
    ('abandon rejects capture abort','Some("empty_capture" | "capture_aborted")','Some("empty_capture")'),
    ('second onset changes the latch','changed = false;','changed = true;'),
    ('late greeting is not ordinary','shown["greeting"] = json!("ordinary")','shown["greeting"] = json!("greeting")'),
    ('intent with no row went out','_ if row.is_null() => "cancelled"','_ if row.is_null() => "started"'),
    ('mismatched trace cancels a different row','_ if row["trace_id"] != intent["trace_id"] => "unknown"','_ if row["trace_id"] != intent["trace_id"] => "cancelled"'),
    ('an intended launch already ran','Some("intended") => "cancelled"','Some("intended") => "started"'),
    ('a started intent is withdrawn','Some("started") => return "started"','Some("started") => return "cancelled"'),
    ('a cancelled intent is considered unknown','Some("cancelled") => return "cancelled"','Some("cancelled") => return "unknown"'),
    ('queued row is left behind','effects.withdraw = true;', 'effects.withdraw = false;'),
    ('an earlier claim may be replaced','Some("queued" | "parked") if row["claimed_at"].is_null()', 'Some("queued" | "parked")'),
    ('a greeting from the wrong phone is accepted','if op["principal"] != g.principal','if false'),
    ('a greeting may go to any call','|| op["call_id"] != g.call',''),
    ('a greeting may go to another session','op["session"] != g.session ||',''),
    ('greeting may use old epoch','if op["epoch"] != *g.epoch','if false'),
    ('the wrong message confirms a greeting','op["greeting"]["client_msg_id"] == id','false'),
    ('a greeting is marked confirmed on ambiguous send','if ok { "confirmed" } else { "unknown" }','if ok { "confirmed" } else { "confirmed" }'),
]:
    PLANTS.append((O,'openings',what,old,new))

for what,old,new in [
    ('a foreign waiting turn is intercepted','if w.queue["foreign"] == true {','if false {'),
    ('a gate accepts a different admission','intent["admission_id"] == id && intent["trace_id"] == trace','intent["trace_id"] == trace'),
    ('a gate accepts a different trace','intent["admission_id"] == id && intent["trace_id"] == trace','intent["admission_id"] == id'),
    ('a duplicate claimed launch is replayed','mark_unknown(op, &mut fx.opening);','return json!(true);'),
    ('claim accepts a parked row','if status != "queued"','if status != "queued" && status != "parked"'),
    ('claim does not take the queue row','fx.claim = true;','fx.claim = false;'),
    ('unsent release leaves the slot claimed','fx.release = true;','fx.release = false;'),
    ('an unprovable launch does not mark the row unknown','fx.launched_unknown = true;','fx.launched_unknown = false;'),
    ('parked launch ignores a newer Stop','if action == Gate::Parked {\n                if let Some(reason) = w.fence','if action == Gate::Parked {\n                if let Some(reason) = None::<&str>'),
    ('claim refuses to record an uncertain row','fx.unknown = true;','fx.unknown = false;'),
    ('release works without a claimed queue row','|| status != "claimed"','|| status == "gone"'),
    ('an end tag calls a greeting an initiative','"greeting_reply"','"initiative"'),
    ('a retired opening can still be synthesized','let skip = !op["retired"].is_null()','let skip = false'),
    ('a later greeting does not stop initiative audio','|| python::truthy(&op["late_greeting"])','|| false'),
    ('an audio tag identifies the other epoch','"epoch":op["epoch"],"kind":kind','"epoch":0,"kind":kind'),
    ('an invalid uuid length is accepted','text.len() == 36','text.len() >= 35'),
    ('uppercase UUID digits identify the Host',"(b'a'..=b'f').contains(&c)","c.is_ascii_hexdigit()"),
    ('metadata loses its call identity','"call_id":op["call_id"],"opening"','"call_id":"another","opening"'),
    ('initiative header has another cause','Some("no_mic")=>','Some("another")=>'),
    ('silence duration is measured in ms','as f64/1000.0','as f64/1.0'),
]:
    PLANTS.append((O,'openings',what,old,new))
for mod,what,old,new in [
    ('calls/opening','the greeting is never confirmed','rules::greeting_admitted(&mut ring.opening, message, ok, &world, fx)','rules::greeting_admitted(&mut ring.opening, message, !ok, &world, fx)'),
    ('calls/opening','retire does not look at the revoked phone','r.opening["principal"] == owner','r.opening["principal"] != owner'),
    ('calls/generation','a full device loses its auth proof','d.device_id == principal','d.device_id != principal'),
    ('calls/opening_launch','a final check returns no launch','result == "launch"','result == "never-launch"'),
    ('voice','drop the greeting trace tag','event[k] = v.clone()','event[k] = Value::Null'),
]:
    PLANTS.append(('clarp-host/src/'+mod+'.rs','host',what,old,new))
for file,what,old,new in [
    ('clarp-actor/src/lib.rs','pre-spawn refusal ends an opening','live.opening && !stopping','!live.opening && !stopping'),
    ('clarp-actor/src/lib.rs','the previous attempt cannot prove it was unsent','let previous_unsent = attempt > 1','let previous_unsent = attempt > u32::MAX - 1'),
    ('clarp-host/src/calls/opening_launch.rs','the restarted gate discards the previous attempt proof','op["launch_attempt"] == attempt - 1','op["launch_attempt"] == attempt'),
    ('clarp-host/src/calls/opening_launch.rs','the gate forgets which attempt claimed the initiative','op["launch_attempt"] = json!(attempt);','op["launch_attempt"] = json!(0);'),
]:
    PLANTS.append((file,'host',what,old,new))
for what,old,new in [
    ('a client admission may become the initiative','admission["channel"] != "call"','admission["channel"] == "never"'),
    ('adoption disregards the admission trace','&admission["trace_id"] != trace','&admission["trace_id"] == &serde_json::Value::Null'),
    ('an unrelated queued trace may be adopted','&w.queue["trace_id"] != trace','&w.queue["trace_id"] == &serde_json::Value::Null'),
    ('a committed admission is created again','if admission.is_null() {','if !admission.is_null() {'),
    ('a matching admission ignores the retained fence','retire(op, reason, w, fx);\n        return "fenced";','return "fenced";'),
]:
    PLANTS.append((O,'openings',what,old,new))
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
