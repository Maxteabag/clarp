#!/usr/bin/env python3
"""Wrong rules for live Shortcut events and controller edges."""
import runner

K = 'clarp-kernel/src/remote_action.rs'
H = 'clarp-host/src/apps.rs'
RUNS = {'vectors':['test','--test','remote_action'],
        'door':['test','--test','host','shortcuts_broadcast_once_to_live_listeners']}
PLANTS = [(K,'vectors',what,old,new) for what, old, new in [
    ('plain event loses its type','"type":"remote-action","action":action,"ts":now','"type":"remote","action":action,"ts":now'),
    ('plain event loses its time','"ts":now','"ts":0'),
    ('GET accepts a controller edge','"record" | "record-toggle" | "stop-agent"','"record" | "record-toggle" | "stop-agent" | "controller-event"'),
    ('omit record','"record" | "record-toggle"','"record-toggle"'),
    ('omit toggle','"record-toggle" | "stop-agent"','"stop-agent"'),
    ('omit stop',' | "stop-agent"',''),
    ('GET keeps an empty first value',' && !value.is_empty()',''),
    ('GET takes the last value','.find(|(key, value)| key == "action" && !value.is_empty())',
     '.filter(|(key, value)| key == "action" && !value.is_empty()).last()'),
    ('GET preserves case','python::strip(&action).to_lowercase()','python::strip(&action).to_owned()'),
    ('GET skips trimming','python::strip(&action).to_lowercase()','action.to_lowercase()'),
    ('POST preserves case','unwrap_or("")).to_lowercase()','unwrap_or("")).to_owned()'),
    ('non-object treated as missing action','.filter(Value::is_object)',''),
    ('empty input refused','if body.is_empty()','if false'),
    ('required field optional','if required && text.is_empty()', 'if false && text.is_empty()'),
    ('untrimmed controller field','let text = python::strip(&text);','let text = text.as_str();'),
    ('count UTF-8 bytes instead of characters','text.chars().count()', 'text.len()'),
    ('refuse exactly the text bound','count() > limit','count() >= limit'),
    ('accept one character too many','count() > limit','count() > limit + 1'),
    ('unrecognized button accepted','if !BUTTONS.contains(&button.as_str())','if false'),
    ('up is refused','"up" | "down"','"down"'),
    ('down is refused','"up" | "down"','"up"'),
    ('unknown gesture accepted','!GESTURES.contains(&gesture.as_str())','false'),
    ('queued defaults true','map_or(Some(false), Value::as_bool)','map_or(Some(true), Value::as_bool)'),
    ('wrong boolean type is accepted','.ok_or("queued must be a boolean")?','.unwrap_or(false)'),
    ('timestamp defaults to zero','"ts", now,','"ts", 0,'),
    ('age defaults nonzero','"age_ms", 0,','"age_ms", 1,'),
    ('duration defaults nonzero','"duration_ms", 0,','"duration_ms", 1,'),
    ('timestamp too large accepted','9_999_999_999_999','10_000_000_000_000'),
    ('duration longer than two minutes accepted','"duration_ms", 0, 120_000','"duration_ms", 0, 120_001'),
    ('age over two minutes accepted','"age_ms", 0, 120_000','"age_ms", 0, 120_001'),
    ('maximum refused','0..=limit','0..limit'),
    ('negative integer accepted','0..=limit','-1..=limit'),
    ('null integer treated as absent','None => default','None | Some(Value::Null) => default'),
    ('keep an empty controller name','if !controller.is_empty()', 'if true'),
    ('omit supplied controller name','if !controller.is_empty()', 'if false'),
    ('replace caller event id with drawn id','if id.is_empty(){draw()}else{id}','draw()'),
    ('leave missing event id empty','if id.is_empty(){draw()}else{id}','id'),
    ('omit id from reply','reply["controller_event_id"] = id.clone();','let _ = id;'),
    ('accepted reply reports false','json!({"ok":true})','json!({"ok":false})'),
]]
PLANTS.extend([
    (K,'vectors','shortcut page differs','<title>ok</title>','<title>done</title>'),
    (H,'door','POST is replayed','app.hub.ephemeral(event, now);\n    Ok(Json(receipt))','app.hub.publish(event, now);\n    Ok(Json(receipt))'),
    (H,'door','GET is replayed','app.hub.ephemeral(event, now);\n    Ok((', 'app.hub.publish(event, now);\n    Ok(('),
    (H,'door','POST delivered twice','app.hub.ephemeral(event, now);\n    Ok(Json(receipt))','app.hub.ephemeral(event.clone(), now); app.hub.ephemeral(event, now);\n    Ok(Json(receipt))'),
    (H,'door','GET is plain text','(axum::http::header::CONTENT_TYPE, "text/html")','(axum::http::header::CONTENT_TYPE, "text/plain")'),
    (H,'door','use fewer random bytes','|| ids::secret(12)','|| ids::secret(11)'),
])
for key, bound in [('button', 16), ('controller_event', 32), ('controller_id', 128), ('controller_event_id', 128)]:
    required = 'true' if key in ('button', 'controller_event') else 'false'
    old = f'field(&data, "{key}", {required}, {bound})?'
    for wrong in [bound - 1, bound + 1]:
        PLANTS.append((K, 'vectors', f'{key} wrongly bounded at {wrong}', old,
                       f'field(&data, "{key}", {required}, {wrong})?'))
for key, variable, other in [('ts','ts','age'), ('duration_ms','duration','age'), ('age_ms','age','duration'),
                              ('controller_event','gesture','button'), ('button','button','gesture'), ('queued','queued','false')]:
    PLANTS.append((K, 'vectors', f'{key} sent with another value', f'"{key}":{variable}', f'"{key}":{other}'))
PLANTS.append(('clarp-kernel/src/devices.rs', 'vectors', 'limited device loses its Shortcut POST',
               '    "/remote-action",', '    "/other-shortcut",'))
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
