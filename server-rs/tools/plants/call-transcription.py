#!/usr/bin/env python3
"""Call identity, the language and engine precedence, and their effects on a real clip."""
import runner

K = 'clarp-kernel/src/call_transcription.rs'
H = 'clarp-host/src/heard/call.rs'
EDGE = 'clarp-host/src/heard.rs'
RUNS = {'vectors':['test','--test','call_transcription_vectors'],
        'host':['test','--test','host','--','a_call_clip_is_heard_for_its_captured_participant']}
PLANTS = []


def changes(file, run, pairs):
    for old, new in pairs:
        PLANTS.append((file, run, f'{old} becomes {new}', old, new))


changes(K, 'vectors', [
    ('"gcl_"','"call_"'), ('tail.len() == 32','tail.len() <= 32'), ('tail.len() == 32','tail.len() >= 32'),
    ("b'a'..=b'f'", "b'A'..=b'F'"), ('"bad call id"','"unknown call"'),
    ('refused(400,','refused(409,'), ('"unknown call"','"no live call"'),
    ('owner != principal','false'), ('call["host_id"] != host','false'),
    ('"not your call"','"wrong call"'), ('call["state"] != "live"', 'call["state"] == "live"'),
    ('"call ended"','"not live"'), ('unwrap_or("ended")','unwrap_or("unknown")'),
    ('"call_id":id,"reason":reason','"call_id":id,"reason":"ended"'), ('p["session"] == session','p["persona"] == session'),
    ('"active" | "invited"','"active"'), ('"active" | "invited"', '"invited"'),
    ('"active" | "invited"', '"on_hold" | "active" | "invited"'), ('"not a call participant"','"not a participant"'),
    ('python::strip(raw_id)','raw_id'), ('python::strip(raw_session)','raw_session'),
    ('"google" | "elevenlabs"', '"google"'), ('"google" | "elevenlabs"','"elevenlabs"'),
    ('Some("google") => Some(language)','Some("google") => Some(first)'),
    ('(!language.contains(\',\')).then_some(language)','Some(first)'),
    ('heard::is_cloud(engine) => Some(first)','heard::is_cloud(engine) => Some(language)'),
    ('engine.ends_with(".en")','engine.ends_with("small")'),
    ('first == "en"', 'first == "no"'), ('Some(sent != language)','Some(false)'),
    ('"language_incompatible"','"incompatible"'), ('step.local_default = false','step.local_default = true'),
    ('"call_languages must be a string of ISO language codes"', '"bad language"'),
    ('"call_engines must be a comma-separated string of engines"', '"bad engines"'),
    ('heard::cloud_model(e).is_none()', 'heard::cloud_model(e).is_some()'),
    ('unknown transcription engine: {bad}', 'unknown engine: {bad}'),
    ('"call_languages is a language set, but a call engine takes a single language"','"single language required"'),
    ('call_languages {} can never work with {}','invalid language {} for {}'),
    ('chain.is_empty()', '!chain.is_empty()'), ('v.join(",")','v.join(" ")'),
])
changes(K, 'vectors', [
    ('Some((owner, call)) = found', 'Some((owner, call)) = found.filter(|_| false)'),
    ('.all(|b| b.is_ascii_digit()', '.any(|b| b.is_ascii_digit()'), ('"not your call"', '"unknown call"'),

    ('"call_id":id,"session":session', '"call_id":id,"session":id'),
    ('matches!(model, "" | "local" | "server-default")','matches!(model, "" | "local")'),

    ("engine.split(':').next()", "engine.split(':').last()"),
    ('sent != language', 'sent == language'),
    ('step.language = Some(Some(sent.into()))','step.language = Some(Some(language.into()))'),
    ('step.adapted = None', 'step.adapted = Some(true)'),
    ('step.language = None','step.language = Some(None)'),
    ('self.language.is_empty() || !self.engines.is_empty()', 'self.language.is_empty() && !self.engines.is_empty()'),


    ('if python::strip(s).is_empty()', 'if s.is_empty()'),
    ('languages::set(v)', 'languages::code(v)'),
    ('let list = languages::engines(s);','let list = vec![s.to_string()];'),
    ('chain.iter().any', 'chain.iter().all'),
    ('let usable = if chain.is_empty()', 'let usable = if !chain.is_empty()'),
    ('.all(|s| s.skip.is_some())', '.any(|s| s.skip.is_some())'),
    ('own_language.filter(|l| !l.is_empty())', 'own_language'),
    ('(l, "agent")', '(l, "call_policy")'),
    ('(self.language.as_str(), "call_policy")','(self.language.as_str(), "global")'),
    ('(global_language, "global")', '(global_language, "call_policy")'),
    ('if !own.is_empty()', 'if own.is_empty()'),
    ('(own.to_vec(), "agent")', '(own.to_vec(), "device")'),
    ('(self.engines.clone(), "call_policy")', '(self.engines.clone(), "agent")'),
    ('(vec![requested.into()], "device")','(vec![requested.into()], "global")'),
    ('heard::is_cloud(&self.global)', 'false'),
    ('"applied":from=="device"', '"applied":from!="device"'),
    ('"reason":(from!="device")', '"reason":(from=="device")'),
    ('!requested.is_empty()).then', 'requested.is_empty()).then'),
    ('"engine":engine,"outcome":"skipped"', '"engine":engine,"outcome":"error"'),
    ('"engine":engine,"outcome":"error"', '"engine":engine,"outcome":"ok"'),
    ('"engine":engine,"outcome":"ok"', '"engine":engine,"outcome":"error"'),
    ('"language":language,"ms":ms,"reason"', '"language":"auto","ms":ms,"reason"'),
    ('"first_code"', '"first_language"'),
    ('self.report["effective_engine"] = json!(engine)', 'self.report["effective_engine"] = Value::Null'),
    ('Value::Array(self.rows)','Value::Array(Vec::new())'),
    ('!live ||', 'false ||'), ('!policy.error.is_empty() ||', 'false ||'),
    ('listen_first || policy.configured()', 'listen_first && policy.configured()'),
    ('listen_first || policy.configured()', 'policy.configured()'),
    ('listen_first || policy.configured()', 'listen_first'),
    ('v["engines"]["source"] != "global"', 'false'),
    ('v["language"]["source"] != "global"', 'false'),
])
changes(H,'host',[
    ('m.record.session == session','m.record.session != session'),
    ('languages::AGENT_LANGUAGES_KEY','languages::LANGUAGE_KEY'),
    ('languages::AGENT_ENGINES_KEY','rules::ENGINES'),
    ('if call["state"] != "live"', 'if call["state"] == "live"'),
    ('answer["call"]["transcription"] = view', 'answer["transcription"] = view'),
    ('if !policy.error.is_empty()', 'if false'),
    ('"error":"call policy invalid"','"error":"no compatible engine"'),
    ('"call_policy_error":policy.error','"call_policy_error":policy.language'),
    ('policy.configured().then_some(language)','None'),
    ('if !rules::has_runnable_step(later, &contacted)','if true'),
    ('report.skipped(&step.engine, reason);','report.skipped(&step.engine, "skipped");'),
    ('step.adapted == Some(true)','false'),
    ('report.failed(&step.engine, handed, elapsed, reason)', 'report.failed(&step.engine, None, elapsed, reason)'),
])
changes(EDGE,'host',[
    ('headers.contains_key("x-clarp-call-id")','false'),
    ('header_text(headers, "x-transcription-session")','String::new()'),
    ('.record(clarp_kernel::python::strip(&id))','.record("")'),
    ('if let Some(report) = report {','if let Some(report) = report.filter(|_|false) {'),
    ('"call_languages": call.language','"call_languages": ""'),
    ('"call_engines": call.engines.join(",")','"call_engines": ""'),
    ('"call_policy_error": call.error','"call_policy_error": ""'),
    ('settings.push((key.into(), v))','settings.push((key.into(), String::new()))'),
])
F='clarp-host/src/fleet.rs'
# Replace the transaction with independent writes: injecting a failure into the
# second write must leave the first visible after restart and fail the test.
RUNS['settings']=['test','--lib','--','refused_settings_batch_preserves_the_directory_and_store']
changes(F,'settings',[
    ('        self.store\n            .put_together(&writes)\n            .map_err(|error| Unavailable(error.to_string()))?;\n        self.settings.extend(settings);',
     '        self.settings.extend(settings);\n        self.store.put_together(&writes).map_err(|error|Unavailable(error.to_string()))?;'),
    ('self.store\n            .put_together(&writes)\n            .map_err(|error| Unavailable(error.to_string()))?;',
     'for (table,key,bytes) in &writes {self.store.put(table,key,bytes.as_deref().unwrap_or_default()).map_err(|error|Unavailable(error.to_string()))?;}'),
])
changes('clarp-host/src/edge.rs','host', [('("call_transcription_policy", 74)','("call_transcription_policy", 73)')])
changes('clarp-kernel/src/heard.rs','vectors',[
    ('write(b"\\0call\\0")','write(b"call")'),
    ('part.len().to_string()', 'part.chars().count().to_string()'),
    ('part.len().to_string()', 'format!("0{}",part.len())'),
    ('write(audio);\n    write(&[0]);','write(audio);'),
    ('write(content_type.as_bytes());\n    write(&[0]);','write(content_type.as_bytes());'),
    ('write(model.as_bytes());\n    write(&[0]);','write(model.as_bytes());'),
    ('write(b":")','write(b",")'),
    ('write(b",")','write(b":")'),
    ('write(part.as_bytes())','write(&[])'),
    ('[&scope.principal, &scope.call_id, &scope.session]', '[&scope.call_id, &scope.principal, &scope.session]'),
    ('[&scope.principal, &scope.call_id, &scope.session]', '[&scope.principal, &scope.session]'),
    ('[&scope.principal, &scope.call_id, &scope.session]', '[&scope.call_id, &scope.session]'),
    ('[&scope.principal, &scope.call_id, &scope.session]', '[&scope.principal, &scope.call_id]'),
    ('write(if hands_free { b"1" } else { b"0" })','write(b"0")'),
    ('write(audio)','write(&[])'),
    ('write(content_type.as_bytes())','write(&[])'),
    ('write(model.as_bytes())','write(&[])'),
])
changes(K,'vectors',[
    ('call_id: id.into()', 'call_id: raw_id.into()'),
    ('session: session.into()', 'session: raw_session.into()'),
])
changes('clarp-host/src/vocabulary/store.rs', 'host', [
    ('r.audit["stt"] = report.clone()', 'r.audit["stt"] = Value::Null'),
])
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
