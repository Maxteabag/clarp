#!/usr/bin/env python3
"""Wrong rules for controller definitions, URL admission, deduplication and the device door."""
import runner

K = 'clarp-kernel/src/controller_actions.rs'
D = 'clarp-kernel/src/destination.rs'
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

# Where an action may send (R133): the Python Host's rule (`url_destination.py`), in the kernel's own module.
rules(D,'vectors',[
    ('(a) a prefix without a port admits every port', '        if !any_port && self.port != prefix.port {', '        if false {'),
    ('(a) the loopback prefixes admit their own port alone', '.map(|prefix| (prefix, true));', '.map(|prefix| (prefix, false));'),
    ("(b) the scheme's own port is named in what is sent", '        let netloc = if self.port == own_port(&self.scheme) {', '        let netloc = if false {'),
    ('(c) a prefix with a query is taken', "(found.query.is_empty() && !text.contains('#'))", "(!text.contains('#'))"),
    ('(c) a prefix with a fragment is taken', "(found.query.is_empty() && !text.contains('#'))", '(found.query.is_empty())'),
    ('(d) a host of any signs is a name', "    raw.split('.').all(label).then(|| raw.to_string())", "    Some(raw.to_string())"),
    ('(d) a host in capitals is another host', '    let host = host(&hostname.to_lowercase())?;', '    let host = host(hostname)?;'),
    ('(d) an IPv6 literal keeps its spelling', '        None => parsed.to_string(),', '        None => address.to_string(),'),
    ('(e) a path that only begins as the prefix\'s is under it', ".is_some_and(|tail| tail.starts_with('/'))", '.is_some()'),
    ("(e) the prefix's own path is not under it", '            || self.path == base\n', ''),
    ("(e) a prefix's closing slash is part of its boundary", "        let base = prefix.path.trim_end_matches('/');", '        let base = prefix.path.as_str();'),
    ('user information is taken', "    if netloc.contains('@') || hostname.is_empty() {", '    if hostname.is_empty() {'),
    ('a space is taken', '        .any(|sign| python_space(sign) || u32::from(sign) < 32)', '        .any(|sign| u32::from(sign) < 32)'),
    ('a control sign is taken', '        .any(|sign| python_space(sign) || u32::from(sign) < 32)', '        .any(python_space)'),
    ('a port with a sign before its digits is taken', '        if !port.chars().all(|sign| sign.is_ascii_digit()) {', '        if false {'),
    ('another scheme is taken', 'const SCHEMES: [(&str, u16); 2] = [("http", 80), ("https", 443)];',
     'const SCHEMES: [(&str, u16); 3] = [("http", 80), ("https", 443), ("ftp", 21)];'),
    ('an empty query is sent', '        if !self.query.is_empty() {', '        if true {'),
    ('a prefix is not trimmed', '    let found = destination(text.trim_matches(python_space))?;', '    let found = destination(text)?;'),
    # R139, taken before the reference's PR: held by the rows of the vectors marked as awaiting it.
    ('R139 a path with a dot segment is taken', '    if dot_segment(&path) {\n        return None;\n    }\n', ''),
    ('R139 an encoded dot is no dot', '        let dots = segment.replace("%2e", ".").replace("%2E", ".");', '        let dots = segment.to_string();'),
    ('R139 an encoded dot in capitals is no dot', '        let dots = segment.replace("%2e", ".").replace("%2E", ".");', '        let dots = segment.replace("%2e", ".");'),
    ('R139 a single dot is no segment', '        dots == "." || dots == ".."', '        dots == ".."'),
    ('an address of a version to come is refused in brackets', "        return (hex && !after.is_empty() && !after.contains('\\n')).then_some(());", '        return None;'),
])
rules(H,'door',[
    ('limited and full devices can define actions', 'op != "mappings" && principal != Principal::Administrator', 'false'),
    ('full devices cannot map', 'op != "mappings" && principal != Principal::Administrator', 'principal != Principal::Administrator'),
    ('creation returns 200', 'StatusCode::CREATED', 'StatusCode::OK'),
    ('receipt reuse flag lost', 'result["duplicate"] = true.into()', 'result["duplicate"] = false.into()'),
    ('debounced receipt lost', 'return record_result(app, &request, result, event).await;', 'return Ok(result);'),
    ('defaults allow-list reused after configuration changes', 'destination::allowed(action["url"].as_str().unwrap_or_default(), &allow_list(app))', 'destination::allowed(action["url"].as_str().unwrap_or_default(), &[])'),
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
rules(H,'door',[
    ('omit the invalid-entry journal notice','"config.controller_action_urls refused {} allowed URL(s)','"config.controller_action_urls kept {} allowed URL(s)'),
    ('audit uses another bound on reads','.clamp(0, 500)', '.clamp(0, 499)'),
    ('delete the newest audit row instead of the oldest','rows.drain(..rows.len() - rule::AUDIT_KEEP);','rows.truncate(rule::AUDIT_KEEP);'),
    ('failure receipts report success on retry','result["duplicate"] = true.into();','result["ok"] = true.into(); result["duplicate"] = true.into();'),
    ('the stored user agent changes','"User-Agent", "clarp-controller-action"','"User-Agent", "action"'),
    ('put the wrong status on a retained HTTP failure','rule::http_result(status,','rule::http_result(status.saturating_add(1),'),
])
rules(K,'door',[
    ('audit retains one fewer row','AUDIT_KEEP: usize = 500;', 'AUDIT_KEEP: usize = 499;'),
    ('audit retains one extra row','AUDIT_KEEP: usize = 500;', 'AUDIT_KEEP: usize = 501;'),
])
rules(CLI,'door',[
    ('an empty update reports success','ToolAnswer::said(1, "nothing to update")','ToolAnswer::said(0, "nothing to update")'),
    ('a mapping is missing from the printed list','← {prefix}', '- {prefix}'),
    ('a failed run exits successfully','if v["ok"] == true { 0 } else { 1 }','0'),
])
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
