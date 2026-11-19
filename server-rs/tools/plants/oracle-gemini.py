#!/usr/bin/env python3
"""Second voice translation, conversation effects, billing and transport."""
import runner

RUNS = {name: ['test', '--test', target] for name, target in (
    ('gemini', 'oracle_gemini_vectors'), ('usage', 'oracle_live_usage_vectors'),
    ('conversation', 'oracle_conversation_vectors'))}
RUNS['host'] = ['test', '--test', 'host', '--', 'a_call_of_the_oracle_uses_its_second_voice']
G = 'clarp-kernel/src/oracle/gemini.rs'
U = 'clarp-kernel/src/oracle/usage.rs'
C = 'clarp-kernel/src/oracle/conversation.rs'
H = 'clarp-host/src/oracle/upstream.rs'
PLANTS = [
    (G, 'gemini', 'hold extends past ten minutes', 'now - at >= 600.', 'now - at > 600.'),
    (G, 'gemini', 'too little time to resume', 'SETUP_SECONDS: u64 = 10', 'SETUP_SECONDS: u64 = 9'),
    (G, 'gemini', 'a phone interrupt unmutes the reply', 'self.muted |= self.speaking', 'self.muted = false'),
    (G, 'gemini', 'sound is declared at the wrong rate', 'audio/pcm;rate=24000', 'audio/pcm;rate=16000'),
    (G, 'gemini', 'a routed answer interrupts the speaker', 'WHEN_IDLE', 'INTERRUPT'),
    (G, 'gemini', 'a cancelled function receives a response', 'if request.cancelled', 'if !request.cancelled'),
    (G, 'gemini', 'a goAway resumes too late', 'left - 2.', 'left - 1.'),
    (G, 'gemini', 'a silent gap cannot resume before the deadline', '!self.speaking || now >= at', 'now >= at'),
    (G, 'gemini', 'thoughts are billed for half the tokens', 'count as f64 * 4.5 / 1e6', 'count as f64 * 2.25 / 1e6'),
    (G, 'gemini', 'resumption is counted twice', 'self.reconnects += 1', 'self.reconnects += 2'),
    (U, 'usage', 'negative seconds and charges are accepted', 'n >= 0.', 'n >= -1.'),
    (U, 'usage', 'a full context window is ignored', '<= Some(1.)', '< Some(1.)'),
    (U, 'usage', 'OpenAI voice is charged twice', 'n * 0.05', 'n * 0.10'),
    (C, 'conversation', 'usage arrives before the first frame', 'event["type"] != "session.started" && self.usage.observe(event)', 'self.usage.observe(event)'),
    (H, 'host', 'a held voice is tried again', 'app.oracle.calls.healthy()', 'true'),
    (H, 'host', 'a new socket gets the first setup again', 'socket.send(Message::text(setup.to_string()))', 'socket.send(Message::text("{}"))'),
]

# Each field is independently visible to a peer that speaks the corresponding protocol.
# Replace exactly that spelling, preserving the shape of the other fields.
SETUP = 'clarp-kernel/src/oracle/gemini-setup.json'
for field in (
    'responseModalities', 'AUDIO', 'functionDeclarations', 'NON_BLOCKING',
    'description": "Hand the user', 'parameters', 'OBJECT', 'required', 'properties',
    'STRING', 'description": "The user', 'realtimeInputConfig', 'automaticActivityDetection',
    'silenceDurationMs', 'outputAudioTranscription', 'contextWindowCompression',
    'slidingWindow', 'sessionResumption', 'delegate_to_agent",', 'On this call',
):
    PLANTS.append((SETUP, 'gemini', 'setup changes ' + field, field, field.replace(field[0], 'x', 1)))

def change(file, run, changes):
    for old, new in changes:
        PLANTS.append((file, run, f'{old} becomes {new}', old, new))


change(G, 'gemini', [
    ('session["instructions"]', 'session["instruction"]'),
    ('session["input"]', 'session["inputs"]'),
    ('list(&v["content"])', 'list(&v["contents"])'),
    ('v["text"].as_str()', 'v["word"].as_str()'),
    ('words.push_str(&history)', 'words.push_str("")'),
    ('models/{}', 'model/{}'),
    ('json!(self.voice);','json!(self.model);'),
    ('json!(words);','json!("");'),
    ('if !self.languages.is_empty()', 'if self.languages.is_empty()'),
    ('"languageCodes"', '"languageCode"'),
    ('Some("session.start")', 'Some("session.begin")'),
    ('Some("session.input_audio.append")','Some("session.audio.append")'),
    ('Effect::Audio(', 'Effect::Send('),
    ('"realtimeInput"', '"realtime_input"'),
    ('"mimeType"', '"mime"'),
    ('event["audio"]', 'event["pcm"]'),
    ('kind @ ("session.commentary.append"', 'kind @ ("session.comment.append"'),
    ('| "session.thinking.append"', '| "session.think.append"'),
    ('| "session.instructions.append"', '| "session.instruction.append"'),
    ('kind == "session.commentary.append"', 'kind == "session.thinking.append"'),
    ('said(&event["content"])', 'said(&event["text"])'),
    ('Some("session.close")', 'Some("session.end")'),
    ('self.closed = true;', 'self.closed = false;'),
    ('"client_closed"', '"closed"'),
    ('request.parts.push(text);','request.parts.insert(0,text);'),
    ('request.speak |= speak;', 'request.speak = speak;'),
    ('"[Host, speak] "', '"[Host, context] "'),
    ('"[Host, context] "', '"[Host, speak] "'),
    ('"clientContent"', '"client_content"'),
    ('"role":"user"', '"role":"model"'),
    ('"turnComplete":speak', '"turnComplete":!speak'),
    ('if self.calls.contains_key(id)', 'if !self.calls.contains_key(id)'),
    ('self.routing.as_deref() == Some(id)', 'self.routing.is_some()'),
    ('self.context(request.speak, text)', 'self.context(!request.speak, text)'),
    (r'request.parts.join("\n\n")', 'request.parts.join(" ")'),
    ('Sent to the Host.', 'Sent to the agent.'),
    ('"result":result','"result":id'),
    ('round(now-self.opened,1)', 'round(now-self.opened,0)'),
    ('round(self.estimate,6)', 'round(self.estimate,4)'),
    ('"reconnects":self.reconnects})\n    }\n    fn count', '"reconnects":0})\n    }\n    fn count'),
    ('"{side}TokensDetails"', '"{side}TokenDetails"'),
    ('row["tokenCount"].as_i64()', 'row["tokenCount"].as_u64().and_then(|n| i64::try_from(n).ok())'),
    ('said(&row["modality"])','said(&row["medium"])'),
    ('if modality.is_empty()', 'if !modality.is_empty()'),
    ('modality.to_lowercase()', 'modality.to_uppercase()'),
    ('("prompt", "AUDIO") => 3.', '("prompt", "AUDIO") => 2.'),
    ('("prompt", _) => 0.75', '("prompt", _) => 0.5'),
    ('(_, "AUDIO") => 12.', '(_, "AUDIO") => 11.'),
    ('_ => 4.5,', '_ => 4.,'),
    ('usage["thoughtsTokenCount"].as_i64()','usage["thoughtTokenCount"].as_i64()'),
    ('self.tokens.entry("thoughts"', 'self.tokens.entry("thinking"'),
    ('self.estimate += cost', 'self.estimate = cost'),
    ('&& !self.began {','{'),
    ('"provider":"gemini"', '"provider":"openai"'),
    ('truthy(&update["resumable"]) &&', 'truthy(&update["resumable"]) ||'),
    ('self.handle = update["newHandle"].clone()', 'self.handle = json!("first")'),
    ('go["timeLeft"]', 'go["time"]'),
    ('and_then(seconds).unwrap_or(5.)', 'and_then(seconds).unwrap_or(1.)'),
    ('if usage.is_object()', 'if usage.is_array()'),
    ('"session.usage.updated"', '"session.usage"'),
    ('message["serverContent"]','message["content"]'),
    ('truthy(&content["interrupted"])','truthy(&content["interrupt"])'),
    ('"session.output_audio.interrupted"', '"session.audio.interrupted"'),
    ('content["modelTurn"]["parts"]', 'content["model"]["parts"]'),
    ('part["inlineData"]["data"]', 'part["data"]["data"]'),
    ('self.speaking = true;', 'self.speaking = false;'),
    ('if !self.muted {','if self.muted {'),
    ('"session.output_audio.delta"', '"session.audio.delta"'),
    ('"inputTranscription", "session.input_transcript.delta"', '"inputTranscription", "session.output_transcript.delta"'),
    ('"outputTranscription", "session.output_transcript.delta"', '"outputTranscription", "session.input_transcript.delta"'),
    ('(now - self.opened) * 1000.', '(now - self.opened) * 100.'),
    ('"start_ms":ms', '"start_ms":0'),
    ('"end_ms":ms', '"end_ms":0'),
    ('"event_id":id()', '"event_id":"first"'),
    ('truthy(&content["turnComplete"])', 'truthy(&content["turnEnded"])'),
    ('message["toolCall"]["functionCalls"]','message["toolCall"]["functions"]'),
    ('call["id"].as_str().filter(|s| !s.is_empty())','call["id"].as_str()'),
    ('if call["name"] != DELEGATE','if call["name"] == DELEGATE'),
    ('Unknown function; use delegate_to_agent.', 'Unknown function.'),
    ('"session.delegation.created"', '"session.delegation"'),
    ('said(&call["args"]["request"])','said(&call["args"]["text"])'),
    ('message["toolCallCancellation"]["ids"]','message["toolCallCancellation"]["id"]'),
    ('call.cancelled = true;', 'call.cancelled = false;'),
    ('!self.closed && self.setup.is_some()', 'self.setup.is_some()'),
    ('self.setup.is_some() && truthy(&self.handle)', 'truthy(&self.handle)'),
    ('self.deadline = None;', 'self.deadline = Some(0.);'),
    ('"handle":handle','"resumed":handle'),
    ('"gemini.resumed"', '"gemini.connected"'),
    ('"reason":reason','"reason":"go_away"'),
    ('"toolResponse"', '"toolResult"'),
    ('"functionResponses"', '"functionResults"'),
    ('"name":name,"response":value', '"name":id,"response":value'),
    ("value.trim().strip_suffix('s')", "value.strip_suffix('s')"),
    ('digits(a) && digits(b)', 'digits(a) || digits(b)'),
])
change(U, 'usage', [
    ('if self.reason.is_some()', 'if false'),
    ('"session.started" | "session.closed"', '"session.begin" | "session.closed"'),
    ('| "session.usage.updated"', '| "session.usage"'),
    ('event["usage"]["seconds"]','event["usage"]["duration"]'),
    ('event["usage"]["voice_estimate_usd"]','event["usage"]["usd"]'),
    ('event["session"]["expires_at"]','event["session"]["expiry"]'),
    ('event["context_window"]["usage_ratio"]','event["context"]["usage_ratio"]'),
    ('reason.chars().take(120)','reason.chars().take(119)'),
    ('"unknown".into()', '"closed".into()'),
    ('self.usage_final = number(seconds)', 'self.usage_final = true'),
    ('"openai_api"', '"api"'), ('"chatgpt_subscription"', '"subscription"'),
    ('"gemini_api"', '"gemini"'), ('"voice_only"', '"all"'),
    ('"finalized": self.reason.is_some()', '"finalized": self.reason.is_none()'),
])
change(H, 'host', [
    ('choice["provider"] == "gemini"', 'choice["provider"] == "openai"'),
    ('connect(&s.address).await', 'None::<Voice>'),
    ('Hook::LocalInterrupt => s.machine.interrupt()', 'Hook::LocalInterrupt => {}'),
])
change(G, 'gemini', [
    ('        effects\n    }\n    pub fn due', '        effects.reverse(); effects\n    }\n    pub fn due'),
    ('Effect::Event(json!({"type":"session.started"', 'Effect::Send(json!({"type":"session.started"'),
    ('    pub fn may_drop_audio(&self) -> bool {\n        truthy(&self.handle)',
     '    pub fn may_drop_audio(&self) -> bool {\n        false'),
    ('request.parts.push(text);', 'request.parts.clear(); request.parts.push(text);'),
    ('self.muted |= self.speaking;', 'self.muted = true;'),
    ('"type":"oracle_v2.provider_observation"', '"type":"session.started"'),
    ('"sessionResumption"] = json!({"handle":handle})', '"sessionResumption"] = json!({})'),
])
ROUTING = 'clarp-kernel/src/oracle/routing.rs'
change(ROUTING, 'conversation', [
    ('wire.hook(super::conversation::Hook::DelegationRouting', 'wire.hook(super::conversation::Hook::DelegationFinished'),
    ('wire.hook(super::conversation::Hook::DelegationFinished', 'wire.hook(super::conversation::Hook::DelegationRouting'),
])
change(C, 'conversation', [
    ('wire.hook(Hook::LocalInterrupt);', 'wire.phone(json!({"type":"oracle_v2.interrupt"}));'),
    ('wire.phone(json!({ "type": "oracle_v2.playback_flush" }));', 'wire.phone(json!({ "type": "oracle_v2.quiet" }));'),
    ('self.usage.observe(event) && self.gemini', 'self.usage.observe(event) && !self.gemini'),
])
change(G, 'gemini', [
    ('message.get("goAway") {','message.get("goAway").filter(|go| go.is_object() || go.is_null()) {'),
])
# If a provider changes one optional field's type, killing the call loses every later word.
for path in (
    '["toolCall"]["functionCalls"][0]["args"]',
    '["usageMetadata"]["promptTokensDetails"]',
    '["usageMetadata"]["responseTokensDetails"]',
    '["serverContent"]["modelTurn"]',
    '["serverContent"]["modelTurn"]["parts"][0]["inlineData"]',
    '["serverContent"]["inputTranscription"]',
    '["serverContent"]["outputTranscription"]',
    '["serverContent"]["modelTurn"]["parts"]',
    '["toolCall"]["functionCalls"]',
    '["toolCallCancellation"]["ids"]',
):
    old = 'let mut effects = vec![];'
    PLANTS.append((G, 'gemini', f'close the call for a malformed {path}', old,
                   old + f' if message{path}.is_number() {{self.closed = true; return effects;}}'))

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
