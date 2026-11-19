#!/usr/bin/env python3
"""Wrong rules for controller definitions, URL admission, deduplication and the device door."""
import runner

K = 'clarp-kernel/src/controller_actions.rs'
H = 'clarp-host/src/controller_actions.rs'
CLI = 'clarp-host/src/controller_action_tool.rs'
RUNS = {'vectors':['test','--test','controller_actions_vectors'],
        'door':['test','--test','host','controller_actions_are_defined_mapped_and_run_once_through_the_door']}
PLANTS = []

def rules(file,run,changes):
    PLANTS.extend((file,run,why,old,new) for why,old,new in changes)

rules(K,'vectors',[
    ('IDs lose the last byte', 'id.len() > 64', 'id.len() >= 64'),
    ('IDs gain one byte', 'id.len() > 64', 'id.len() > 65'),
    ('an ID may start with punctuation', '!id.as_bytes()[0].is_ascii_alphanumeric()', 'false'),
    ('uppercase IDs admitted', 'b.is_ascii_lowercase()', 'b.is_ascii_alphabetic()'),
    ('dash refused in IDs', "matches!(b, b'-' | b'_')", "b == b'_'"),
    ('underscore refused in IDs', "matches!(b, b'-' | b'_')", "b == b'-'"),
    ('a title loses the last character', 'text(data,"title",80)', 'text(data,"title",79)'),
    ('a title gains another character', 'text(data,"title",80)', 'text(data,"title",81)'),
    ('strict boolean lost', 'Value::as_bool', '|v| Some(python::truthy(v))'),
    ('disabled by default', 'Some(true), Value::as_bool', 'Some(false), Value::as_bool'),
    ('URL loses its last character', 'text(data, "url", 2000)', 'text(data, "url", 1999)'),
    ('URL gains a character', 'text(data, "url", 2000)', 'text(data, "url", 2001)'),
    ('a missing body becomes null', 'unwrap_or_else(|| json!({}))', 'unwrap_or(Value::Null)'),
    ('non-object bodies pass', 'if !body.is_object()', 'if false'),
    ('body limit loses its last byte', '> 8192', '>= 8192'),
    ('body limit gains a byte', '> 8192', '> 8193'),
    ('body is measured without ASCII escaping', 'python::dumps_ascii(&body)', 'python::dumps(&body)'),
    ('session loses its last character', 'text(data, "session", 128)', 'text(data, "session", 127)'),
    ('session gains a character', 'text(data, "session", 128)', 'text(data, "session", 129)'),
    ('prompt loses its last character', 'text(data, "text", 4000)', 'text(data, "text", 3999)'),
    ('prompt gains a character', 'text(data, "text", 4000)', 'text(data, "text", 4001)'),
    ('whitespace not stripped from field strings', 'Value::String(s) => python::strip(s)', 'Value::String(s) => s'),
    ('HTTPS loopback allowed by default', 'address.0.scheme_str() == Some("http")', 'true'),
    ('IPv6 loopback missing', '"localhost" | "[::1]"', '"localhost"'),
    ('localhost missing', '"localhost" | "[::1]"', '"[::1]"'),
    ('IPv4 loopback missing', '"127.0.0.1" | "localhost"', '"localhost"'),
    ('configured port ignored', 'p != target.port_u16().unwrap_or(default)', 'false'),
    ('scope admits a different path', 'path.strip_prefix(want)', 'Some("")'),
    ('path colon accepted after allowed prefix', "tail.starts_with([\'/\', \'?\', \'#\'])", "tail.starts_with([\'/\', \'?\', \'#\', \':\'])"),
    ('configured trailing slash not a prefix', "want.ends_with('/')", 'false'),
    ('a normal exact configured path refused', 'tail.is_empty() ||', 'false ||'),
    ('different device wins over matching device', '[device, "*"]', '["*", device]'),
    ('mapping ignores the button', '.find(|m| m["device"] == d && m["button"] == button && m["gesture"] == gesture)', '.find(|m| m["device"] == d && m["gesture"] == gesture)'),
    ('mapping device limit loses a character', 'device.len() > 128', 'device.len() >= 128'),
    ('mapping device limit gains a character', 'device.len() > 128', 'device.len() > 129'),
    ('mapping device may start with punctuation', '!device.as_bytes()[0].is_ascii_alphanumeric()', 'false'),
    ('unknown mapping action allowed', 'if !aid.is_empty() && !self.actions.contains_key(aid)', 'if false'),
    ('empty device does not default to wildcard', 'let device = if device.is_empty()', 'let device = if false'),
    ('delete leaves the mappings', 'self.mappings.retain(|m| m["action_id"] != id);', 'self.mappings.retain(|_| true);'),
    ('first revision ignores current wall clock', 'now.max(self.revision.saturating_add(1))', 'self.revision.saturating_add(1)'),
    ('revision may repeat', 'now.max(self.revision.saturating_add(1))', 'now'),
    ('wrong TTL on its exact boundary', 'at >= now.saturating_sub(REQUEST_MS)', 'at > now.saturating_sub(REQUEST_MS)'),
    ('first expired receipt reused', '600_000;', '600_001;'),
    ('debounce lasts one millisecond longer', 'elapsed_ms >= DEBOUNCE_MS', 'elapsed_ms > DEBOUNCE_MS'),
    ('last debounced millisecond treated as expired', '1_500;', '1_499;'),
    ('HTTP 199 counted as success', '(200..300).contains(&status)', '(199..300).contains(&status)'),
    ('HTTP 200 counted as failure', '(200..300).contains(&status)', '(201..300).contains(&status)'),
    ('HTTP 299 counted as failure', '(200..300).contains(&status)', '(200..299).contains(&status)'),
    ('HTTP 300 counted as success', '(200..300).contains(&status)', '(200..301).contains(&status)'),
    ('target reply one character too long', 'python::first(text,500)', 'python::first(text,501)'),
    ('request id not bounded', 'python::first(python::strip(&id), 128)', 'python::first(python::strip(&id), 129)'),
    ('audit device words not bounded', 'python::first(&word(&data[key]), 128)', 'python::first(&word(&data[key]),129)'),
])
rules(H,'door',[
    ('limited and full devices can define actions', 'op != "mappings" && principal != Principal::Administrator', 'false'),
    ('full devices cannot map', 'op != "mappings" && principal != Principal::Administrator', 'principal != Principal::Administrator'),
    ('creation returns 200', 'StatusCode::CREATED', 'StatusCode::OK'),
    ('receipt reuse flag lost', 'result["duplicate"] = true.into()', 'result["duplicate"] = false.into()'),
    ('debounced receipt lost', 'return record_result(app, &request, result, event).await;', 'return Ok(result);'),
    ('defaults allow-list reused after configuration changes', 'Address::allowed(action["url"].as_str().unwrap_or_default(), &allow_list(app))', 'Address::allowed(action["url"].as_str().unwrap_or_default(), &[])'),
    ('post uses an empty object', 'python::dumps_ascii(&action["body"])', '"{}".to_string()'),
    ('run result misses title', 'result["title"] = action["title"].clone()', 'result["title"] = Value::Null'),
    ('successful HTTP POST has wrong content type', '"Content-Type", "application/json"', '"Content-Type", "text/plain"'),
    ('send is delivered to the wrong session', 'session: action["session"].as_str().unwrap_or_default().into()', 'session: "absent-fixture".into()'),
    ('a prompt loses its text', 'text: action["text"].as_str().unwrap_or_default().into()', 'text: String::new()'),
    ('newest audit ordering reversed', 'rows.into_iter().rev()', 'rows.into_iter()'),
    ('disabled devices mislabelled', 'format!("device:{device_id}")', '"administrator".into()'),
    ('registry updates made durable SSE', 'app.hub.ephemeral(', 'app.hub.publish('),
])
rules(K,'door',[
    ('disabled actions run', 'if action["enabled"] == false', 'if false'),
    ('failed effects hold the debounce', 'last.is_some_and(|r| r["ok"] != true)', 'false'),
    ('in-flight debounce has the wrong duration', 'answer["duration_ms"] = 0.into()', 'answer["duration_ms"] = 1.into()'),
    ('in-flight debounce has wrong flag', 'answer["debounced"] = true.into()', 'answer["debounced"] = false.into()'),
])
rules('clarp-host/src/edge.rs','door',[('contract is off by one','("controller_actions", 63)','("controller_actions", 64)')])
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
