#!/usr/bin/env python3
"""Speech provider requests, response folds, fallback and the routes that serve them."""
import runner

RUNS = {
    'deepgram':['test','-p','clarp-speech','--lib','both_deepgram_versions_send'],
    'guard':['test','-p','clarp-host','--lib','google_collisions_and_acknowledgements_match'],
    'batch':['test','--test','speech_engines','ink_batch_matches'],
    'ink':['test','-p','clarp-speech','--lib','ink_urls_preserve'],
    'google':['test','-p','clarp-speech','--lib','google_voice::tests'],
    'eleven':['test','--test','speech_engines','elevenlabs_reads'],
    'pcm':['test','--test','speech_engines','ink_sends'],
    'fallback':['test','--test','speech_engines','an_engine_may_take_over'],
    'host':['test','--test','host','the_other_speech_engines_answer_the_apps'],
}
D='clarp-speech/src/deepgram_voice.rs'
I='clarp-speech/src/ink.rs'
G='clarp-speech/src/google_voice.rs'
E='clarp-speech/src/eleven_voice.rs'
S='clarp-speech/src/speak.rs'
V='clarp-host/src/voice.rs'
R='clarp-host/src/voices.rs'
PLANTS=[]


def wrong(file, run, why, before, after):
    PLANTS.append((file,run,why,before,after))


def words(file, run, parts):
    for word in parts:
        wrong(file,run,'the wire spelling changes: '+word,'"'+word+'"','"'+word+'_wrong"')


words(D,'deepgram',['flux-','v2','v1','model','encoding','mp3','container','none'])
wrong(D,'deepgram','wrong synthesis path','/{version}/speak?', '/{version}/talk?')
wrong(D,'host','wrong authorization scheme','Token {key}','Bearer {key}')
wrong(D,'host','audio loses the final byte of every read','sink(&buf[..n]);','sink(&buf[..n.saturating_sub(1)]);')
wrong(D,'host','success status is treated as an error','if !response.status().is_success()', 'if response.status().is_success()')

words(I,'ink',['sample_rate','16000','pcm_s16le','cartesia_version','keyterm'])
for why,old,new in [
        ('one fewer term fits','.take(100)','.take(99)'),
        ('one extra term fits','.take(100)','.take(101)'),
        ('a term that fits exactly is dropped','chars > 1200','chars >= 1200'),
        ('one extra character fits','chars > 1200','chars > 1201'),
        ('language dropped from socket URL','("language", language)','("missing_language", language)'),
        ('model dropped from socket URL','("model", self.model)','("not_model", self.model)'),
        ('wrong socket resource','/stt/websocket?','/stt/batch?'),
        ('different API date','2026-08-14','2025-01-01')]:
    wrong(I,'ink',why,old,new)
for why,old,new in [
        ('shorter audio frames','pcm.chunks(3200)','pcm.chunks(3199)'),
        ('longer audio frames','pcm.chunks(3200)','pcm.chunks(3201)'),
        ('close before finalize','["finalize", "close"]','["close", "finalize"]'),
        ('partial instead of final transcripts','event["is_final"] == true','event["is_final"] == false'),
        ('wrong transcript field','event["text"].as_str()','event["other"].as_str()'),
        ('whitespace at the ends survives','Heard::text(text.trim(),','Heard::text(text,'),
        ('error messages lose the title fallback','event["title"].as_str()','event["subtitle"].as_str()'),
        ('error without detail is misreported','cartesia stt stream error','unexpected error')]:
    wrong(I,'pcm',why,old,new)

for why,old,new in [
        ('ampersands survive SSML escaping','"&amp;"','"&"'),('less-than survives escaping','"&lt;"','"<"'),
        ('greater-than survives escaping','"&gt;"','">"'),('seconds are not converted','1000.0','100.0'),
        ('a pause can exceed three seconds','min(3000.0)','min(3001.0)'),
        ('largest legal pause is shortened','min(3000.0)','min(2999.0)'),
        ('fractional ties round away','round_ties_even()','round()'),('zero duration produces SSML','*ms > 0','*ms <= 3000'),
        ('emotion tags spoken','let text = strip_emotion_tags(text);','let text = text.to_string();'),
        ('SSML returned as plain text','"ssml":format!','"text":format!'),('plain input becomes SSML','"text":plain.split_whitespace()','"ssml":plain.split_whitespace()'),
        ('a different root element','<speak>{}</speak>','<text>{}</text>'),
        ('a 201 response is accepted','200 => Ok(())','200 | 201 => Ok(())'),
        ('quota is an authentication failure','429 => Err("google_quota")','429 => Err("google_auth_failed")'),
        ('a 403 is a generic failure','401 | 403 =>','401 =>'),
        ('a 401 is a generic failure','401 | 403 =>','403 =>'),
        ('missing audio is read by a different key','data["audioContent"]','data["sound"]'),
        ('empty audio is accepted','if bytes.is_empty()','if !bytes.is_empty()'),
        ('the maximum permitted response grows by a byte','raw.len() > RESPONSE_MAX','raw.len() > RESPONSE_MAX + 1')]:
    wrong(G,'google',why,old,new)

for why,old,new in [
        ('authorization frame loses its key','"xi_api_key":key','"api_key":key'),
        ('initial space is empty','"text":" ","xi_api_key"','"text":"","xi_api_key"'),
        ('a line is not sent','json!({"text":text})','json!({"text":""})'),
        ('EOS has a space','json!({"text":""})','json!({"text":" "})'),
        ('wrong stability','"stability":0.5,"similarity_boost":0.75,"speed":speed','"stability":0.4,"similarity_boost":0.75,"speed":speed'),
        ('wrong similarity','"similarity_boost":0.75,"speed":speed','"similarity_boost":0.76,"speed":speed'),
        ('wrong configured speed','"speed":speed','"speed":1.0'),
        ('wrong chunk schedule','[50,90,120,150,200]','[50,90,120,150,201]'),
        ('non-final audio ends the stream','msg["isFinal"] == true','msg["isFinal"] != true'),
        ('zero bytes count as begun','let begun = produced > 0;','let begun = produced == 0;'),
        ('empty frame is a different error','empty frame from server','empty frame'),
        ('failure to parse a frame loses its class','server sent non-JSON frame','invalid frame'),
        ('a bad encoding loses its class','bad base64 from server','invalid encoded frame'),
        ('a provider refusal loses its class','ElevenLabs refused synthesis','synthesis failed')]:
    wrong(E,'eleven',why,old,new)
wrong(S,'fallback','speech repeats after partial audio','Self::Failed { begun, .. } => !begun','Self::Failed { begun, .. } => *begun')
wrong(S,'fallback','fallback stops before it can begin','if !another {','if another {')

for why,old,new in [
        ('provider settings are not saved','("speech.provider".into(), provider)','("other.provider".into(), provider)'),
        ('fallback settings are not saved','("speech.fallback".into(), fallback)','("other.fallback".into(), fallback)'),
        ('a live change claims a restart','result["restart_scheduled"] = json!(false);','result["restart_scheduled"] = json!(true);'),
        ('legacy preview events lose their URL','"url":format!("/audio/{name}")','"url":format!("/clips/{name}")'),
        ('legacy picker id cannot be selected','"selection_value":json!({"cartesia":id})','"selection_value":json!({"google":id})'),
        ('preview lookup has the wrong query key','/cartesia-voice-preview?id={quoted}','/cartesia-voice-preview?voice={quoted}')]:
    wrong(R,'host',why,old,new)

for why,old,new in [
        ('missing executable marker differs','b"noexec"','b"absent"'),
        ('illegal executable marker differs','b"other"','b"invalid"'),
        ('file content marker differs','b"exec"','b"file"'),
        ('the acknowledgement uses a different fingerprint version','b"clarp-google-collision-v1"','b"clarp-google-collision-v2"'),
        ('part lengths use another byte order','(part.len() as u64).to_be_bytes()','(part.len() as u64).to_le_bytes()'),
        ('the operator choice has another spelling','Some("builtin")','Some("built-in")'),
        ('an incomplete inventory has the wrong refusal','return Some("google_id_unverifiable")','return Some("google_id_collision")'),
        ('a conflicting id has the wrong refusal','    Some("google_id_collision")','    Some("google_id_unverifiable")')]:
    wrong('clarp-host/src/google_voice_guard.rs','guard',why,old,new)
for why,old,new in [
        ('the batch request uses an incorrect key header','header("Authorization",','header("Other",'),
        ('the batch request uses another API version header','header("Cartesia-Version",','header("Cartesia-Date",'),
        ('the uploaded file is named after its MIME type','("file", file_name(content))','("file", content)'),
        ('the batch duration is absent','Ok(Heard::text(text, duration))','Ok(Heard::text(text, 0.0))'),
        ('a numeric duration encoded as a string is ignored','data["duration"].as_str()?','data["duration"].as_i64().map(|_| "0")?'),
        ('a response returns a different text','let text = data["text"]','let text = data["transcript"]')]:
    wrong(I,'batch',why,old,new)

for why,old,new in [
        ('5000 bytes refused','s.len() > 5000','s.len() >= 5000'),
        ('5001 bytes accepted','s.len() > 5000','s.len() > 5001'),
        ('Unicode counted in characters','s.len() > 5000','s.chars().count() > 5000'),
        ('allowed voice rejected','if !(self.allowed)(line.voice)','if (self.allowed)(line.voice)'),
        ('encoded size limit ignored','encoded.len() / 4 * 3 > AUDIO_MAX','encoded.len() / 4 * 3 > AUDIO_MAX + 4'),
        ('a different body field carries the input','"input":input','"speech":input'),
        ('Google receives another billing project header','header("x-goog-user-project", project)','header("other-project", project)')]:
    wrong(G,'host' if 'body field' in why or 'billing' in why else 'google',why,old,new)
for why,old,new in [
        ('the wrong legacy model is chosen','"eleven_flash_v2_5".into()','"another-model".into()'),
        ('the legacy speed default is changed','.unwrap_or(1.2)','.unwrap_or(0.9)'),
        ('the text preview takes the streaming transport','file: usage == SpeechUse::TextPreview','file: usage == SpeechUse::Turn'),
        ('a missing fallback prevents a clip failure event','if route.fallback.is_empty()','if !route.fallback.is_empty()'),
        ('the fallback supplies a different Google default','Some("nb-NO-Chirp3-HD-Kore".into())','Some("nb-NO-Chirp3-HD-Aoede".into())')]:
    if 'Google default' not in why:
        wrong(V,'host',why,old,new)
for why,old,new in [
        ('the legacy preview loses its character limit','first(text, 200)','first(text, 201)'),
        ('the local voice setting is not persisted','("speech.local_voice".into(), voice)','("speech.other_voice".into(), voice)'),
        ('request uses the wrong selection field','word("provider", "")','word("engine", "")'),
        ('legacy picker uses the wrong name','r["name"].as_str()','r["title"].as_str()'),
        ('Google conflict applies only to the primary','provider == "google" || fallback == "google"','provider == "google" && fallback == "google"')]:
    wrong(R,'host',why,old,new)

for why,old,new in [
        ('text previews pass the whole provider map as a voice','name.is_some() || provider == "google"','name.is_none() || provider=="google"'),
        ('text preview sent as supplied to file engine','let direct = name.is_some();','let direct=true;'),
        ('the selected fallback gets the primary voice','let chosen = resolve(fallback);','let chosen=resolve(provider);')]:
    wrong(V,'host',why,old,new)
wrong(S,'fallback','live speech keeps emotion tags for every engine','match engine.speak_turn(Line { text, voice }, sink)','match engine.speak(Line { text, voice }, sink)')

# A mutation must not allow a stand-in to be replaced by a real speech or token endpoint.
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
