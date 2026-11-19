#!/usr/bin/env python3
"""Wrong rules for form events, spoken form replies and countdown Updates."""
import runner
RUNS={'vectors':['test','--test','forms_events'], 'events':['test','--test','host','a_forms_events_are_private_append_only_and_exported_at_one_snapshot']}
K='clarp-kernel/src/artifacts/form_events.rs'
H='clarp-host/src/library/events.rs'
T='clarp-host/src/artifact_tool.rs'
PLANTS=[]


def change(file,reason,old,new):
    PLANTS.append((file,'vectors' if file==K else 'events',reason,old,new))


for why,old,new in [
    ('empty event batch accepted','.filter(|v| (1..=32)', '.filter(|v| (0..=32)'),
    ('32nd record refused','.filter(|v| (1..=32)', '.filter(|v| (1..=31)'),
    ('33rd record accepted','.filter(|v| (1..=32)', '.filter(|v| (1..=33)'),
    ('malformed batch error changes','return Err("JSON object required");','return Err("object required");'),
    ('nonobject row error changes','return Err("event record must be an object");','return Err("object required");'),
    ('short ID accepted','16..=80','15..=80'),
    ('shortest valid ID refused','16..=80','17..=80'),
    ('longest valid ID refused','16..=80','16..=79'),
    ('overlong ID accepted','16..=80','16..=81'),
    ('underscore allowed in event ID',"c == b'-'","c != b'!'"),
    ('largest safe integer refused','<= 9007199254740991','< 9007199254740991'),
    ('first unsafe integer accepted','<= 9007199254740991','<= 9007199254740992'),
    ('sequence field names time','safe("client_seq")','safe("client_at")'),
    ('client time field names sequence','safe("client_at")','safe("client_seq")'),
    ('nonobject event error changes','"event must be an object"','"invalid event"'),
    ('last allowed byte refused','encoded.len() > 16384','encoded.len() >= 16384'),
    ('first extra byte accepted','encoded.len() > 16384','encoded.len() > 16385'),
    ('payload size counts characters','encoded.len() > 16384','encoded.chars().count() > 16384'),
    ('payload content omitted from retry digest','data["version"], encoded','data["version"],0'),
    ('draft disable refused','!key.is_null()','true'),
    ('empty draft key accepted','1..=80','0..=80'),
    ('longest draft key refused','1..=80','1..=79'),
    ('overlong draft key accepted','1..=80','1..=81'),
    ('missing draft key error changes','draft_key required; use null to disable','draft_key required'),
    ('zero limit accepted','1..=100','0..=100'),
    ('largest limit refused','1..=100','1..=99'),
    ('overlarge limit accepted','1..=100','1..=101'),
    ('cursor minimum moves','0..=i64::MAX as i128','1..=i64::MAX as i128'),
    ('snapshot may precede cursor','after..=i64::MAX as i128','0..=i64::MAX as i128'),
    ('maximum snapshot refused','after..=i64::MAX as i128','after..i64::MAX as i128'),
]: change(K,why,old,new)
for why,old,new in [
    ('old ID silently accepts different event','digest != event.digest','digest == event.digest'),
    ('version comparison inverted','data["version"] != v','data["version"] == v'),
    ('receipt uses client sequence','"seq":seq}));','"seq":event.client_seq}));'),
    ('pagination includes the exclusive cursor','<= after','< after'),
    ('snapshot excludes last visible record','if seq > through','if seq >= through'),
    ('page reports no continuation','more = true','more = false'),
    ('page cursor stays at start','next = seq','next = after'),
    ('global sequence jumps','seq.checked_add(1)','seq.checked_add(2)'),
    ('session ordinal jumps','ordinal.checked_add(1)','ordinal.checked_add(2)'),
    ('interleaved forms share a row namespace','key(id, &ordinal.to_string())','key("", &ordinal.to_string())'),
    ('client receives a negative receipt','"accepted":true','"accepted":false'),
]: change(H,why,old,new)
for why,old,new in [
    ('form config read skips the saved key','read(app, CONFIG, id.into())','read(app, CONFIG, "none".into())'),
    ('new event receives another form ID','"artifact_id":id,"version":v,"event_id":event.id','"artifact_id":"","version":v,"event_id":event.id'),
    ('stored event payload omitted','"event":event.value','"event":null'),
    ('receipt output uses another event ID','"event_id":event.id,"seq":seq','"event_id":"","seq":seq'),
    ('receipt is saved with another sequence','let receipt = (seq, event.digest)','let receipt = (seq+1, event.digest)'),
    ('the batch remembers the wrong ordinal','position = Some((ordinal, seq))','position = Some((ordinal+1, seq))'),
    ('snapshot falls back to the ordinal','through.unwrap_or(last.into())','through.unwrap_or(count.into())'),
    ('more flag raised a record too soon','if events.len() == limit','if events.len()+1 == limit'),
    ('next cursor leaks an extra event','next = seq;','next = seq+1;'),
    ('read-only reports accept events','!= Some(&Value::Bool(true))','== Some(&Value::Bool(true))'),
    ('form changes require read-only scope','scope: Scope::Limited','scope: Scope::Full'),
    ('form id is ignored when indexing','format!("{}:{id}{suffix}", id.len())','format!("{}:{suffix}", id.len())'),
]: change(H,why,old,new)
for why,old,new in [
    ('second page of export is lost','if page["has_more"] != true','if page["has_more"] == true'),
    ('export mode allows group reads','.mode(0o600)','.mode(0o640)'),
    ('snapshot receipt lies about export length','"events":total','"events":total+1'),
    ('draft tracking cannot be disabled','(!key.is_empty()).then_some(key)','Some(key)'),
    ('overwrite flag ignored','options.flags.contains("--overwrite")','false'),
    ('no-overwrite refuses a new export','!overwrite && target.exists()','!overwrite || target.exists()'),
]: change(T,why,old,new)
RUNS['reply']=['test','--test','host','a_forms_spoken_answer_belongs_to_the_submission_that_requested_it']
for why,old,new in [
 ('all form replies are spoken','speak: spoken,','speak: true,'),
 ('no form replies are spoken','speak: spoken,','speak: false,'),
 ('form answers are attached to the wrong message','format!("u-html-form-{submission_id}")','format!("u-{submission_id}")'),
]:
    PLANTS.append(('clarp-host/src/library.rs','reply',why,old,new))

PLANTS.append(('clarp-host/src/edge.rs','reply','queued form conceals its audio choice','"synthesize_audio": message.speak','"synthesize_audio": false'))
RUNS['clock']=['test','--test','forms_events','a_countdown_crosses_its_target']
RUNS['countdown']=['test','--test','host','countdowns_are_created_with_a_clock']
CLOCK='clarp-kernel/src/artifacts/countdown.rs'
for why,old,new in [
 ('draft countdown appears active','("history", "draft")','("working", "active")'),
 ('cancelled countdown returns to review','("history", "cancelled")','("review", "cancelled")'),
 ('expired countdown stays for review','("history", "expired")','("review", "expired")'),
 ('working countdown appears ready','("working", "active")','("review", "active")'),
 ('failed countdown is hidden','("correction", "failed")','("history", "failed")'),
 ('unknown state is paused','("unsupported", "unknown")','("working", "paused")'),
 ('target instant is still upcoming','now < ms','now <= ms'),
 ('last due millisecond is elapsed','< 60_000','< 59_999'),
 ('a full minute after zero is still due','< 60_000','<= 60_000'),
 ('invalid target hidden','("correction", "invalid_target")','("history", "invalid_target")'),
 ('positive timestamp offsets added','micros - i128::from(seconds)','micros + i128::from(seconds)'),
 ('unknown time zone accepted','zone.iana_name() == Some(name)','true'),
]:
    PLANTS.append((CLOCK,'countdown' if why=='unknown time zone accepted' else 'clock',why,old,new))

for why,old,new,run in [
 ('zero year admitted','civil.starts_with("0000")','civil.starts_with("9999")','countdown'),
 ('valid minute before midnight refused','minutes >= 24 * 60','minutes >= 24 * 60 - 1','countdown'),
 ('offset of a full day admitted','minutes >= 24 * 60','minutes > 24 * 60','countdown'),
 ('western offset sign lost',"if offset.starts_with('-') { -1 } else { 1 }","1",'clock'),
 ('fractional seconds truncated to hundredths','fraction.len().min(6)','fraction.len().min(2)','clock'),
 ('seconds counted as milliseconds','1_000_000.0 * 1000.0','1_000_000.0 * 1.0','clock'),
 ('completed countdown no longer projected','"ready" | "completed"','"ready" | "closing"','clock'),
 ('invalid date error changes','countdown target_at requires a valid date/time and explicit UTC offset','invalid countdown target','countdown'),
 ('wrong timestamp shape accepted as a date','ok_or(SHAPE)?','ok_or(DATE)?','countdown'),
 ('time zone refusal changes','countdown time_zone must be an IANA timezone','time zone required','countdown'),
]:
    PLANTS.append((CLOCK,run,why,old,new))
for why,old,new in [
 ('failed timer shown as ready','"correction" => Some("blocking")','"correction" => Some("review")'),
 ('ready timer hidden','"review" => Some("review")','"review" => None'),
 ('active timer hidden','"working" => Some("working")','"working" => None'),
]:
    PLANTS.append(('clarp-kernel/src/artifacts/updates.rs','countdown',why,old,new))
PLANTS.append(('clarp-kernel/src/artifacts.rs','countdown',
    'countdown required-field validation skips the publish-time type check',
    'let page = kind == "html_form";',
    'if kind == "countdown" && let Some(Value::Object(raw)) = &new.payload { require_payload(&kind, raw, ctx.published)?; } let page = kind == "html_form";'))
# The four former rollover mutations exercised a day-advance branch that
# does not exist in the 3.13 reference. Plant the opposite of its refusal.
PLANTS.append((CLOCK, 'countdown', 'hour 24 is admitted as midnight',
    'let civil = &parts[1];',
    'let normalized = parts[1].replace("T24:00:00","T00:00:00"); let civil = normalized.as_str();'))
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
