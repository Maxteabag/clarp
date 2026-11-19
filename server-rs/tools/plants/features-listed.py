"""Wrong rules for what the features piece built: the chat a recording is for (transcription_session_header)."""
import runner

K='clarp-kernel/src/heard.rs'
RUNS={
    'chat':['test','--test','client_chat_vectors'],
    'wire':['test','--test','host','a_recording_is_for_the_chat_its_phone_names_else_the_one_it_last_opened'],
}
PLANTS=[(K,'chat',label,old,new) for label,old,new in [
    ('the key is of who it is in small letters','Sha256::digest(principal.as_bytes())','Sha256::digest(principal.to_lowercase().as_bytes())'),
    ('kept with the time first','format!("{{\\"session\\":{session},\\"at\\":{now_ms}}}")','format!("{{\\"at\\":{now_ms},\\"session\\":{session}}}")'),
    ('kept for a day','pub const CLIENT_CHAT_FOR_MS: i64 = 12 * 3600 * 1000;','pub const CLIENT_CHAT_FOR_MS: i64 = 24 * 3600 * 1000;'),
    ('twelve hours to the millisecond are over','if now_ms.saturating_sub(at) > CLIENT_CHAT_FOR_MS {','if now_ms.saturating_sub(at) >= CLIENT_CHAT_FOR_MS {'),
    ('a time that is text is none','Some(Value::String(at)) => match crate::python::int(at) {','Some(Value::String(at)) => match None::<i128>.filter(|_| at.is_empty()) {'),
    ('a time without its kind is now','None => 0,\n        Some(Value::Number(at))','None => now_ms,\n        Some(Value::Number(at))'),
    ('a session that is no text is none','Some(session) => crate::python::repr(session),','Some(_) => String::new(),'),
    ('the named chat is not stripped','let explicit = crate::python::strip(explicit);','let explicit = explicit;'),
    ('a named chat nobody has is taken','if !explicit.is_empty() && known(explicit) {','if !explicit.is_empty() {'),
    ('the last chat taken before the named one','if !explicit.is_empty() && known(explicit) {\n        return explicit.to_string();\n    }\n    if !last.is_empty() && known(last) {\n        return last.to_string();\n    }','if !last.is_empty() && known(last) {\n        return last.to_string();\n    }\n    if !explicit.is_empty() && known(explicit) {\n        return explicit.to_string();\n    }'),
    ('a last chat nobody has is taken','if !last.is_empty() && known(last) {','if !last.is_empty() {'),
]]
for file,label,old,new in [
    ('clarp-host/src/heard.rs','the named chat is not read','let named = header_text(headers, NAMED_CHAT);','let named = String::new();'),
    ('clarp-host/src/edge.rs','the client\'s last chat is not read','clarp_kernel::heard::client_chat(text.as_deref(), app.clock.now_ms())','clarp_kernel::heard::client_chat(None, app.clock.now_ms())'),
    ('clarp-host/src/edge.rs','nothing is kept','app.fleet.keep(OPENED, key, Some(kept.into_bytes()))','app.fleet.keep(OPENED, key, None)'),
    ('clarp-host/src/edge.rs','kept under another key than it is read','let key = clarp_kernel::heard::client_chat_key(&who);\n    if let Err(why)','let key = clarp_kernel::heard::client_chat_key(&who.to_uppercase());\n    if let Err(why)'),
    ('clarp-host/src/heard.rs','what was opened read before what is named','match heard::transcription_session(&named, "", known) {','match heard::transcription_session("", "", known) {'),
    ('clarp-host/src/heard.rs','the language of the shared focus','spoken_to(&directory, recorded_for, &ears.language)','spoken_to(&directory, directory.focus.as_deref().and_then(|id| directory.named(id)), &ears.language)'),
    ('clarp-host/src/edge.rs','a select is not kept','            opened_by(&app, &principal, request.session.trim()).await;\n',''),
    ('clarp-host/src/apps.rs','a focus is not kept','        crate::edge::opened_by(&app, &principal, session).await;\n',''),
    ('clarp-host/src/edge.rs','the administrator is nobody','Principal::Administrator if !app.config.auth_token.is_empty() => "administrator".into(),','Principal::Administrator if false => "administrator".into(),'),
]:
    PLANTS.append((file,'wire',label,old,new))
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
