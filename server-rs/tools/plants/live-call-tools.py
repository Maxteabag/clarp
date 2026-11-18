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

if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
